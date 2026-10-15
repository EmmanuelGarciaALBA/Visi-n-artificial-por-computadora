"""Une las cámaras, los 4 bloques de inspección y las alarmas."""
import logging

from .alarms import AlarmManager
from .camera import CameraManager
from .vision.andon import AndonModule
from .vision.ppe import PpeModule
from .vision.safety import SafetyModule
from .vision.stock import StockModule

log = logging.getLogger("hub")

# Nombres aceptados por TCP (y la API) para cada bloque
ALIASES = {
    "safety": "safety", "seguridad": "safety", "zonas": "safety", "saf": "safety",
    "ppe": "ppe", "epp": "ppe",
    "andon": "andon", "and": "andon",
    "stock": "stock", "stk": "stock",
}


class Hub:
    def __init__(self, cfg, web_urls):
        self.cfg = cfg
        self.web_urls = web_urls  # función -> ["http://ip:puerto", ...] (para los correos)
        self.cameras = CameraManager(cfg["cameras"])
        self.modules = {}
        self.alarms = AlarmManager(self)
        self.modules = {m.name: m for m in (SafetyModule(self), PpeModule(self), AndonModule(self), StockModule(self))}
        self._recipe_listeners = []
        for m in self.modules.values():
            m.start()

    def module(self, key):
        return self.modules.get(ALIASES.get(str(key).strip().lower(), ""))

    def add_recipe_listener(self, fn):
        """fn(module, recipe) cuando cambia la receta activa de un bloque (web, OPC UA o TCP)."""
        self._recipe_listeners.append(fn)

    def on_recipe(self, module, recipe):
        for fn in list(self._recipe_listeners):
            try:
                fn(module, recipe)
            except Exception:
                log.exception("Error notificando cambio de receta")

    def apply_config(self, cfg):
        self.cfg = cfg
        self.cameras.apply(cfg["cameras"])

    def stop(self):
        for m in self.modules.values():
            m.stop()
        self.cameras.stop()
