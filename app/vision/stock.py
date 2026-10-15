"""Stock: cajas (cartón / plástico) en racks o estantes.

Modelo: yoloe-26s-seg.pt (YOLOE-26, Ultralytics) de vocabulario abierto: detecta lo que se le
describe en texto (en inglés). Clases por defecto, probadas con la cámara de la planta:
  cajas:  cardboard storage box, plastic storage box, plastic crate   (el tipo cartón / plástico es aproximado)
  racks:  shelving unit, storage rack
Se pueden cambiar en la interfaz (p. ej. "pallet", "plastic tote"); la primera vez que cambian
se calculan con el codificador de texto (models/mobileclip2_b.ts) y quedan guardadas en models/vocab.
La misma caja detectada con dos nombres se cuenta una vez.

Herramienta (posición): {id, name, enabled, camera, roi, min_boxes, max_boxes, require_rack}
Cada posición reporta cuántas cajas tiene y si hay rack. Su estado
(OK, BAJO, VACÍO, EXCESO, SIN RACK) solo cambia cuando el nuevo se mantiene `confirm_s`
segundos: una persona que pasa frente al rack no dispara la alarma.
"""
import statistics
from collections import deque

import cv2

from . import yolo
from .module import LEVEL_COLORS, NEUTRAL, Module, Stable, banner, put_label, worst

BOX_COLOR = (0, 200, 255)
RACK_COLOR = (255, 120, 60)
STATE_KEY = {"OK": None, "BAJO": "low", "VACÍO": "empty", "EXCESO": "over", "SIN RACK": "no_rack"}
ALL = "_all"
DEFAULT_BOXES = "cardboard storage box, plastic storage box, plastic crate"
DEFAULT_RACKS = "shelving unit, storage rack"


def _classes(text):
    return [c.strip() for c in str(text).split(",") if c.strip()]


def box_type(cls):
    c = cls.lower()
    if "plastic" in c or "crate" in c or "bin" in c or "container" in c or "tote" in c:
        return "plastico"
    if "cardboard" in c or "carton" in c:
        return "carton"
    return "caja"


def _iou(a, b):
    inter = _overlap(a, b)
    return inter / max(1, a[2] * a[3] + b[2] * b[3] - inter)


def _dedupe(dets, thr=0.6):
    """La misma caja detectada con dos nombres ("storage box" y "cardboard box") cuenta una vez."""
    kept = []
    for d in sorted(dets, key=lambda d: -d["conf"]):
        if all(_iou(d["box"], k["box"]) < thr for k in kept):
            kept.append(d)
    return kept


