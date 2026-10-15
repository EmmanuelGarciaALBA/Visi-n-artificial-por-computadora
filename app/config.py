"""Carga y guardado de la configuración (config.json).

Estructura:
  cameras  lista de cámaras; cada una indica qué funciones (módulos) corren sobre su imagen
  modules  los 4 bloques de inspección: safety (zonas), ppe (EPP), andon (torretas), stock
           cada bloque tiene sus recetas (conjuntos de herramientas), su alarma y su correo
  email    servidor de correo para enviar las alarmas (Gmail, Outlook o SMTP de la empresa)
  opcua / tcp / server   comunicación
"""
import copy
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# VS_CONFIG permite probar con otra configuración sin tocar la de producción
CONFIG_PATH = Path(os.environ.get("VS_CONFIG") or BASE_DIR / "config.json")

MODULE_NAMES = ("safety", "ppe", "andon", "stock")
LEVELS = ("ninguna", "advertencia", "peligro")


def _alarm(min_level="peligro"):
    return {
        "sound": True,  # sonido de alarma en los navegadores (botón 🔊 / 🔇)
        "email": {
            "enabled": False,
            "to": "",  # destinatarios separados por coma
            "min_level": min_level,  # advertencia | peligro
            "cooldown_min": 10,  # mínimo entre correos del mismo bloque
            "attach_image": True,
            "on_clear": False,  # avisar también cuando la alarma se normaliza
        },
    }


def _recipe(**extra):
    return {"id": "r1", "number": 1, "name": "Principal", "tools": [], **extra}


