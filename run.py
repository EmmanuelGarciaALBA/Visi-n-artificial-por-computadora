"""Punto de entrada: python run.py

Con pythonw.exe (arranque automático, sin ventana) no existen stdout ni stderr: cualquier
print() de una librería tumbaría el servicio, así que primero se redirigen a un archivo.
"""
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

if sys.stdout is None or sys.stderr is None:
    _salida = open(LOG_DIR / "consola.log", "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = _salida

import uvicorn  # noqa: E402  (después de asegurar la salida estándar)

from app.config import load_config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_DIR / "visionserver.log", maxBytes=5_000_000,
                                             backupCount=3, encoding="utf-8"),
    ],
)

if __name__ == "__main__":
    cfg = load_config()
    try:
        uvicorn.run("app.main:app", host=cfg["server"]["host"], port=cfg["server"]["port"],
                    log_level="warning")
    except Exception:
        logging.getLogger("run").exception("El servicio terminó por un error")
        raise
