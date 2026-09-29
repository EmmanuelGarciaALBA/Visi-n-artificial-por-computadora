"""VisionServer: servicio web de visión artificial con comunicación OPC UA y TCP/IP.

Se abre desde cualquier PC de la misma red en  http://<IP-del-servidor>:<puerto>
"""
import asyncio
import copy
import logging
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import textwrap

import cv2
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .comms.opcua_server import OpcUaServer
from .comms.tcp_server import TcpServer
from .config import active_recipe, load_config, save_config
from .vision import yolo
from .vision.engine import VisionEngine
from .vision.safety import SafetyMonitor
from .vision.tools import TOOLS

log = logging.getLogger("main")
WEB_DIR = Path(__file__).resolve().parent / "web"


class State:
    engine: VisionEngine = None
    safety: SafetyMonitor = None
    opcua: OpcUaServer = None
    tcp: TcpServer = None
    cfg: dict = None
    websockets: set = set()
    loop: asyncio.AbstractEventLoop = None


S = State()


async def start_comms():
    if S.cfg["opcua"]["enabled"]:
        S.opcua = OpcUaServer(S.engine, S.cfg, S.safety)
        try:
            await S.opcua.start()
        except Exception as e:
            log.exception("No se pudo iniciar OPC UA")
            S.opcua.error = str(e)
    if S.cfg["tcp"]["enabled"]:
        S.tcp = TcpServer(S.engine, S.cfg, S.safety)
        try:
            await S.tcp.start()
        except Exception as e:
            log.exception("No se pudo iniciar TCP")
            S.tcp.error = str(e)


async def stop_comms():
    for srv in (S.opcua, S.tcp):
        if srv:
            try:
                await srv.stop()
            except Exception:
                log.exception("Error deteniendo comunicación")
    S.opcua = S.tcp = None


def _ws_listener(result):
    if S.loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast({"type": "result", "data": result}), S.loop)


def _ws_safety_listener(state):
    if S.loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast({"type": "safety", "data": state}), S.loop)


def _recipe_listener(recipe):
    """Cambio de receta (desde PLC, TCP o web): reconstruir nodos OPC UA y avisar a los navegadores."""
    if S.loop:
        asyncio.run_coroutine_threadsafe(_on_recipe_changed(recipe), S.loop)


async def _on_recipe_changed(recipe):
    if S.opcua:
        await S.opcua.rebuild_tools()
    await _ws_broadcast({"type": "recipe", "data": {"id": recipe["id"], "number": recipe["number"],
                                                    "name": recipe["name"]}})


def _uses_yolo(cfg):
    return cfg["safety"].get("enabled") or any(
        t["type"] in ("person_detect", "yolo_detect") for r in cfg["recipes"] for t in r.get("tools", []))


def _warm_up_yolo(cfg):
    """Carga el modelo en segundo plano para que la primera inspección no tarde ~15 s."""
    def run():
        try:
            yolo.detect(__import__("numpy").zeros((64, 64, 3), "uint8"), cfg["safety"].get("model"))
            log.info("Modelo YOLO listo")
        except Exception:
            log.exception("No se pudo cargar YOLO")
    if _uses_yolo(cfg):
        threading.Thread(target=run, name="yolo-warmup", daemon=True).start()


def _signature(cfg):
    """Lo que define los nodos OPC UA dinámicos: herramientas de la receta activa y zonas."""
    r = active_recipe(cfg)
    return (r["id"], [(t["id"], t["name"]) for t in r.get("tools", [])],
            [(z["id"], z["name"]) for z in cfg["safety"].get("zones", [])])


def _modules():
    return {"inspection": S.engine.running, "safety": bool(S.cfg["safety"].get("enabled"))}


async def _ws_broadcast(msg):
    for ws in list(S.websockets):
        try:
            await ws.send_json(msg)
        except Exception:
            S.websockets.discard(ws)


@asynccontextmanager
async def lifespan(app):
    S.loop = asyncio.get_running_loop()
    S.cfg = load_config()
    S.engine = VisionEngine(S.cfg)
    S.engine.add_listener(_ws_listener)
    S.engine.add_recipe_listener(_recipe_listener)
    S.safety = SafetyMonitor(S.engine)
    S.safety.add_listener(_ws_safety_listener)
    _warm_up_yolo(S.cfg)
    await start_comms()
    urls = ", ".join(f"http://{ip}:{S.cfg['server']['port']}" for ip in lan_ips())
    log.info("Interfaz web disponible en: %s", urls)
    yield
    await stop_comms()
    S.safety.stop()
    S.engine.stop()


