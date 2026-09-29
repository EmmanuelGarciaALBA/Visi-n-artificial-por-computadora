"""Herramientas de inspección.

Cada herramienta recibe la imagen completa, su ROI y sus parámetros, y devuelve
(valor, overlays) o (valor, overlays, extra). El motor compara el valor contra
[min, max] para decidir OK / NOK.

`extra` es opcional:
  "text": texto leído (códigos) que se publica por OPC UA / TCP / web
  "ok":   False fuerza NOK aunque el valor esté en rango (p. ej. código distinto al esperado)

Para agregar una herramienta nueva: escribir una función `tool_xxx(img, roi, params)`
y registrarla en TOOLS.
"""
import logging
import re

import cv2
import numpy as np

from . import yolo

log = logging.getLogger("tools")


def clip_roi(img, roi):
    H, W = img.shape[:2]
    x, y, w, h = [int(v) for v in roi]
    x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
    w, h = max(1, min(w, W - x)), max(1, min(h, H - y))
    return x, y, w, h


def tool_brightness(img, roi, p):
    """Brillo promedio (0-255) en la ROI. Útil para presencia/ausencia simple."""
    x, y, w, h = roi
    gray = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    return float(gray.mean()), []


def tool_color_area(img, roi, p):
    """Porcentaje de la ROI dentro de un rango de color HSV (H: 0-179)."""
    x, y, w, h = roi
    hsv = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    lo = np.array([p.get("h_min", 0), p.get("s_min", 0), p.get("v_min", 0)], np.uint8)
    hi = np.array([p.get("h_max", 179), p.get("s_max", 255), p.get("v_max", 255)], np.uint8)
    if lo[0] <= hi[0]:
        mask = cv2.inRange(hsv, lo, hi)
    else:  # rango de tono que cruza el 0 (p. ej. rojo 170..10)
        mask = cv2.inRange(hsv, lo, np.array([179, hi[1], hi[2]], np.uint8)) | cv2.inRange(
            hsv, np.array([0, lo[1], lo[2]], np.uint8), hi)
    pct = 100.0 * cv2.countNonZero(mask) / mask.size
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    overlays = [{"type": "contour", "pts": (c + (x, y)).tolist()} for c in contours if cv2.contourArea(c) > 50]
    return pct, overlays


def tool_blob_count(img, roi, p):
    """Cuenta objetos por umbralización. threshold=0 usa Otsu automático."""
    x, y, w, h = roi
    gray = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    mode = cv2.THRESH_BINARY_INV if p.get("invert") else cv2.THRESH_BINARY
    thr = int(p.get("threshold", 0))
    if thr <= 0:
        _, mask = cv2.threshold(gray, 0, 255, mode | cv2.THRESH_OTSU)
    else:
        _, mask = cv2.threshold(gray, thr, 255, mode)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    amin, amax = float(p.get("min_area", 100)), float(p.get("max_area", 1e9))
    overlays = []
    for c in contours:
        if amin <= cv2.contourArea(c) <= amax:
            bx, by, bw, bh = cv2.boundingRect(c)
            overlays.append({"type": "box", "rect": [bx + x, by + y, bw, bh]})
    return float(len(overlays)), overlays


def _yolo_count(img, roi, p, classes):
    x, y, w, h = roi
    dets = yolo.detect(img[y:y + h, x:x + w], p.get("model") or yolo.DEFAULT_MODEL,
                       p.get("conf", 0.5), classes)
    overlays = [{"type": "box", "rect": [d["box"][0] + x, d["box"][1] + y, d["box"][2], d["box"][3]],
                 "label": f"{d['cls']} {d['conf']:.2f}"} for d in dets]
    return float(len(dets)), overlays


def tool_person_detect(img, roi, p):
    """Cuenta personas en la ROI con YOLO (clase COCO 'person')."""
    return _yolo_count(img, roi, p, [yolo.PERSON])


def tool_yolo_detect(img, roi, p):
    """Cuenta detecciones YOLO de las clases indicadas (separadas por coma; vacío = todas).
    Con el modelo por defecto (COCO) hay 80 clases: person, car, bottle, cup, ..."""
    wanted = [c.strip() for c in str(p.get("class", "")).split(",") if c.strip()]
    return _yolo_count(img, roi, p, wanted or None)


# ---------------------------------------------------------------- códigos
_SYMBOLOGIES = {
    "todos": None,
    "qr": ("QRCode", "MicroQRCode", "RMQRCode"),
    "datamatrix": ("DataMatrix",),
    "barras": ("AllLinear", "LinearCodes"),
}


def _zxing_formats(zx, name):
    names = _SYMBOLOGIES.get(str(name).lower())
    if not names:
        return None
    fmt = None
    for n in names:
        f = getattr(zx.BarcodeFormat, n, None)
        if f is not None:
            fmt = f if fmt is None else fmt | f
    return fmt


def tool_code_reader(img, roi, p):
    """Lee códigos QR, DataMatrix y de barras (EAN, UPC, Code128, Code39, ITF, ...).
    Valor = cantidad de códigos leídos. Si 'expected' tiene texto, cada lectura debe
    coincidir con él (se acepta una expresión regular) para dar OK."""
    import zxingcpp as zx

    x, y, w, h = roi
    crop = np.ascontiguousarray(img[y:y + h, x:x + w])
    kwargs = {}
    fmt = _zxing_formats(zx, p.get("symbology", "todos"))
    if fmt is not None:
        kwargs["formats"] = fmt
    codes = zx.read_barcodes(crop, **kwargs)

    overlays, texts = [], []
    for c in codes:
        pos = c.position
        pts = [[pt.x + x, pt.y + y] for pt in (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)]
        fmt_name = str(c.format).split(".")[-1]
        overlays.append({"type": "contour", "pts": pts, "label": f"{fmt_name}: {c.text[:30]}"})
        texts.append(c.text)

    extra = {"text": " | ".join(texts)}
    expected = str(p.get("expected", "")).strip()
    if expected:
        try:
            rx = re.compile(expected)
            extra["ok"] = bool(texts) and all(rx.fullmatch(t) for t in texts)
        except re.error:
            extra["ok"] = bool(texts) and all(t == expected for t in texts)
    return float(len(codes)), overlays, extra


TOOLS = {
    "brightness": tool_brightness,
    "color_area": tool_color_area,
    "blob_count": tool_blob_count,
    "person_detect": tool_person_detect,
    "yolo_detect": tool_yolo_detect,
    "code_reader": tool_code_reader,
}
