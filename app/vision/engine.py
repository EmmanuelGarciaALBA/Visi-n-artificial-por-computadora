"""Motor de inspección: toma el frame, ejecuta las herramientas de la receta activa y publica el resultado."""
import logging
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np

from ..camera import Camera
from ..config import active_recipe, find_recipe, save_config
from .tools import TOOLS, clip_roi

log = logging.getLogger("engine")

GREEN, RED, YELLOW, BLUE = (60, 200, 60), (50, 50, 230), (0, 210, 255), (230, 160, 40)


def put_label(img, text, org, color, scale=0.7):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)


class VisionEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.camera = Camera(cfg["camera"])
        self.counter = 0
        self.last_result = None
        self.last_image = None  # imagen anotada de la última inspección (BGR)
        self._listeners = []
        self._recipe_listeners = []
        self._inspect_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="vision", daemon=True)
        self._thread.start()

    # ---------- suscripción a resultados ----------
    def add_listener(self, fn):
        """fn(result: dict) se llama desde el hilo de visión en cada inspección."""
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def add_recipe_listener(self, fn):
        """fn(recipe: dict) se llama cuando cambia la receta activa."""
        self._recipe_listeners.append(fn)

    # ---------- configuración ----------
    def apply_config(self, cfg):
        cam_changed = cfg["camera"] != self.cfg["camera"]
        self.cfg = cfg
        if cam_changed:
            self.camera.stop()
            self.camera = Camera(cfg["camera"])

    @property
    def running(self):
        """Módulo de inspección en ejecución (botón Ejecutar/Detener)."""
        return bool(self.cfg.get("inspection", {}).get("running"))

    def set_running(self, running):
        self.cfg.setdefault("inspection", {})["running"] = bool(running)
        if not running:
            self.last_image = None
        save_config(self.cfg)
        log.info("Inspección %s", "EN EJECUCIÓN" if running else "DETENIDA")

    @property
    def recipe(self):
        return active_recipe(self.cfg)

    @property
    def tools(self):
        return self.recipe.get("tools", [])

    def select_recipe(self, key):
        """Cambia la receta activa por número, id o nombre. Devuelve la receta o None."""
        r = find_recipe(self.cfg, key)
        if r is None:
            return None
        if r["id"] != self.cfg.get("active_recipe"):
            with self._inspect_lock:  # no cambiar a mitad de una inspección
                self.cfg["active_recipe"] = r["id"]
            save_config(self.cfg)
            log.info("Receta activa: %s - %s", r["number"], r["name"])
            for fn in list(self._recipe_listeners):
                try:
                    fn(r)
                except Exception:
                    log.exception("Error notificando cambio de receta")
        return r

    # ---------- ciclo ----------
    def _loop(self):
        while not self._stop.is_set():
            trig = self.cfg["trigger"]
            if self.running and trig.get("mode") == "continuous":
                t0 = time.time()
                self.inspect(source="auto")
                wait = max(0.0, trig.get("interval_ms", 500) / 1000 - (time.time() - t0))
                self._stop.wait(wait)
            else:
                self._stop.wait(0.1)

    def inspect(self, source="manual"):
        """Ejecuta una inspección completa y devuelve el resultado (thread-safe).
        Devuelve None si el módulo de inspección está detenido."""
        if not self.running:
            return None
        with self._inspect_lock:
            t0 = time.perf_counter()
            _, img = self.camera.latest()
            recipe = self.recipe
            result = {
                "counter": self.counter + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "recipe": {"id": recipe["id"], "number": recipe["number"], "name": recipe["name"]},
                "pass": False,
                "cycle_ms": 0.0,
                "tools": [],
                "error": None,
            }
            if img is None:
                result["error"] = self.camera.error or "Sin imagen"
                self.last_image = None  # no mostrar una imagen vieja como si fuera actual
            else:
                annotated = img.copy()
                all_ok = True
                for t in recipe.get("tools", []):
                    if not t.get("enabled", True):
                        continue
                    tr = {"id": t["id"], "name": t["name"], "type": t["type"], "value": 0.0,
                          "text": "", "pass": False, "error": None}
                    roi = clip_roi(img, t.get("roi", [0, 0, img.shape[1], img.shape[0]]))
                    overlays = []
                    try:
                        out = TOOLS[t["type"]](img, roi, t.get("params", {}))
                        value, overlays = out[0], out[1]
                        extra = out[2] if len(out) > 2 else {}
                        tr["value"] = round(float(value), 3)
                        tr["text"] = extra.get("text", "")
                        in_range = float(t.get("min", float("-inf"))) <= value <= float(t.get("max", float("inf")))
                        tr["pass"] = in_range and extra.get("ok", True)
                    except Exception as e:
                        tr["error"] = str(e)
                        log.exception("Error en herramienta %s", t["name"])
                    all_ok &= tr["pass"]
                    result["tools"].append(tr)
                    self._draw_tool(annotated, roi, tr, overlays)
                result["pass"] = all_ok and bool(result["tools"])
                result["cycle_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                self._draw_banner(annotated, result)
                self.last_image = annotated
            if img is None:
                result["cycle_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            self.counter += 1
            self.last_result = result

        for fn in list(self._listeners):
            try:
                fn(result)
            except Exception:
                log.exception("Error notificando resultado")
        return result

    # ---------- dibujo ----------
    @staticmethod
    def _draw_tool(img, roi, tr, overlays):
        x, y, w, h = roi
        color = GREEN if tr["pass"] else RED
        for o in overlays:
            if o["type"] == "box":
                bx, by, bw, bh = o["rect"]
                cv2.rectangle(img, (bx, by), (bx + bw, by + bh), YELLOW, 2)
                if o.get("label"):
                    put_label(img, o["label"], (bx, max(12, by - 6)), YELLOW, 0.55)
            elif o["type"] == "contour":
                pts = np.array(o["pts"], np.int32)
                cv2.polylines(img, [pts], True, YELLOW, 2)
                if o.get("label"):
                    px, py = pts.min(axis=0)
                    put_label(img, o["label"], (int(px), max(12, int(py) - 6)), YELLOW, 0.55)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        label = f"{tr['name']}: {tr['value']:g}" if not tr["error"] else f"{tr['name']}: ERROR"
        put_label(img, label, (x + 6, y + 24), color)

    @staticmethod
    def _draw_banner(img, result):
        rec = result["recipe"]
        txt = (f"#{result['counter']}  {'OK' if result['pass'] else 'NOK'}  {result.get('cycle_ms', 0):.0f} ms"
               f"   Receta {rec['number']}: {rec['name']}")
        cv2.rectangle(img, (0, img.shape[0] - 36), (img.shape[1], img.shape[0]), (20, 20, 20), -1)
        cv2.putText(img, txt, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    GREEN if result["pass"] else RED, 2)

    def live_view(self):
        """Frame en vivo con las ROIs. Si la inspección corre, cada ROI se pinta verde/roja
        con el valor de la última inspección; si no, azul."""
        _, img = self.camera.latest()
        if img is None:
            return None
        last = {}
        if self.running and self.last_result and self.last_result["recipe"]["id"] == self.recipe["id"]:
            last = {t["id"]: t for t in self.last_result["tools"]}
        for t in self.tools:
            if not t.get("enabled", True):
                continue
            x, y, w, h = clip_roi(img, t.get("roi", [0, 0, img.shape[1], img.shape[0]]))
            tr = last.get(t["id"])
            if tr is None:
                color, label = BLUE, t["name"]
            else:
                color = GREEN if tr["pass"] else RED
                shown = tr["text"] if tr.get("text") else f"{tr['value']:g}"
                label = f"{t['name']}: {'ERROR' if tr['error'] else shown}"[:60]
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
            put_label(img, label, (x + 6, y + 24), color)
        if self.running and self.last_result and not self.last_result.get("error"):
            r = self.last_result
            cv2.rectangle(img, (0, img.shape[0] - 36), (img.shape[1], img.shape[0]), (20, 20, 20), -1)
            cv2.putText(img, f"EN VIVO   ultimo: #{r['counter']} {'OK' if r['pass'] else 'NOK'}",
                        (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        GREEN if r["pass"] else RED, 2)
        return img

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)
        self.camera.stop()