app = FastAPI(title="VisionServer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

PUBLIC_PATHS = {"/login", "/api/login", "/static/style.css", "/static/login.js", "/favicon.ico"}
MASK = "********"


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Todo requiere sesión excepto la pantalla de login."""
    if request.url.path in PUBLIC_PATHS:
        return _no_cache(await call_next(request))
    session = auth.read_token(request.cookies.get(auth.COOKIE_NAME, ""))
    if session is None:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "no autenticado"}, status_code=401)
        return RedirectResponse("/login")
    request.state.session = session
    return _no_cache(await call_next(request))


def _no_cache(response):
    """El navegador debe revalidar la interfaz: así nunca mezcla una versión vieja con una nueva."""
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _require_admin(request: Request):
    if request.state.session["role"] != "admin":
        return JSONResponse({"error": "solo un administrador puede cambiar la configuración"}, status_code=403)
    return None


@app.get("/login")
async def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.post("/api/login")
async def login(data: dict):
    res = auth.authenticate(str(data.get("user", "")), str(data.get("password", "")))
    if res is None:
        await asyncio.sleep(1)  # frena intentos de fuerza bruta
        return JSONResponse({"error": "Usuario o contraseña incorrectos"}, status_code=401)
    name, role = res
    log.info("Inicio de sesión: %s (%s)", name, role)
    r = JSONResponse({"user": name, "role": role})
    r.set_cookie(auth.COOKIE_NAME, auth.make_token(name, role), httponly=True, samesite="strict",
                 max_age=auth.SESSION_HOURS * 3600)
    return r


@app.post("/api/logout")
async def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE_NAME)
    return r


@app.get("/api/me")
async def me(request: Request):
    return request.state.session


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # IP de la interfaz con ruta por defecto
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips = {ip for ip in ips if not ip.startswith(("127.", "169.254."))}
    return sorted(ips) or ["127.0.0.1"]


# ------------------------------------------------------------------ páginas
def _ui_version():
    """Cambia cada vez que se modifica un archivo de la interfaz (evita cachés viejas)."""
    return str(int(max(p.stat().st_mtime for p in WEB_DIR.iterdir())))


@app.get("/")
async def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", _ui_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ API
@app.get("/api/status")
async def status():
    e = S.engine
    _, frame = e.camera.latest()
    return {
        "camera": {"ok": e.camera.error is None, "error": e.camera.error, "fps": round(e.camera.fps, 1),
                   "size": [frame.shape[1], frame.shape[0]] if frame is not None else None},
        "opcua": {"enabled": S.cfg["opcua"]["enabled"], "running": bool(S.opcua and S.opcua.running),
                  "error": getattr(S.opcua, "error", None),
                  "endpoints": [f"opc.tcp://{ip}:{S.cfg['opcua']['port']}/visionserver/" for ip in lan_ips()]},
        "tcp": {"enabled": S.cfg["tcp"]["enabled"], "running": bool(S.tcp and S.tcp.running),
                "error": getattr(S.tcp, "error", None), "port": S.cfg["tcp"]["port"],
                "clients": len(S.tcp.clients) if S.tcp else 0},
        "web": [f"http://{ip}:{S.cfg['server']['port']}" for ip in lan_ips()],
        "last_result": e.last_result,
        "safety": S.safety.state,
        "modules": _modules(),
        "recipe": {k: e.recipe[k] for k in ("id", "number", "name")},
        "tool_types": list(TOOLS.keys()),
    }


@app.get("/api/config")
async def get_config():
    cfg = copy.deepcopy(S.cfg)
    if cfg["camera"].get("password"):
        cfg["camera"]["password"] = MASK
    return cfg


@app.put("/api/config")
async def put_config(new_cfg: dict, request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    old = S.cfg
    cfg = copy.deepcopy(new_cfg)
    if cfg["camera"].get("password") == MASK:  # sin cambios: conservar la guardada
        cfg["camera"]["password"] = old["camera"].get("password", "")
    # Ejecutar/Detener solo se cambia con sus botones (/api/modules), nunca al guardar configuración
    cfg["inspection"] = {**cfg.get("inspection", {}), "running": S.engine.running}
    cfg["safety"]["enabled"] = bool(old["safety"].get("enabled"))
    save_config(cfg)
    S.cfg = cfg
    S.engine.apply_config(cfg)
    restart_needed = cfg["server"] != old["server"]
    if cfg["opcua"] != old["opcua"] or cfg["tcp"] != old["tcp"]:
        await stop_comms()
        await start_comms()
    else:
        for srv in (S.opcua, S.tcp):
            if srv:
                srv.cfg = cfg
        if S.opcua and _signature(cfg) != _signature(old):
            await S.opcua.rebuild_tools()
    _warm_up_yolo(cfg)
    return {"ok": True, "restart_required": restart_needed}


@app.post("/api/recipe/select")
async def select_recipe(data: dict):
    """Cambiar de receta lo puede hacer cualquier usuario (operador incluido)."""
    r = await asyncio.get_running_loop().run_in_executor(None, S.engine.select_recipe, data.get("key"))
    if r is None:
        return JSONResponse({"error": "La receta no existe"}, status_code=404)
    return {"id": r["id"], "number": r["number"], "name": r["name"]}


@app.get("/api/safety")
async def safety_state():
    return S.safety.state


@app.post("/api/modules/{name}")
async def set_module(name: str, data: dict):
    """Ejecutar/Detener un módulo. Lo puede hacer cualquier usuario (es operación, no configuración)."""
    running = bool(data.get("running"))
    if name == "inspection":
        await asyncio.get_running_loop().run_in_executor(None, S.engine.set_running, running)
    elif name == "safety":
        S.cfg["safety"]["enabled"] = running
        save_config(S.cfg)
        if running:
            _warm_up_yolo(S.cfg)
        log.info("Zonas de seguridad %s", "EN EJECUCIÓN" if running else "DETENIDAS")
    else:
        return JSONResponse({"error": f"Módulo desconocido: {name}"}, status_code=404)
    mods = _modules()
    await _ws_broadcast({"type": "modules", "data": mods})
    return mods


@app.post("/api/trigger")
async def trigger():
    r = await asyncio.get_running_loop().run_in_executor(None, S.engine.inspect, "web")
    if r is None:
        return JSONResponse({"error": "La inspección está detenida. Presiona Ejecutar."}, status_code=409)
    return r


def _placeholder(msg):
    """Cuadro negro con el motivo por el que no hay imagen."""
    img = np.zeros((360, 640, 3), np.uint8)
    cv2.putText(img, "SIN IMAGEN DE LA CAMARA", (40, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 230), 2)
    for i, line in enumerate(textwrap.wrap(msg or "", 55)[:4]):
        cv2.putText(img, line, (40, 200 + 28 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    return img


def _stopped(img, text):
    """Imagen en vivo con una franja que indica que el módulo está detenido."""
    img = img.copy()
    cv2.rectangle(img, (0, img.shape[0] - 40), (img.shape[1], img.shape[0]), (20, 20, 20), -1)
    cv2.putText(img, text, (12, img.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (150, 150, 150), 2)
    return img


def _jpeg(img, quality=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


@app.get("/api/snapshot.jpg")
async def snapshot():
    _, img = S.engine.camera.latest()
    if img is None:
        return JSONResponse({"error": "sin imagen"}, status_code=503)
    return Response(_jpeg(img, 90), media_type="image/jpeg")


@app.get("/stream.mjpg")
async def stream(view: str = "result"):
    """Video MJPEG. view=live (en vivo con ROIs) | result (última inspección) | safety (zonas)
    | raw (cámara sin anotaciones, para dibujar ROIs y zonas)."""

    async def gen():
        loop = asyncio.get_running_loop()
        while True:
            if view == "safety":
                _, img = S.engine.camera.latest()
                if img is not None:
                    if S.safety.state["running"]:
                        img = S.safety.annotate(img)  # cuadro actual + última detección
                    elif not S.cfg["safety"].get("enabled"):
                        img = _stopped(img, "EN VIVO - MONITOR DE ZONAS DETENIDO")
            elif view == "raw":
                _, img = S.engine.camera.latest()
            elif view == "live":
                img = S.engine.live_view()
                if img is not None and not S.engine.running:
                    img = _stopped(img, "EN VIVO - INSPECCION DETENIDA")
            elif S.engine.running:
                img = S.engine.last_image
                if img is None:
                    img = S.engine.live_view()
            else:
                img = S.engine.live_view()
                if img is not None:
                    img = _stopped(img, "INSPECCION DETENIDA")
            if img is None:
                img = _placeholder(S.engine.camera.error or "Esperando imagen...")
            if img is not None:
                data = await loop.run_in_executor(None, _jpeg, img)
                if data:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if auth.read_token(websocket.cookies.get(auth.COOKIE_NAME, "")) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    S.websockets.add(websocket)
    try:
        if S.engine.last_result:
            await websocket.send_json({"type": "result", "data": S.engine.last_result})
        await websocket.send_json({"type": "safety", "data": S.safety.state})
        await websocket.send_json({"type": "modules", "data": _modules()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        S.websockets.discard(websocket)
