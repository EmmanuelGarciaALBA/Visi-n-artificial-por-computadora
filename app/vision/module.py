"""Base común de los 4 bloques de inspección (zonas de seguridad, EPP, ANDON, stock).

Cada bloque:
  - corre en su propio hilo, a `fps` ciclos por segundo, solo mientras está en ejecución
  - analiza todas las cámaras que tienen asignada su función (pestaña Cámaras)
  - tiene recetas: cada receta es un conjunto de herramientas (zonas, áreas, torretas o
    posiciones), cada herramienta pertenece a una cámara
  - calcula un nivel de alarma: ninguna | advertencia | peligro
Las subclases implementan `process()` (análisis de un cuadro) y `draw()` (anotación del video).
"""
import logging
import threading
import time
import unicodedata
from datetime import datetime, timezone

import cv2

from ..config import active_recipe, find_recipe, save_config

log = logging.getLogger("module")

LEVEL_RANK = {"ninguna": 0, "advertencia": 1, "peligro": 2}
LEVEL_COLORS = {"ninguna": (60, 200, 60), "advertencia": (0, 165, 255), "peligro": (50, 50, 230)}
NEUTRAL = (230, 160, 40)  # azul: herramienta sin evaluar
GRAY = (150, 150, 150)


def worst(levels):
    best = "ninguna"
    for lv in levels:
        if LEVEL_RANK.get(lv, 0) > LEVEL_RANK[best]:
            best = lv
    return best


def ascii_text(text):
    """OpenCV solo dibuja ASCII: "Área ñ" -> "Area n"."""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()


def put_label(img, text, org, color, scale=0.7):
    text = ascii_text(text)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    # que el texto no se salga de la imagen (herramientas pegadas al borde)
    org = (int(max(2, min(org[0], img.shape[1] - tw - 4))), int(max(th + 4, org[1])))
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)


def banner(img, text, color):
    text = ascii_text(text)
    cv2.rectangle(img, (0, img.shape[0] - 36), (img.shape[1], img.shape[0]), (20, 20, 20), -1)
    cv2.putText(img, text, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)


def clip_roi(img, roi):
    H, W = img.shape[:2]
    x, y, w, h = [int(v) for v in roi]
    x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
    w, h = max(1, min(w, W - x)), max(1, min(h, H - y))
    return x, y, w, h


def in_rect(px, py, rect):
    x, y, w, h = rect
    return x <= px <= x + w and y <= py <= y + h


class Debounce:
    """Condición con retardo al activarse (`confirm_s`) y al liberarse (`hold_s`).
    Un parpadeo más corto que `hold_s` no reinicia ni libera la condición."""

    def __init__(self):
        self._since, self._last, self._on = {}, {}, {}

    def update(self, key, raw, now, confirm_s, hold_s):
        if raw:
            self._last[key] = now
            self._since.setdefault(key, now)
            if now - self._since[key] >= confirm_s:
                self._on[key] = True
        elif now - self._last.get(key, 0) >= hold_s:
            self._since.pop(key, None)
            self._on[key] = False
        return self._on.get(key, False)

    def clear(self):
        self._since.clear()
        self._last.clear()
        self._on.clear()


class Stable:
    """Valor que solo cambia cuando el nuevo se mantiene `confirm_s` segundos."""

    def __init__(self):
        self._val, self._cand = {}, {}

    def update(self, key, value, now, confirm_s):
        if key not in self._val:
            self._val[key] = value
        cur = self._val[key]
        if value == cur:
            self._cand.pop(key, None)
            return cur
        cand = self._cand.get(key)
        if cand is None or cand[0] != value:
            self._cand[key] = (value, now)
        elif now - cand[1] >= confirm_s:
            self._val[key] = value
            self._cand.pop(key, None)
            return value
        return cur

    def clear(self):
        self._val.clear()
        self._cand.clear()


