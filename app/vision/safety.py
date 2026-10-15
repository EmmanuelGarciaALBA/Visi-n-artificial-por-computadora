"""Zonas de seguridad: detecta personas (YOLO) y avisa si entran en zonas poligonales.

Herramienta (zona): {id, name, enabled, camera, level: peligro|advertencia, points: [[x, y], ...]}

IMPORTANTE: esto es una función de ASISTENCIA/monitoreo. No es un sistema de seguridad
certificado (ISO 13849 / IEC 62061) y no sustituye cortinas de luz, escáneres láser de
seguridad ni relevadores de seguridad para detener máquinas.
"""
import cv2
import numpy as np

from . import yolo
from .module import LEVEL_COLORS, NEUTRAL, Module, banner, put_label, worst

FREE = LEVEL_COLORS["ninguna"]
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


class SafetyModule(Module):
    name = "safety"
    title = "Zonas de seguridad"
    fail_level = "peligro"  # si el monitor falla, el PLC debe tratarlo como condición insegura
    opc_folder = "Safety"
    opc_tools_folder = "Zones"
    opc_summary = [
        ("Danger", "Boolean", lambda s: s["alarm"]),
        ("PersonCount", "UInt32", lambda s: s["summary"].get("persons", 0)),
        ("OccupiedZones", "UInt32", lambda s: s["summary"].get("occupied", 0)),
    ]
    opc_tool_fields = [
        ("Occupied", "Boolean", lambda t: t["occupied"]),
        ("Count", "UInt32", lambda t: t["count"]),
    ]
    tcp_prefix = "SAF"

    def __init__(self, hub):
        self._last_seen = {}  # zona -> time.time() de la última detección
        super().__init__(hub)

    def reset(self):
        self._last_seen.clear()

    def empty_summary(self):
        return {"persons": 0, "occupied": 0}

    def process(self, cid, fid, img, tools, recipe, now):
        c = self.cfg
        persons = yolo.detect(img, c.get("model") or yolo.DEFAULT_MODEL, c.get("conf", 0.5),
                              [yolo.PERSON], key=(cid, fid))
        hold = float(c.get("hold_ms", 1000)) / 1000
        mode = c.get("mode", "pies")
        states = []
        for z in tools:
            if len(z.get("points", [])) < 3:
                continue
            count = sum(1 for p in persons if _inside(z["points"], p["box"], mode))
            if count:
                self._last_seen[z["id"]] = now
            occupied = count > 0 or (now - self._last_seen.get(z["id"], 0)) < hold
            zlevel = z.get("level", "peligro")
            states.append({"id": z["id"], "name": z["name"], "camera": cid, "zone_level": zlevel,
                           "level": zlevel if occupied else "ninguna", "occupied": occupied, "count": count,
                           "text": f"OCUPADA ({count})" if occupied else "libre"})
        level = worst(z["level"] for z in states)
        names = [z["name"] for z in states if z["level"] == level and z["occupied"]]
        text = ""
        if level == "peligro":
            text = f"Persona en zona de peligro: {', '.join(names)}"
        elif level == "advertencia":
            text = f"Persona en zona de advertencia: {', '.join(names)}"
        return {"tools": states, "level": level, "text": text, "persons": persons,
                "summary": {"persons": len(persons), "occupied": sum(1 for z in states if z["occupied"])}}

    def _draw_zones(self, img, tools, states=None):
        states = {z["id"]: z for z in (states or [])}
        overlay = img.copy()
        for z in tools:
            if len(z.get("points", [])) < 3:
                continue
            pts = np.array(z["points"], np.int32)
            st = states.get(z["id"])
            color = NEUTRAL if st is None else LEVEL_COLORS[z.get("level", "peligro")] if st["occupied"] else FREE
            cv2.fillPoly(overlay, [pts], color)
            cv2.polylines(img, [pts], True, color, 3)
        cv2.addWeighted(overlay, 0.25, img, 0.75, 0, img)
        for z in tools:
            if len(z.get("points", [])) < 3:
                continue
            cx, cy = np.array(z["points"], np.int32).min(axis=0)
            st = states.get(z["id"])
            txt = z["name"] if st is None else f"{z['name']}: {'OCUPADA' if st['occupied'] else 'libre'}"
            put_label(img, txt, (int(cx) + 6, int(cy) + 26), (255, 255, 255))

    def draw_tools(self, img, tools):
        self._draw_zones(img, tools)

    def draw(self, img, res, tools):
        self._draw_zones(img, tools, res["tools"])
        for p in res["persons"]:
            x, y, w, h = p["box"]
            cv2.rectangle(img, (x, y), (x + w, y + h), PERSON_COLOR, 2)
            cv2.circle(img, (x + w // 2, y + h), 6, PERSON_COLOR, -1)
            put_label(img, f"persona {p['conf']:.2f}", (x, max(14, y - 6)), PERSON_COLOR, 0.55)
        lv = res["level"]
        txt = "PELIGRO" if lv == "peligro" else "ADVERTENCIA" if lv == "advertencia" else "ZONAS LIBRES"
        banner(img, f"EN VIVO   {txt}   personas: {len(res['persons'])}", LEVEL_COLORS[lv])

    def tcp_fields(self, s):
        warn = any(z["occupied"] and z["zone_level"] == "advertencia" for z in s["tools"])
        return ["1" if s["alarm"] else "0", "1" if warn else "0", str(s["summary"].get("persons", 0))] + \
               ["1" if z["occupied"] else "0" for z in s["tools"]]
