"""Torretas ANDON: detección de luces verde, amarilla y roja encendidas.

No usa red neuronal: una lámpara encendida es una mancha BRILLANTE y SATURADA de su color.
Esto es más confiable que un modelo para luces fijas, no pesa nada y casi no usa CPU.
En cada torreta (ROI) se buscan manchas por color (HSV):
    rojo     H 0-10 y 160-179
    amarillo H 11-37  (incluye ámbar / naranja)
    verde    H 38-95
y se cuentan las lámparas encendidas de cada color.

Una luz intermitente se mantiene "encendida" entre destellos (hold_ms) y se marca
como intermitente. Cada encendido suma 1 al contador de activaciones de ese color.

Herramienta (torreta): {id, name, enabled, camera, roi, v_min, s_min, min_area}
"""
from collections import deque

import cv2
import numpy as np

from .module import LEVEL_COLORS, NEUTRAL, Module, banner, clip_roi, put_label, worst

COLORS = ("red", "yellow", "green")
COLOR_LABEL = {"red": "ROJO", "yellow": "AMARILLO", "green": "VERDE"}
LETTER = {"red": "R", "yellow": "A", "green": "V"}
DRAW = {"red": (40, 40, 255), "yellow": (0, 210, 255), "green": (60, 220, 60)}
HUES = {"red": ((0, 10), (160, 179)), "yellow": ((11, 37),), "green": ((38, 95),)}
BLINK_WINDOW_S = 4.0
ALL = "_all"


def lit_blobs(img, roi, v_min, s_min, min_area):
    """{color: [rects]} de las manchas brillantes y saturadas de cada color dentro de la ROI."""
    x, y, w, h = roi
    hsv = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    hch, sch, vch = cv2.split(hsv)
    bright = ((vch >= v_min) & (sch >= s_min)).astype(np.uint8)
    out = {}
    kernel = np.ones((3, 3), np.uint8)
    for color, ranges in HUES.items():
        hue = np.zeros_like(bright)
        for lo, hi in ranges:
            hue |= ((hch >= lo) & (hch <= hi)).astype(np.uint8)
        mask = cv2.morphologyEx(bright & hue, cv2.MORPH_OPEN, kernel) * 255
        mask = cv2.dilate(mask, kernel, iterations=2)  # une el halo con el centro de la lámpara
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rects = []
        for cnt in contours:
            if cv2.contourArea(cnt) >= min_area:
                bx, by, bw, bh = cv2.boundingRect(cnt)
                rects.append([bx + x, by + y, bw, bh])
        out[color] = rects
    return out