def _overlap(a, b):
    """Área de intersección de dos rectángulos x, y, w, h."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


class StockModule(Module):
    name = "stock"
    title = "Stock"
    opc_folder = "Stock"
    opc_tools_folder = "Positions"
    opc_summary = [
        ("BoxCount", "UInt32", lambda s: s["summary"].get("boxes", 0)),
        ("RackCount", "UInt32", lambda s: s["summary"].get("racks", 0)),
        ("PositionsOk", "UInt32", lambda s: s["summary"].get("pos_ok", 0)),
        ("PositionsEmpty", "UInt32", lambda s: s["summary"].get("pos_empty", 0)),
        ("PositionsLow", "UInt32", lambda s: s["summary"].get("pos_low", 0)),
        ("PositionsNoRack", "UInt32", lambda s: s["summary"].get("pos_no_rack", 0)),
    ]
    opc_tool_fields = [
        ("Boxes", "UInt32", lambda t: t["boxes"]),
        ("RackPresent", "Boolean", lambda t: t["rack"]),
        ("Ok", "Boolean", lambda t: t["status"] == "OK"),
        ("State", "String", lambda t: t["status"]),
    ]
    tcp_prefix = "STK"

    def __init__(self, hub):
        self._hist = {}  # (cam, posición) -> últimos conteos (mediana: filtra parpadeos del detector)
        self._stable = Stable()
        super().__init__(hub)

    def reset(self):
        self._hist.clear()
        self._stable.clear()

    def empty_summary(self):
        return {"boxes": 0, "racks": 0, "cardboard": 0, "plastic": 0, "pos_ok": 0, "pos_empty": 0, "pos_low": 0, "pos_over": 0, "pos_no_rack": 0}

    def process(self, cid, fid, img, tools, recipe, now):
        c = self.cfg
        box_names = _classes(c.get("box_classes", DEFAULT_BOXES))
        rack_names = _classes(c.get("rack_classes", DEFAULT_RACKS))
        box_cls, rack_cls = {n.lower() for n in box_names}, {n.lower() for n in rack_names}
        dets = yolo.detect(img, c.get("model") or "yoloe-26s-seg.pt", c.get("conf", 0.3),
                           box_names + rack_names, imgsz=int(c.get("imgsz", 960)), key=(cid, fid))
        boxes = _dedupe([d for d in dets if d["cls"].lower() in box_cls])
        racks = _dedupe([d for d in dets if d["cls"].lower() in rack_cls], 0.5)
        levels = {"empty": "advertencia", "low": "advertencia", "over": "ninguna", "no_rack": "peligro",
                  **c.get("levels", {})}
        confirm = float(c.get("confirm_s", 5))
        H, W = img.shape[:2]
        positions = [t for t in tools if t.get("roi")]
        whole = not positions
        if whole:  # sin posiciones: solo conteo general, sin alarmas
            positions = [{"id": ALL, "name": "Toda la imagen", "roi": [0, 0, W, H], "min_boxes": 0,
                          "max_boxes": 10 ** 6, "require_rack": False}]

        states, texts = [], []
        for p in positions:
            roi = [int(v) for v in p["roi"]]
            n_raw = sum(1 for b in boxes if roi[0] <= b["box"][0] + b["box"][2] / 2 <= roi[0] + roi[2]
                        and roi[1] <= b["box"][1] + b["box"][3] / 2 <= roi[1] + roi[3])
            area = max(1, roi[2] * roi[3])
            rack = any(_overlap(roi, r["box"]) >= 0.3 * min(area, r["box"][2] * r["box"][3]) for r in racks)
            hist = self._hist.setdefault((cid, p["id"]), deque(maxlen=5))
            hist.append(n_raw)
            n = int(round(statistics.median(hist)))
            mn, mx = int(p.get("min_boxes", 1)), int(p.get("max_boxes", 99))
            if p.get("require_rack", True) and not rack:
                raw_status = "SIN RACK"
            elif n < mn:
                raw_status = "VACÍO" if n == 0 else "BAJO"
            elif n > mx:
                raw_status = "EXCESO"
            else:
                raw_status = "OK"
            status = self._stable.update((cid, p["id"]), raw_status, now, confirm)
            plevel = "ninguna" if whole or status == "OK" else levels.get(STATE_KEY[status], "ninguna")
            if plevel != "ninguna":
                texts.append(f"{p['name']}: {status} ({n} cajas)")
            states.append({"id": p["id"], "name": p["name"], "camera": cid, "level": plevel, "status": status,
                           "boxes": n, "rack": rack, "min": mn, "max": mx,
                           "text": f"{status} · {n} caja(s)" + ("" if rack else " · sin rack"), "roi": roi})
        real = [] if whole else states
        summary = {"boxes": len(boxes), "racks": len(racks),
                   "cardboard": sum(1 for b in boxes if box_type(b["cls"]) == "carton"),
                   "plastic": sum(1 for b in boxes if box_type(b["cls"]) == "plastico"),
                   "pos_ok": sum(1 for s in real if s["status"] == "OK"),
                   "pos_empty": sum(1 for s in real if s["status"] == "VACÍO"),
                   "pos_low": sum(1 for s in real if s["status"] == "BAJO"),
                   "pos_over": sum(1 for s in real if s["status"] == "EXCESO"),
                   "pos_no_rack": sum(1 for s in real if s["status"] == "SIN RACK")}
        return {"tools": [{k: v for k, v in s.items() if k != "roi"} for s in states], "summary": summary,
                "level": worst(s["level"] for s in states), "text": " · ".join(texts),
                "boxes": boxes, "racks": racks, "positions": states, "whole": whole}

    def draw_tools(self, img, tools):
        for p in tools:
            if p.get("roi"):
                x, y, w, h = [int(v) for v in p["roi"]]
                cv2.rectangle(img, (x, y), (x + w, y + h), NEUTRAL, 2)
                put_label(img, p["name"], (x + 6, y + 24), NEUTRAL)

    def draw(self, img, res, tools):
        for r in res["racks"]:
            x, y, w, h = r["box"]
            cv2.rectangle(img, (x, y), (x + w, y + h), RACK_COLOR, 2)
            put_label(img, f"rack {r['conf']:.2f}", (x + 4, y + 20), RACK_COLOR, 0.5)
        for b in res["boxes"]:
            x, y, w, h = b["box"]
            cv2.rectangle(img, (x, y), (x + w, y + h), BOX_COLOR, 2)
            put_label(img, f"{box_type(b['cls'])} {b['conf']:.2f}", (x, max(12, y - 4)), BOX_COLOR, 0.45)
        if not res["whole"]:
            for p in res["positions"]:
                x, y, w, h = p["roi"]
                color = LEVEL_COLORS["ninguna"] if p["status"] == "OK" else LEVEL_COLORS[p["level"]] \
                    if p["level"] != "ninguna" else NEUTRAL
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
                put_label(img, f"{p['name']}: {p['status']} ({p['boxes']})", (x + 6, y + 24), color, 0.6)
        s = res["summary"]
        banner(img, f"EN VIVO   cajas: {s['boxes']}   racks: {s['racks']}" +
               ("" if res["whole"] else f"   posiciones OK: {s['pos_ok']}  vacias: {s['pos_empty']}"
                                        f"  bajas: {s['pos_low']}  sin rack: {s['pos_no_rack']}"),
               LEVEL_COLORS[res["level"]])

    def tcp_fields(self, s):
        m = s["summary"]
        pos = [f"{t['boxes']}:{t['status'].replace('Í', 'I').replace(' ', '')}" for t in s["tools"] if t["id"] != ALL]
        return ["1" if s["level"] != "ninguna" else "0", str(m.get("boxes", 0)), str(m.get("racks", 0))] + pos
