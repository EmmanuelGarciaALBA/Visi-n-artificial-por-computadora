"""Zonas de seguridad: detecta personas (YOLO) y avisa si entran en zonas poligonales.

Corre en su propio hilo, independiente del disparo de inspección, a `fps` cuadros por segundo.

IMPORTANTE: esto es una función de ASISTENCIA/monitoreo. No es un sistema de seguridad
certificado (ISO 13849 / IEC 62061) y no sustituye cortinas de luz, escáneres láser de
seguridad ni relevadores de seguridad para detener máquinas.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np

from . import yolo

log = logging.getLogger("safety")

LEVEL_COLORS = {"peligro": (50, 50, 230), "advertencia": (0, 165, 255)}
FREE = (60, 200, 60)
PERSON_COLOR = (255, 200, 0)


def _inside(zone_pts, box, mode):
    """¿La persona (box x,y,w,h) está dentro del polígono?"""
    x, y, w, h = box
    poly = np.array(zone_pts, np.float32)
    if mode == "caja":
        # Cualquier esquina, el centro o los pies dentro, o algún vértice de la zona dentro del recuadro
        pts = [(x, y), (x + w, y), (x, y + h), (x + w, y + h), (x + w / 2, y + h / 2), (x + w / 2, y + h)]
        if any(cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0 for px, py in pts):
            return True
        return any(x <= px <= x + w and y <= py <= y + h for px, py in zone_pts)
    # "pies": punto inferior central (la persona está parada dentro de la zona del piso)
    return cv2.pointPolygonTest(poly, (float(x + w / 2), float(y + h)), False) >= 0


class SafetyMonitor:
    def __init__(self, engine):
        self.engine = engine
        self.state = self._empty_state()
        self.last_image = None
        self._last_seen = {}  # zone_id -> time.time() de la última detección
        self._last_step = None  # para medir los cuadros por segundo reales
        self._last_persons = []
        self._last_zones = []
        self._listeners = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="safety", daemon=True)
        self._thread.start()

    @property
    def cfg(self):
        return self.engine.cfg["safety"]

    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _empty_state(self):
        return {"enabled": False, "running": False, "persons": 0, "danger": False, "warning": False,
                "zones": [], "error": None, "fps": 0.0, "timestamp": None}

    def _loop(self):
        while not self._stop.is_set():
            c = self.cfg
            if not c.get("enabled"):
                if self.state["enabled"] or self.state["running"]:
                    self.state = self._empty_state()
                    self._last_seen.clear()
                    self._last_step = None
                    self._last_persons, self._last_zones = [], []
                    self._notify()
                self._stop.wait(0.5)
                continue
            t0 = time.time()
            try:
                self._step(c)
            except Exception as e:
                log.exception("Error en monitor de seguridad")
                # Ante una falla se reporta peligro: el PLC debe tratarlo como condición insegura
                self.state = {**self._empty_state(), "enabled": True, "danger": True, "error": str(e),
                              "timestamp": datetime.now(timezone.utc).isoformat()}
                self._notify()
                self._stop.wait(2)
                continue
            period = 1.0 / max(0.5, float(c.get("fps", 5)))
            self._stop.wait(max(0.0, period - (time.time() - t0)))

    def _step(self, c):
        t0 = time.time()
        _, img = self.engine.camera.latest()
        if img is None:
            raise RuntimeError(self.engine.camera.error or "Sin imagen de la cámara")
        persons = yolo.detect(img, c.get("model") or yolo.DEFAULT_MODEL, c.get("conf", 0.5), [yolo.PERSON])
        now = time.time()
        hold = float(c.get("hold_ms", 1000)) / 1000
        mode = c.get("mode", "pies")

        zones_state, danger, warning = [], False, False
        for z in c.get("zones", []):
            if not z.get("enabled", True) or len(z.get("points", [])) < 3:
                continue
            count = sum(1 for p in persons if _inside(z["points"], p["box"], mode))
            if count:
                self._last_seen[z["id"]] = now
            occupied = count > 0 or (now - self._last_seen.get(z["id"], 0)) < hold
            level = z.get("level", "peligro")
            danger |= occupied and level == "peligro"
            warning |= occupied and level == "advertencia"
            zones_state.append({"id": z["id"], "name": z["name"], "level": level,
                                "occupied": occupied, "count": count})
        self._last_persons = persons
        self._last_zones = zones_state
        self.last_image = self.annotate(img, persons, zones_state, danger, warning)

        # fps real = tiempo entre ciclos (incluye la espera), no solo el tiempo de proceso
        now2 = time.time()
        dt = now2 - self._last_step if self._last_step else now2 - t0
        self._last_step = now2
        self.state = {"enabled": True, "running": True, "persons": len(persons), "danger": danger,
                      "warning": warning, "zones": zones_state, "error": None,
                      "fps": round(1 / dt, 1) if dt > 0 else 0.0,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
        self._notify()

    def annotate(self, img, persons=None, zones_state=None, danger=None, warning=None):
        """Dibuja zonas, personas y estado sobre `img`. Sin argumentos usa la última detección:
        así el video en vivo va a velocidad normal aunque YOLO corra a pocos cuadros por segundo."""
        persons = self._last_persons if persons is None else persons
        zones_state = self._last_zones if zones_state is None else zones_state
        danger = self.state["danger"] if danger is None else danger
        warning = self.state["warning"] if warning is None else warning
        points = {z["id"]: z["points"] for z in self.cfg.get("zones", [])}
        annotated = img.copy()
        overlay = img.copy()
        for z in zones_state:
            if z["id"] not in points:
                continue
            pts = np.array(points[z["id"]], np.int32)
            color = LEVEL_COLORS.get(z["level"], LEVEL_COLORS["peligro"]) if z["occupied"] else FREE
            cv2.fillPoly(overlay, [pts], color)
            cv2.polylines(annotated, [pts], True, color, 3)
        cv2.addWeighted(overlay, 0.25, annotated, 0.75, 0, annotated)
        for z in zones_state:
            if z["id"] not in points:
                continue
            cx, cy = np.array(points[z["id"]], np.int32).min(axis=0)
            txt = f"{z['name']}: {'OCUPADA' if z['occupied'] else 'libre'}"
            cv2.putText(annotated, txt, (int(cx) + 6, int(cy) + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
            cv2.putText(annotated, txt, (int(cx) + 6, int(cy) + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        for p in persons:
            x, y, w, h = p["box"]
            cv2.rectangle(annotated, (x, y), (x + w, y + h), PERSON_COLOR, 2)
            cv2.circle(annotated, (x + w // 2, y + h), 6, PERSON_COLOR, -1)
            cv2.putText(annotated, f"persona {p['conf']:.2f}", (x, max(14, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, PERSON_COLOR, 2)
        banner = "PELIGRO" if danger else "ADVERTENCIA" if warning else "ZONAS LIBRES"
        bcolor = LEVEL_COLORS["peligro"] if danger else LEVEL_COLORS["advertencia"] if warning else FREE
        cv2.rectangle(annotated, (0, img.shape[0] - 36), (img.shape[1], img.shape[0]), (20, 20, 20), -1)
        cv2.putText(annotated, f"EN VIVO   {banner}   personas: {len(persons)}", (10, img.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, bcolor, 2)
        return annotated

    def _notify(self):
        for fn in list(self._listeners):
            try:
                fn(self.state)
            except Exception:
                log.exception("Error notificando estado de seguridad")

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)