class Module:
    name = ""
    title = ""
    fail_level = "advertencia"  # nivel cuando falla la cámara o el análisis
    # Comunicación. opc_summary: [(nombre, tipo OPC, función(state) -> valor)]
    opc_folder = ""
    opc_tools_folder = "Items"
    opc_summary = []
    opc_tool_fields = []  # [(nombre, tipo OPC, función(tool_state) -> valor)]
    tcp_prefix = ""

    def __init__(self, hub):
        self.hub = hub
        self._results = {}  # cámara -> último resultado de process() (para anotar el video)
        self._listeners = []
        self._last_step = None
        self._lock = threading.Lock()
        self.counters = {}
        self.state = self._empty_state()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)

    def start(self):
        self._thread.start()

    # ---------------------------------------------------------------- configuración
    @property
    def cfg(self):
        return self.hub.cfg["modules"][self.name]

    @property
    def running(self):
        return bool(self.cfg.get("running"))

    @property
    def recipe(self):
        return active_recipe(self.cfg)

    def cameras(self):
        return self.hub.cameras.with_function(self.name)

    def tools_for(self, cid, recipe=None):
        recipe = recipe or self.recipe
        return [t for t in recipe.get("tools", []) if t.get("enabled", True) and t.get("camera") == cid]

    def set_running(self, running):
        self.cfg["running"] = bool(running)
        save_config(self.hub.cfg)
        log.info("%s %s", self.title, "EN EJECUCIÓN" if running else "DETENIDO")

    def set_sound(self, on):
        self.cfg["alarm"]["sound"] = bool(on)
        save_config(self.hub.cfg)
        self.state = {**self.state, "sound": bool(on)}
        self._notify()

    def select_recipe(self, key):
        """Cambia la receta activa por número, id o nombre. Devuelve la receta o None."""
        r = find_recipe(self.cfg, key)
        if r is None:
            return None
        if r["id"] != self.cfg.get("active_recipe"):
            with self._lock:  # no cambiar a mitad de un análisis
                self.cfg["active_recipe"] = r["id"]
                self.reset()
            save_config(self.hub.cfg)
            log.info("%s: receta activa %s - %s", self.title, r["number"], r["name"])
            self.hub.on_recipe(self, r)
        return r

    def reset_counters(self):
        self.counters = {}

    def reset(self):
        """Olvida el estado temporal (retardos, historial). Lo llaman las subclases."""

    # ---------------------------------------------------------------- suscripción
    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self):
        for fn in list(self._listeners):
            try:
                fn(self, self.state)
            except Exception:
                log.exception("Error notificando estado de %s", self.name)

    # ---------------------------------------------------------------- ciclo
    def _recipe_info(self):
        r = self.recipe
        return {"id": r["id"], "number": r["number"], "name": r["name"]}

    def empty_summary(self):
        return {}

    def _empty_state(self):
        return {"module": self.name, "title": self.title, "running": False, "active": False,
                "level": "ninguna", "alarm": False, "warning": False, "text": "", "error": None,
                "summary": self.empty_summary(), "tools": [], "cameras": {}, "recipe": self._recipe_info(),
                "sound": bool(self.cfg["alarm"].get("sound", True)), "fps": 0.0, "timestamp": None,
                "counters": self.counters}

    def _loop(self):
        while not self._stop.is_set():
            if not self.running:
                if self.state["running"] or self.state["active"]:
                    self._results.clear()
                    with self._lock:
                        self.reset()
                    self._last_step = None
                    self.state = self._empty_state()
                    self._publish()
                self._stop.wait(0.5)
                continue
            t0 = time.time()
            try:
                self._step()
            except Exception as e:
                log.exception("Error en %s", self.title)
                self.state = {**self._empty_state(), "running": True, "active": True, "error": str(e),
                              "level": self.fail_level, "alarm": self.fail_level == "peligro",
                              "warning": self.fail_level == "advertencia",
                              "text": f"Falla del bloque {self.title}: {e}",
                              "timestamp": datetime.now(timezone.utc).isoformat()}
                self._publish()
                self._stop.wait(2)
                continue
            period = 1.0 / max(0.1, float(self.cfg.get("fps", 2)))
            self._stop.wait(max(0.0, period - (time.time() - t0)))

    def _step(self):
        cams = self.cameras()
        now = time.time()
        results = {}
        for cid in cams:
            fid, img = self.hub.cameras.latest(cid)
            if img is None:
                self._results.pop(cid, None)
                results[cid] = {"error": self.hub.cameras.error(cid) or "Sin imagen", "tools": [],
                                "summary": {}, "level": self.fail_level,
                                "text": f"Sin imagen de {self.hub.cameras.name(cid)}"}
                continue
            with self._lock:
                recipe = self.recipe
                res = self.process(cid, fid, img, self.tools_for(cid, recipe), recipe, now)
            res.setdefault("error", None)
            self._results[cid] = res
            results[cid] = res
        for cid in list(self._results):
            if cid not in cams:
                self._results.pop(cid, None)

        level = worst(r["level"] for r in results.values())
        multi = len(results) > 1
        texts = [f"{self.hub.cameras.name(cid)}: {r['text']}" if multi else r["text"]
                 for cid, r in results.items() if LEVEL_RANK[r["level"]] > 0 and r.get("text")]
        errors = [f"{self.hub.cameras.name(cid)}: {r['error']}" for cid, r in results.items() if r.get("error")]
        if not cams:
            errors = ["Ninguna cámara tiene asignada esta función (pestaña Cámaras)"]
        t = time.time()
        dt = t - self._last_step if self._last_step else t - now
        self._last_step = t
        self.state = {
            "module": self.name, "title": self.title, "running": True, "active": True,
            "level": level, "alarm": level == "peligro", "warning": level == "advertencia",
            "text": " · ".join(texts), "error": " · ".join(errors) or None,
            "summary": self.combine([r["summary"] for r in results.values() if not r.get("error")]),
            "tools": [tl for r in results.values() for tl in r.get("tools", [])],
            "cameras": {cid: {"name": self.hub.cameras.name(cid), "error": r.get("error"), "level": r["level"]}
                        for cid, r in results.items()},
            "recipe": self._recipe_info(), "sound": bool(self.cfg["alarm"].get("sound", True)),
            "fps": round(1 / dt, 1) if dt > 0 else 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(), "counters": self.counters,
        }
        self._publish()

    def _publish(self):
        try:
            self.hub.alarms.update(self, self.state)
        except Exception:
            log.exception("Error evaluando alarmas de %s", self.name)
        self._notify()

    def combine(self, summaries):
        """Suma los números y combina con OR los booleanos de las cámaras."""
        out = dict(self.empty_summary())
        for s in summaries:
            for k, v in s.items():
                if isinstance(v, bool):
                    out[k] = bool(out.get(k)) or v
                elif isinstance(v, (int, float)):
                    out[k] = out.get(k, 0) + v
        return out

    # ---------------------------------------------------------------- a implementar
    def process(self, cid, fid, img, tools, recipe, now):
        """Analiza un cuadro. Devuelve {"tools": [...], "summary": {...}, "level", "text", ...}."""
        raise NotImplementedError

    def draw(self, img, res, tools):
        """Dibuja la última detección `res` sobre `img` (in situ)."""

    def draw_tools(self, img, tools):
        """Herramientas sin resultado (módulo detenido o iniciando): solo su contorno."""

    def tcp_fields(self, state):
        return []

    # ---------------------------------------------------------------- video
    def annotate(self, cid, img):
        img = img.copy()
        tools = self.tools_for(cid)
        res = self._results.get(cid)
        if not self.running:
            self.draw_tools(img, tools)
            banner(img, f"EN VIVO - {self.title.upper()} DETENIDO", GRAY)
        elif cid not in self.cameras():
            banner(img, "ESTA FUNCION NO ESTA ASIGNADA A ESTA CAMARA", GRAY)
        elif res is None:
            self.draw_tools(img, tools)
            banner(img, "EN VIVO - INICIANDO...", GRAY)
        else:
            self.draw(img, res, tools)
        return img

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
