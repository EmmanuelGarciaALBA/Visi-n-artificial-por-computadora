"""VisionServer: 4 bloques de visión (zonas de seguridad, EPP, ANDON, stock) con interfaz web,
alarmas (sonido y correo) y comunicación OPC UA y TCP/IP.

Se abre desde cualquier PC de la misma red en  http://<IP-del-servidor>:<puerto>
"""
import asyncio
import copy
import logging
import socket
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, notify
from .comms.opcua_server import OpcUaServer
from .comms.tcp_server import TcpServer
from .config import MODULE_NAMES, load_config, save_config
from .hub import Hub
from .vision import yolo

log = logging.getLogger("main")
WEB_DIR = Path(__file__).resolve().parent / "web"


class State:
    hub: Hub = None
    opcua: OpcUaServer = None
    tcp: TcpServer = None
    websockets: set = set()
    loop: asyncio.AbstractEventLoop = None


S = State()


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


def web_urls():
    return [f"http://{ip}:{S.hub.cfg['server']['port']}" for ip in lan_ips()]


async def start_comms():
    cfg = S.hub.cfg
    if cfg["opcua"]["enabled"]:
        S.opcua = OpcUaServer(S.hub)
        try:
            await S.opcua.start()
        except Exception as e:
            log.exception("No se pudo iniciar OPC UA")
            S.opcua.error = str(e)
    if cfg["tcp"]["enabled"]:
        S.tcp = TcpServer(S.hub)
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


async def _ws_broadcast(msg):
    for ws in list(S.websockets):
        try:
            await ws.send_json(msg)
        except Exception:
            S.websockets.discard(ws)


def _from_thread(msg):
    if S.loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast(msg), S.loop)


def _state_listener(module, state):
    _from_thread({"type": "module", "name": module.name, "data": state})


def _event_listener(ev):
    _from_thread({"type": "event", "data": ev})


def _recipe_listener(module, recipe):
    """Cambio de receta (web, PLC o TCP): reconstruir nodos OPC UA y avisar a los navegadores."""
    if S.loop:
        asyncio.run_coroutine_threadsafe(_on_recipe_changed(module, recipe), S.loop)


async def _on_recipe_changed(module, recipe):
    if S.opcua:
        await S.opcua.rebuild_tools()
    await _ws_broadcast({"type": "recipe", "name": module.name,
                         "data": {k: recipe[k] for k in ("id", "number", "name")}})


def _signature(cfg):
    """Lo que define los nodos OPC UA dinámicos: cámaras y herramientas de la receta activa."""
    from .config import active_recipe
    return ([(c["id"], c["name"]) for c in cfg["cameras"]],
            [(n, active_recipe(cfg["modules"][n])["id"],
              [(t["id"], t["name"]) for t in active_recipe(cfg["modules"][n]).get("tools", [])])
             for n in MODULE_NAMES])


@asynccontextmanager
async def lifespan(app):
    S.loop = asyncio.get_running_loop()
    S.hub = Hub(load_config(), web_urls)
    for m in S.hub.modules.values():
        m.add_listener(_state_listener)
    S.hub.alarms.add_listener(_event_listener)
    S.hub.add_recipe_listener(_recipe_listener)
    await start_comms()
    log.info("Interfaz web disponible en: %s", ", ".join(web_urls()))
    yield
    await stop_comms()
    await asyncio.get_running_loop().run_in_executor(None, S.hub.stop)


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


def _module_or_404(name):
    m = S.hub.module(name)
    if m is None:
        return None, JSONResponse({"error": f"Bloque desconocido: {name}"}, status_code=404)
    return m, None


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


# ------------------------------------------------------------------ páginas
def _ui_version():
    """Cambia cada vez que se modifica un archivo de la interfaz (evita cachés viejas)."""
    return str(int(max(p.stat().st_mtime for p in WEB_DIR.iterdir())))