PPE_ITEMS = ("helmet", "gloves", "goggles", "vest", "mask", "shoes")
PPE_REQUIRE_DEFAULT = {"helmet": True, "gloves": True, "goggles": True, "vest": False, "mask": False, "shoes": False}

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8090},
    # type: test | usb | url | folder.  functions: módulos que analizan esta cámara
    "cameras": [
        {"id": "cam1", "name": "Cámara 1", "enabled": True, "type": "test", "source": "0",
         "user": "", "password": "", "width": 1280, "height": 720,
         "functions": list(MODULE_NAMES)},
    ],
    "modules": {
        # Zonas de seguridad: personas (YOLO) dentro de polígonos.
        # Herramienta = zona {id, name, enabled, camera, level, points}
        "safety": {
            "running": False, "fps": 5, "conf": 0.5, "model": "yolo26n.pt",
            "mode": "pies",  # pies: punto inferior-central | caja: cualquier parte del recuadro
            "hold_ms": 1000,  # la zona sigue ocupada este tiempo tras la última detección
            "active_recipe": "r1", "recipes": [_recipe()], "alarm": _alarm("peligro"),
        },
        # Equipo de protección personal: personas (YOLO COCO) + equipo (modelo EPP).
        # Herramienta = área {id, name, enabled, camera, roi, require{...}}; sin áreas = toda la imagen
        "ppe": {
            "running": False, "fps": 2, "conf": 0.35, "person_conf": 0.5,
            "model": "epp_yolov8n.pt", "person_model": "yolo26n.pt",
            "min_person_px": 120,  # personas más pequeñas (lejanas) no se evalúan
            "confirm_ms": 1500,  # la falta de EPP debe mantenerse este tiempo antes de alarmar
            "hold_ms": 2000,
            "level": "peligro",
            "active_recipe": "r1", "recipes": [_recipe(require=dict(PPE_REQUIRE_DEFAULT))],
            "alarm": _alarm("peligro"),
        },
        # Torretas ANDON: luces verde / amarilla / roja encendidas (color + brillo, OpenCV).
        # Herramienta = torreta {id, name, enabled, camera, roi, v_min, s_min, min_area}
        "andon": {
            "running": False, "fps": 4,
            "hold_ms": 1500,  # una luz intermitente no se reporta como apagada entre destellos
            "levels": {"red": "peligro", "yellow": "advertencia", "green": "ninguna", "off": "ninguna"},
            "active_recipe": "r1", "recipes": [_recipe()], "alarm": _alarm("advertencia"),
        },
        # Stock: cajas (cartón / plástico) en racks o estantes (YOLOE-26, vocabulario abierto en texto).
        # Herramienta = posición {id, name, enabled, camera, roi, min_boxes, max_boxes, require_rack}
        "stock": {
            "running": False, "fps": 0.5, "conf": 0.3, "imgsz": 960, "model": "yoloe-26s-seg.pt",
            "box_classes": "cardboard storage box, plastic storage box, plastic crate",
            "rack_classes": "shelving unit, storage rack",
            "confirm_s": 5,  # un cambio debe mantenerse este tiempo (personas pasando frente al rack)
            "levels": {"empty": "advertencia", "low": "advertencia", "over": "ninguna", "no_rack": "peligro"},
            "active_recipe": "r1", "recipes": [_recipe()], "alarm": _alarm("advertencia"),
        },
    },
    # provider: gmail | outlook | smtp.  Gmail y Outlook solo usan address + password.
    "email": {"provider": "gmail", "address": "", "password": "", "host": "", "port": 587,
              "security": "starttls", "user": "", "from_name": "VisionServer"},
    "opcua": {"enabled": True, "port": 4841, "namespace": "urn:visionserver"},
    "tcp": {"enabled": True, "port": 5000, "push_results": False, "format": "csv", "separator": ";"},
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
    """Convierte la configuración de la versión anterior (una cámara, módulo de inspección
    con lector de códigos y zonas de seguridad sueltas) a la estructura de 4 bloques."""
    if "camera" in loaded and "cameras" not in loaded:
        cam = loaded.pop("camera")
        loaded["cameras"] = [{"id": "cam1", "name": "Cámara 1", "enabled": True, **cam,
                              "functions": list(MODULE_NAMES)}]
    if "modules" not in loaded:
        loaded["modules"] = {}
    old_safety = loaded.pop("safety", None)
    if old_safety and "safety" not in loaded["modules"]:
        zones = [{**z, "camera": z.get("camera", "cam1")} for z in old_safety.pop("zones", [])]
        running = bool(old_safety.pop("enabled", False))
        if old_safety.get("model") == "yolo11n.pt":
            old_safety["model"] = "yolo26n.pt"  # más preciso y más rápido en CPU
        loaded["modules"]["safety"] = {**old_safety, "running": running, "active_recipe": "r1",
                                       "recipes": [_recipe(tools=zones)]}
    # El módulo de inspección genérico (y el lector QR / códigos de barras) ya no existe
    for k in ("recipes", "active_recipe", "inspection", "trigger", "tools"):
        loaded.pop(k, None)
    return loaded


def active_recipe(mcfg: dict) -> dict:
    """Receta activa de un módulo (la primera si el id guardado ya no existe)."""
    recipes = mcfg.setdefault("recipes", [])
    for r in recipes:
        if r["id"] == mcfg.get("active_recipe"):
            return r
    if not recipes:
        recipes.append(_recipe())
    mcfg["active_recipe"] = recipes[0]["id"]
    return recipes[0]


def find_recipe(mcfg: dict, key):
    """Busca una receta por número, id o nombre (sin distinguir mayúsculas)."""
    key_s = str(key).strip().lower()
    for r in mcfg.get("recipes", []):
        if str(r.get("number")) == key_s or r["id"].lower() == key_s or r["name"].lower() == key_s:
            return r
    return None


def _normalize(cfg: dict) -> dict:
    for name in MODULE_NAMES:
        active_recipe(cfg["modules"][name])
    for c in cfg["cameras"]:
        c.setdefault("functions", [])
        c.setdefault("enabled", True)
    return cfg


def load_config() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            save_config(cfg, _locked=True)
            return cfg
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _merge(DEFAULT_CONFIG, _migrate(json.load(f)))
        return _normalize(cfg)


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
