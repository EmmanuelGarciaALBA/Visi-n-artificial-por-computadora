"""Carga y guardado de la configuración (config.json)."""
import copy
import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8090},
    "camera": {
        # type: test | usb | url | folder
        "type": "test",
        "source": "0",
        # Credenciales para cámaras IP (se insertan en la URL rtsp://)
        "user": "",
        "password": "",
        "width": 1280,
        "height": 720,
    },
    "trigger": {
        # mode: continuous (inspección periódica) | external (solo por disparo OPC/TCP/web)
        "mode": "continuous",
        "interval_ms": 500,
    },
    # Módulo de inspección: solo procesa (y acepta disparos) mientras está en ejecución.
    "inspection": {"running": False},
    # Recetas: cada una es un programa de inspección (conjunto de herramientas).
    # El PLC puede cambiar de receta por número (OPC UA / TCP).
    "active_recipe": "r1",
    "recipes": [
        {
            "id": "r1",
            "number": 1,
            "name": "Principal",
            "tools": [
                {
                    "id": "t1",
                    "name": "Piezas",
                    "type": "blob_count",
                    "enabled": True,
                    "roi": [0, 120, 1280, 480],
                    "params": {"threshold": 100, "invert": False, "min_area": 800, "max_area": 200000},
                    "min": 1,
                    "max": 10,
                },
                {
                    "id": "t2",
                    "name": "Rojo",
                    "type": "color_area",
                    "enabled": True,
                    "roi": [0, 0, 1280, 720],
                    "params": {"h_min": 0, "h_max": 10, "s_min": 120, "s_max": 255, "v_min": 70, "v_max": 255},
                    "min": 0.5,
                    "max": 100,
                },
    
            ],
        },
    ],
    # Zonas de seguridad: detección de personas (YOLO) dentro de polígonos.
    "safety": {
        "enabled": False,
        "fps": 5,
        "conf": 0.5,
        "model": "yolo11n.pt",
        # pies: punto inferior-central de la persona | caja: cualquier parte del recuadro
        "mode": "pies",
        # Tiempo que la zona sigue "ocupada" después de la última detección (anti-parpadeo)
        "hold_ms": 1000,
        # zones: [{"id", "name", "level": "peligro"|"advertencia", "enabled", "points": [[x, y], ...]}]
        "zones": [],
    },
    "opcua": {
        "enabled": True,
        "port": 4841,
        "namespace": "urn:visionserver",
    },
    "tcp": {
        "enabled": True,
        "port": 5000,
        # Envía el resultado a todos los clientes conectados en cada inspección
        "push_results": False,
        # csv | json
        "format": "csv",
        "separator": ";",
    },
}

_lock = threading.Lock()


def _merge(default, loaded):
    """Completa las claves faltantes de `loaded` con las de `default`."""
    if not isinstance(default, dict) or not isinstance(loaded, dict):
        return loaded
    out = copy.deepcopy(default)
    for k, v in loaded.items():
        out[k] = _merge(default.get(k), v) if k in default else v
    return out


def _migrate(loaded: dict) -> dict:
    """Configs antiguas tenían una sola lista "tools": se convierte en la receta 1."""
    if "tools" in loaded and "recipes" not in loaded:
        loaded["recipes"] = [{"id": "r1", "number": 1, "name": "Principal", "tools": loaded.pop("tools")}]
        loaded["active_recipe"] = "r1"
    loaded.pop("tools", None)
    return loaded


def active_recipe(cfg: dict) -> dict:
    """Receta activa (la primera si el id guardado ya no existe)."""
    for r in cfg["recipes"]:
        if r["id"] == cfg.get("active_recipe"):
            return r
    if not cfg["recipes"]:
        cfg["recipes"].append({"id": "r1", "number": 1, "name": "Principal", "tools": []})
    cfg["active_recipe"] = cfg["recipes"][0]["id"]
    return cfg["recipes"][0]


def find_recipe(cfg: dict, key):
    """Busca una receta por número, id o nombre (sin distinguir mayúsculas)."""
    key_s = str(key).strip().lower()
    for r in cfg["recipes"]:
        if str(r.get("number")) == key_s or r["id"].lower() == key_s or r["name"].lower() == key_s:
            return r
    return None


def load_config() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            save_config(DEFAULT_CONFIG, _locked=True)
            return copy.deepcopy(DEFAULT_CONFIG)
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _merge(DEFAULT_CONFIG, _migrate(json.load(f)))
        active_recipe(cfg)
        return cfg


def save_config(cfg: dict, _locked: bool = False) -> None:
    def _write():
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        tmp.replace(CONFIG_PATH)

    if _locked:
        _write()
    else:
        with _lock:
            _write()
