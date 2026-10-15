"""Acceso compartido a modelos YOLO (Ultralytics).

Los modelos se cargan una sola vez y se comparten entre los bloques (zonas de seguridad, EPP,
stock). Un candado por modelo evita llamadas simultáneas al mismo modelo.

Modelos en ./models (todos de uso libre):
  yolo26n.pt        personas (COCO, 80 clases)                       Ultralytics, AGPL-3.0
  epp_yolov8n.pt    guantes, chaleco, lentes, casco, cubrebocas, calzado   Hugging Face Nexus3, MIT
  yoloe-26s-seg.pt  vocabulario abierto: cajas y racks descritos en texto (stock)  Ultralytics, AGPL-3.0
                    usa mobileclip2_b.ts (codificador de texto) solo cuando cambian las clases
Si un modelo oficial de Ultralytics no existe en ./models, se descarga la primera vez.
"""
import io
import logging
import os
import threading
from collections import OrderedDict

from ..config import BASE_DIR

log = logging.getLogger("yolo")

MODELS_DIR = BASE_DIR / "models"
DEFAULT_MODEL = "yolo26n.pt"
PERSON = "person"

# Con `key` (cámara, cuadro) la detección se guarda y otro bloque que pida el mismo cuadro
# la reutiliza. Se detecta con esta confianza mínima y cada bloque filtra con la suya.
CACHE_CONF = 0.2
CACHE_SIZE = 16

_models = {}
_locks = {}
_global = threading.Lock()
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _resolve(name):
    """Busca el modelo en ./models; si no está, deja que Ultralytics lo descargue ahí."""
    p = MODELS_DIR / name
    if p.exists():
        return str(p)
    MODELS_DIR.mkdir(exist_ok=True)
    return name if "/" in name or "\\" in name else str(p)


def is_open_vocab(name):
    """YOLOE: detecta lo que se le pida en texto ("storage box", "shelving unit", ...)."""
    return "yoloe" in str(name).lower()


def _get(name):
    with _global:
        if name not in _models:
            from ultralytics import YOLO, YOLOE  # import diferido: arranque rápido del servidor

            log.info("Cargando modelo YOLO: %s", name)
            _models[name] = (YOLOE if is_open_vocab(name) else YOLO)(_resolve(name))
            _locks[name] = threading.Lock()
        return _models[name], _locks[name]


_vocab = {}  # modelo YOLOE -> clases de texto cargadas


def _set_vocabulary(model, m, names):
    """Carga en el modelo YOLOE las clases de texto. Las "huellas" de texto se guardan en
    models/vocab: el codificador de texto (mobileclip2_b.ts, 242 MB) solo se usa cuando cambian."""
    key = tuple(names)
    if _vocab.get(model) == key:
        return
    import hashlib

    import torch

    cache = MODELS_DIR / "vocab" / (hashlib.sha1(f"{model}|{'|'.join(key)}".encode()).hexdigest()[:16] + ".pt")
    if cache.exists():
        pe = torch.load(cache, map_location="cpu", weights_only=True)
    else:
        log.info("Calculando clases de texto para %s: %s", model, ", ".join(key))
        from ultralytics.utils import downloads

        original = downloads.attempt_download_asset

        def in_models_dir(file, *a, **k):  # el codificador de texto se lee (o descarga) en ./models
            if os.path.basename(str(file)) == str(file):
                file = str(MODELS_DIR / file)
            path = original(file, *a, **k)
            if str(path).endswith(".ts"):
                # torch.jit.load no abre rutas con acentos en Windows ("Visión"): se le pasa en memoria
                with open(path, "rb") as f:
                    return io.BytesIO(f.read())
            return path

        downloads.attempt_download_asset = in_models_dir
        try:
            pe = m.get_text_pe(list(key))
        finally:
            downloads.attempt_download_asset = original
        cache.parent.mkdir(exist_ok=True)
        torch.save(pe, cache)
    m.set_classes(list(key), pe)
    _vocab[model] = key


def class_ids(model, classes):
    """Ids de las clases pedidas (sin distinguir mayúsculas). None = todas."""
    if not classes:
        return None
    m, _ = _get(model or DEFAULT_MODEL)
    wanted = {c.strip().lower() for c in classes if c.strip()}
    return [i for i, n in m.names.items() if n.lower() in wanted]


def _predict(img, model, conf, ids, imgsz, vocab=None):
    m, lock = _get(model)
    with lock:
        if vocab:
            _set_vocabulary(model, m, vocab)
        res = m.predict(img, conf=float(conf), classes=ids, imgsz=int(imgsz), verbose=False)[0]
    out = []
    for (x1, y1, x2, y2), c, p in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
        out.append({"cls": res.names[int(c)], "conf": round(float(p), 3),
                    "box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]})
    return out


def detect(img, model=DEFAULT_MODEL, conf=0.5, classes=None, imgsz=640, key=None):
    """Devuelve [{"cls": str, "conf": float, "box": [x, y, w, h]}] en coordenadas de `img`.
    `classes`: nombres a conservar (None = todas). `key`: (cámara, id de cuadro) para la caché."""
    model = model or DEFAULT_MODEL
    vocab = None
    if is_open_vocab(model):  # YOLOE: las clases pedidas son su vocabulario
        vocab = tuple(sorted({c.strip() for c in classes or () if c.strip()}))
        if not vocab:
            return []
        ids = None
    else:
        ids = class_ids(model, classes)
        if ids is not None and not ids:
            return []  # ninguna de las clases pedidas existe en el modelo
    if key is None or conf < CACHE_CONF:
        return _predict(img, model, conf, ids, imgsz, vocab)
    ck = (model, int(imgsz), tuple(ids or ()), vocab, key)
    with _cache_lock:
        hit = _cache.get(ck)
    if hit is None:
        hit = _predict(img, model, CACHE_CONF, ids, imgsz, vocab)
        with _cache_lock:
            _cache[ck] = hit
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return [d for d in hit if d["conf"] >= conf]


def class_names(model=DEFAULT_MODEL):
    m, _ = _get(model or DEFAULT_MODEL)
    return list(m.names.values())


def available_models():
    MODELS_DIR.mkdir(exist_ok=True)
    return sorted(p.name for p in MODELS_DIR.glob("*.pt"))
