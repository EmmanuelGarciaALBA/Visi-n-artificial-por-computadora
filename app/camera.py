"""Fuentes de imagen: cámara USB, cámara IP (RTSP/HTTP), carpeta de imágenes o patrón de prueba.

Un hilo de captura mantiene siempre el último frame disponible, así la inspección
no espera al buffer de la cámara.
"""
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

# RTSP por TCP: por Wi-Fi, UDP pierde paquetes y la imagen sale con artefactos.
# Espera máxima 10 s si la cámara no responde. OpenCV lee esta variable al abrir la captura.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;10000000")

log = logging.getLogger("camera")

# Un cuadro más viejo que esto se considera inválido (cámara congelada o desconectada)
MAX_FRAME_AGE_S = 3.0

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def imread_unicode(path):
    """cv2.imread no abre rutas con acentos en Windows (p. ej. "Visión"); esto sí."""
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


class TestPatternSource:
    """Genera una 'banda transportadora' sintética con piezas de colores.
    Sirve para probar todo el sistema sin cámara física."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.t0 = time.time()
        rng = np.random.default_rng(7)
        colors = [(60, 60, 255), (60, 230, 60), (255, 150, 40), (40, 220, 250)]
        self.parts = [
            {
                "x": float(rng.uniform(0, width)),
                "y": float(rng.uniform(0.25, 0.75) * height),
                "r": int(rng.uniform(0.04, 0.08) * height),
                "c": colors[i % len(colors)],
            }
            for i in range(6)
        ]

    def read(self):
        dt = time.time() - self.t0
        img = np.full((self.h, self.w, 3), 45, np.uint8)
        cv2.rectangle(img, (0, int(self.h * 0.15)), (self.w, int(self.h * 0.85)), (70, 70, 70), -1)
        speed = self.w * 0.08
        for p in self.parts:
            x = int((p["x"] + dt * speed) % (self.w + 2 * p["r"])) - p["r"]
            cv2.circle(img, (x, int(p["y"])), p["r"], p["c"], -1)
        noise = np.random.randint(0, 12, img.shape, np.uint8)
        img = cv2.add(img, noise)
        time.sleep(1 / 30)
        return True, img

    def release(self):
        pass


class FolderSource:
    """Recorre cíclicamente las imágenes de una carpeta (útil para probar con fotos reales)."""

    def __init__(self, folder, period=1.0):
        self.files = sorted(p for p in Path(folder).glob("*") if p.suffix.lower() in IMG_EXT)
        if not self.files:
            raise RuntimeError(f"No hay imágenes en la carpeta: {folder}")
        self.i = 0
        self.period = period
        self.last = 0.0
        self.img = None

    def read(self):
        now = time.time()
        if self.img is None or now - self.last >= self.period:
            self.img = imread_unicode(self.files[self.i % len(self.files)])
            self.i += 1
            self.last = now
        time.sleep(1 / 30)
        return self.img is not None, self.img

    def release(self):
        pass


def open_source(cam_cfg):
    ctype = cam_cfg.get("type", "test")
    src = str(cam_cfg.get("source", "0"))
    w, h = int(cam_cfg.get("width", 1280)), int(cam_cfg.get("height", 720))
    if ctype == "test":
        return TestPatternSource(w, h)
    if ctype == "folder":
        return FolderSource(src)
    if ctype == "usb":
        cap = cv2.VideoCapture(int(src), cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    elif ctype == "url":
        user, pwd = cam_cfg.get("user", ""), cam_cfg.get("password", "")
        if user and "://" in src and "@" not in src:
            scheme, rest = src.split("://", 1)
            src = f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{rest}"
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    else:
        raise ValueError(f"Tipo de cámara desconocido: {ctype}")
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir la cámara {ctype}:{cam_cfg.get('source')}")
    return cap


class Camera:
    def __init__(self, cam_cfg):
        self.cfg = cam_cfg
        self._frame = None
        self._frame_id = 0
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error = None
        self.fps = 0.0
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def _run(self):
        src = None
        n, t_fps = 0, time.time()
        while not self._stop.is_set():
            if src is None:
                try:
                    src = open_source(self.cfg)
                    self.error = None
                    log.info("Cámara abierta: %s %s", self.cfg.get("type"), self.cfg.get("source"))
                except Exception as e:
                    self.error = str(e)
                    log.warning("Error abriendo cámara: %s (reintento en 3 s)", e)
                    self._stop.wait(3)
                    continue
            ok, frame = src.read()
            if not ok or frame is None:
                self.error = "Sin imagen de la cámara"
                with self._lock:
                    self._frame = None
                src.release()
                src = None
                self._stop.wait(1)
                continue
            with self._lock:
                self._frame = frame
                self._frame_id += 1
                self._frame_time = time.time()
                self.error = None
            n += 1
            if time.time() - t_fps >= 1:
                self.fps = n / (time.time() - t_fps)
                n, t_fps = 0, time.time()
        if src is not None:
            src.release()

    def latest(self):
        """Devuelve (frame_id, copia del último frame) o (0, None)."""
        with self._lock:
            if self._frame is None:
                return 0, None
            if time.time() - self._frame_time > MAX_FRAME_AGE_S:
                self.error = self.error or f"Imagen congelada (sin cuadros nuevos en {MAX_FRAME_AGE_S:g} s)"
                return 0, None
            return self._frame_id, self._frame.copy()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)