@app.get("/")
async def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", _ui_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ estado y configuración
@app.get("/api/status")
async def status():
    cfg = S.hub.cfg
    return {
        "cameras": S.hub.cameras.status(),
        "opcua": {"enabled": cfg["opcua"]["enabled"], "running": bool(S.opcua and S.opcua.running),
                  "error": getattr(S.opcua, "error", None),
                  "endpoints": [f"opc.tcp://{ip}:{cfg['opcua']['port']}/visionserver/" for ip in lan_ips()]},
        "tcp": {"enabled": cfg["tcp"]["enabled"], "running": bool(S.tcp and S.tcp.running),
                "error": getattr(S.tcp, "error", None), "port": cfg["tcp"]["port"],
                "clients": len(S.tcp.clients) if S.tcp else 0},
        "web": web_urls(),
        "email_configured": notify.is_configured(cfg["email"]),
        "modules": {n: m.state for n, m in S.hub.modules.items()},
    }


@app.get("/api/config")
async def get_config():
    cfg = copy.deepcopy(S.hub.cfg)
    for c in cfg["cameras"]:
        if c.get("password"):
            c["password"] = MASK
    if cfg["email"].get("password"):
        cfg["email"]["password"] = MASK
    return cfg


@app.put("/api/config")
async def put_config(new_cfg: dict, request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    old = S.hub.cfg
    cfg = copy.deepcopy(new_cfg)
    old_cams = {c["id"]: c for c in old["cameras"]}
    for c in cfg["cameras"]:
        if c.get("password") == MASK:  # sin cambios: conservar la guardada
            c["password"] = old_cams.get(c["id"], {}).get("password", "")
    if cfg["email"].get("password") == MASK:
        cfg["email"]["password"] = old["email"].get("password", "")
    # Ejecutar/Detener y el sonido se cambian con sus botones, nunca al guardar configuración
    for n in MODULE_NAMES:
        cfg["modules"][n]["running"] = bool(old["modules"][n].get("running"))
        cfg["modules"][n].setdefault("alarm", {})["sound"] = bool(old["modules"][n]["alarm"].get("sound", True))
    save_config(cfg)
    await asyncio.get_running_loop().run_in_executor(None, S.hub.apply_config, cfg)
    restart_needed = cfg["server"] != old["server"]
    if cfg["opcua"] != old["opcua"] or cfg["tcp"] != old["tcp"]:
        await stop_comms()
        await start_comms()
    elif S.opcua and _signature(cfg) != _signature(old):
        await S.opcua.rebuild_tools()
    return {"ok": True, "restart_required": restart_needed}


@app.get("/api/models")
async def models():
    return yolo.available_models()


# ------------------------------------------------------------------ operación de los bloques
# Ejecutar/Detener, sonido, receta y contadores los puede usar cualquier usuario (operador incluido)
@app.post("/api/modules/{name}/run")
async def set_running(name: str, data: dict):
    m, err = _module_or_404(name)
    if err:
        return err
    await asyncio.get_running_loop().run_in_executor(None, m.set_running, bool(data.get("running")))
    mods = {n: x.running for n, x in S.hub.modules.items()}
    await _ws_broadcast({"type": "modules", "data": mods})
    return mods


@app.post("/api/modules/{name}/sound")
async def set_sound(name: str, data: dict):
    m, err = _module_or_404(name)
    if err:
        return err
    m.set_sound(bool(data.get("on")))
    return {"sound": m.cfg["alarm"]["sound"]}


@app.post("/api/modules/{name}/recipe")
async def select_recipe(name: str, data: dict):
    m, err = _module_or_404(name)
    if err:
        return err
    r = await asyncio.get_running_loop().run_in_executor(None, m.select_recipe, data.get("key"))
    if r is None:
        return JSONResponse({"error": "La receta no existe"}, status_code=404)
    return {"id": r["id"], "number": r["number"], "name": r["name"]}


@app.post("/api/modules/{name}/reset")
async def reset_counters(name: str):
    m, err = _module_or_404(name)
    if err:
        return err
    m.reset_counters()
    return {"ok": True}


TCP_COMMANDS = {"safety": "SAFETY", "ppe": "EPP", "andon": "ANDON", "stock": "STOCK"}


@app.get("/api/modules/{name}/comms")
async def module_comms(name: str):
    """Nodos OPC UA y respuesta TCP del bloque, con sus valores actuales (pestaña Comunicación)."""
    from .comms.opcua_server import LEVEL_NUM, _unique_names
    m, err = _module_or_404(name)
    if err:
        return err
    s, b, rec = m.state, m.opc_folder, m.recipe
    rows = [(f"{b}.Enabled", "Bool", s["running"]), (f"{b}.Alarm", "Bool", s["alarm"]),
            (f"{b}.Warning", "Bool", s["warning"]), (f"{b}.Level", "Int32", LEVEL_NUM.get(s["level"], 0)),
            (f"{b}.AlarmText", "String", s.get("text", "")), (f"{b}.Error", "String", s.get("error") or ""),
            (f"{b}.RecipeNumber", "Int32 RW", rec["number"]), (f"{b}.ActiveRecipeName", "String", rec["name"]),
            (f"{b}.ResetCounters", "Bool RW", False)]
    rows += [(f"{b}.{n}", vt.replace("Boolean", "Bool"), fn(s)) for n, vt, fn in m.opc_summary]
    by_id = {t["id"]: t for t in s["tools"]}
    for t, tname in _unique_names(rec.get("tools", [])):
        st = by_id.get(t["id"])
        for n, vt, fn in m.opc_tool_fields:
            rows.append((f"{b}.{m.opc_tools_folder}.{tname}.{n}", vt.replace("Boolean", "Bool"),
                         fn(st) if st else None))
    tcp = S.tcp or TcpServer(S.hub)
    return {"namespace": S.hub.cfg["opcua"]["namespace"], "opc_enabled": S.hub.cfg["opcua"]["enabled"],
            "nodes": [{"path": f"VisionServer.{p}", "type": t, "value": v} for p, t, v in rows],
            "tcp_enabled": S.hub.cfg["tcp"]["enabled"], "tcp_port": S.hub.cfg["tcp"]["port"],
            "tcp_command": TCP_COMMANDS[m.name], "tcp_response": tcp.format_state(m)}


@app.get("/api/events")
async def events(module: str = "", limit: int = 100):
    return S.hub.alarms.list(module or None, max(1, min(limit, 400)))


@app.post("/api/email/test")
async def email_test(data: dict, request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    m = S.hub.module(data.get("module", "")) if data.get("module") else None
    try:
        to = await asyncio.get_running_loop().run_in_executor(None, S.hub.alarms.test_email, m, data.get("to"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "to": to}


# ------------------------------------------------------------------ video
def _placeholder(msg):
    """Cuadro negro con el motivo por el que no hay imagen."""
    img = np.zeros((360, 640, 3), np.uint8)
    cv2.putText(img, "SIN IMAGEN DE LA CAMARA", (40, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 230), 2)
    from .vision.module import ascii_text
    for i, line in enumerate(textwrap.wrap(ascii_text(msg or ""), 55)[:4]):
        cv2.putText(img, line, (40, 200 + 28 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    return img


def _jpeg(img, quality=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


@app.get("/api/snapshot.jpg")
async def snapshot(cam: str = "cam1"):
    _, img = S.hub.cameras.latest(cam)
    if img is None:
        return JSONResponse({"error": "sin imagen"}, status_code=503)
    return Response(_jpeg(img, 90), media_type="image/jpeg")


@app.get("/stream.mjpg")
async def stream(cam: str = "cam1", view: str = "raw"):
    """Video MJPEG de una cámara. view=raw (sin anotaciones, para dibujar herramientas)
    | safety | ppe | andon | stock (anotado por ese bloque)."""
    module = S.hub.modules.get(view)

    async def gen():
        loop = asyncio.get_running_loop()
        while True:
            _, img = S.hub.cameras.latest(cam)
            if img is None:
                img = _placeholder(S.hub.cameras.error(cam) or "Esperando imagen...")
            elif module is not None:
                img = module.annotate(cam, img)
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
        for n, m in S.hub.modules.items():
            await websocket.send_json({"type": "module", "name": n, "data": m.state})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        S.websockets.discard(websocket)
