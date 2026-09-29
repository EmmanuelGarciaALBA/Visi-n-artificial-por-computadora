"""Acceso compartido a modelos YOLO (Ultralytics).

Los modelos se cargan una sola vez y se comparten entre las herramientas de inspección
y el monitor de zonas de seguridad. Un candado por modelo evita llamadas simultáneas.
Si el archivo .pt no existe, Ultralytics lo descarga la primera vez (yolo11n.pt ≈ 5 MB).
"""
import logging
import threading

from ..config import BASE_DIR

log = logging.getLogger("yolo")

MODELS_DIR = BASE_DIR / "models"
DEFAULT_MODEL = "yolo11n.pt"
PERSON = "person"

_models = {}
_locks = {}
_global = threading.Lock()


def _resolve(name):
    """Busca el modelo en ./models; si no está, deja que Ultralytics lo descargue ahí."""
    p = MODELS_DIR / name
    if p.exists():
        return str(p)
    MODELS_DIR.mkdir(exist_ok=True)
    return name if "/" in name or "\\" in name else str(p)


def _get(name):
    with _global:
        if name not in _models:
            from ultralytics import YOLO  # import diferido: arranque rápido del servidor

            log.info("Cargando modelo YOLO: %s", name)
            _models[name] = YOLO(_resolve(name))
            _locks[name] = threading.Lock()
        return _models[name], _locks[name]


def detect(img, model=DEFAULT_MODEL, conf=0.5, classes=None):
    """Devuelve [{"cls": str, "conf": float, "box": [x, y, w, h]}] en coordenadas de `img`.
    `classes`: lista de nombres a conservar (None = todas)."""
    m, lock = _get(model or DEFAULT_MODEL)
    ids = None
    if classes:
        ids = [i for i, n in m.names.items() if n in classes]
    with lock:
        res = m.predict(img, conf=float(conf), classes=ids, verbose=False)[0]
    out = []
    for (x1, y1, x2, y2), c, p in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
        out.append({"cls": res.names[int(c)], "conf": round(float(p), 3),
                    "box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]})
    return out


def class_names(model=DEFAULT_MODEL):
    m, _ = _get(model or DEFAULT_MODEL)
    return list(m.names.values())