class AndonModule(Module):
    name = "andon"
    title = "ANDON"
    opc_folder = "Andon"
    opc_tools_folder = "Towers"
    opc_summary = [
        ("RedOn", "UInt32", lambda s: s["summary"].get("red", 0)),
        ("YellowOn", "UInt32", lambda s: s["summary"].get("yellow", 0)),
        ("GreenOn", "UInt32", lambda s: s["summary"].get("green", 0)),
        ("LitCount", "UInt32", lambda s: s["summary"].get("lit", 0)),
    ]
    opc_tool_fields = [
        ("Red", "Boolean", lambda t: t["lit"]["red"]),
        ("Yellow", "Boolean", lambda t: t["lit"]["yellow"]),
        ("Green", "Boolean", lambda t: t["lit"]["green"]),
        ("Blinking", "Boolean", lambda t: any(t["blink"].values())),
        ("LitCount", "UInt32", lambda t: sum(t["lamps"].values())),
        ("State", "String", lambda t: t["text"]),
        ("RedCount", "UInt32", lambda t: t["activations"]["red"]),
        ("YellowCount", "UInt32", lambda t: t["activations"]["yellow"]),
        ("GreenCount", "UInt32", lambda t: t["activations"]["green"]),
    ]
    tcp_prefix = "AND"

    def __init__(self, hub):
        self._last_on = {}  # (cam, torreta, color) -> última vez que se vio encendida
        self._lit = {}  # (cam, torreta, color) -> estado mostrado (con retardo)
        self._lamps = {}  # (cam, torreta, color) -> lámparas contadas la última vez que estuvo encendida
        self._edges = {}  # (cam, torreta, color) -> tiempos de encendido (detección de intermitencia)
        self._raw = {}
        super().__init__(hub)

    def reset(self):
        for d in (self._last_on, self._lit, self._lamps, self._edges, self._raw):
            d.clear()

    def empty_summary(self):
        return {"red": 0, "yellow": 0, "green": 0, "lit": 0}

    def process(self, cid, fid, img, tools, recipe, now):
        c = self.cfg
        hold = float(c.get("hold_ms", 1500)) / 1000
        levels = {"red": "peligro", "yellow": "advertencia", "green": "ninguna", "off": "ninguna",
                  **c.get("levels", {})}
        H, W = img.shape[:2]
        towers = [t for t in tools if t.get("roi")] or [
            {"id": ALL, "name": "Toda la imagen", "roi": [0, 0, W, H], "min_area": 150}]
        states, texts = [], []
        for t in towers:
            roi = clip_roi(img, t["roi"])
            blobs = lit_blobs(img, roi, int(t.get("v_min", 180)), int(t.get("s_min", 80)),
                              float(t.get("min_area", 40)))
            lit, lamps, blink = {}, {}, {}
            acts = self.counters.setdefault(f"{cid}/{t['id']}", {k: 0 for k in COLORS})
            for color in COLORS:
                k = (cid, t["id"], color)
                raw = bool(blobs[color])
                edges = self._edges.setdefault(k, deque(maxlen=8))
                if raw and not self._raw.get(k):  # se encendió
                    # un destello de una luz intermitente no es una activación nueva
                    if now - self._last_on.get(k, -1e9) > hold:
                        acts[color] += 1
                    edges.append(now)
                self._raw[k] = raw
                if raw:
                    self._last_on[k] = now
                    self._lamps[k] = len(blobs[color])
                blinking = sum(1 for e in edges if now - e <= BLINK_WINDOW_S) >= 2
                # Entre destellos, una luz intermitente se sigue reportando encendida;
                # una luz fija que se apaga se reporta apagada de inmediato.
                on = raw or (blinking and now - self._last_on.get(k, -1e9) < hold)
                self._lit[k] = on
                lit[color] = on
                lamps[color] = self._lamps.get(k, 0) if on else 0
                blink[color] = on and blinking
            on_colors = [col for col in COLORS if lit[col]]
            tlevel = worst(levels.get(col, "ninguna") for col in on_colors) if on_colors else levels.get("off", "ninguna")
            label = " + ".join(COLOR_LABEL[col] + (" (intermitente)" if blink[col] else "") for col in on_colors) \
                or "APAGADA"
            if tlevel != "ninguna":
                where = "" if t["id"] == ALL else f"{t['name']}: "
                texts.append(f"{where}{label}")
            states.append({"id": t["id"], "name": t["name"], "camera": cid, "level": tlevel, "lit": lit,
                           "lamps": lamps, "blink": blink, "activations": dict(acts), "text": label,
                           "blobs": blobs, "roi": list(roi)})
        summary = {col: sum(s["lamps"][col] for s in states) for col in COLORS}
        summary["lit"] = sum(summary.values())
        return {"tools": [{k: v for k, v in s.items() if k not in ("blobs", "roi")} for s in states],
                "summary": summary, "level": worst(s["level"] for s in states), "text": " · ".join(texts),
                "towers": states}

    def draw_tools(self, img, tools):
        for t in tools:
            if t.get("roi"):
                x, y, w, h = [int(v) for v in t["roi"]]
                cv2.rectangle(img, (x, y), (x + w, y + h), NEUTRAL, 2)
                put_label(img, t["name"], (x + 6, y + 24), NEUTRAL)

    def draw(self, img, res, tools):
        for t in res["towers"]:
            x, y, w, h = t["roi"]
            if t["id"] != ALL:
                cv2.rectangle(img, (x, y), (x + w, y + h), LEVEL_COLORS[t["level"]], 2)
                put_label(img, f"{t['name']}: {t['text']}", (x + 4, max(18, y - 8)), LEVEL_COLORS[t["level"]], 0.6)
            for color, rects in t["blobs"].items():
                for bx, by, bw, bh in rects:
                    cv2.rectangle(img, (bx - 3, by - 3), (bx + bw + 3, by + bh + 3), DRAW[color], 2)
        s = res["summary"]
        banner(img, f"EN VIVO   encendidas: {s['lit']}   rojo: {s['red']}   amarillo: {s['yellow']}"
                    f"   verde: {s['green']}", LEVEL_COLORS[res["level"]])

    def tcp_fields(self, s):
        m = s["summary"]
        towers = ["".join(LETTER[col] for col in COLORS if t["lit"][col]) or "-" for t in s["tools"] if t["id"] != ALL]
        return [str(m.get("lit", 0)), str(m.get("red", 0)), str(m.get("yellow", 0)), str(m.get("green", 0))] + towers
