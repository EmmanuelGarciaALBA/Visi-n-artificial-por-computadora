"""Servidor OPC UA: expone resultados, recetas y zonas de seguridad para SCADA/PLC.

Árbol de nodos (NodeId de tipo string, namespace de la config):
  VisionServer.Control.Trigger            Bool   (RW)  PLC pone True -> se inspecciona -> el servidor lo regresa a False
  VisionServer.Control.ResetCounter       Bool   (RW)
  VisionServer.Control.RecipeNumber       Int32  (RW)  PLC escribe el número de receta a cargar
  VisionServer.Status.Heartbeat           UInt32       incrementa cada segundo
  VisionServer.Status.InspectionRunning   Bool         módulo de inspección en ejecución
  VisionServer.Status.Ready               Bool         en ejecución y sin inspección en curso
  VisionServer.Status.Busy                Bool
  VisionServer.Status.CameraOk            Bool
  VisionServer.Status.Error               String
  VisionServer.Status.ActiveRecipeNumber  Int32
  VisionServer.Status.ActiveRecipeName    String
  VisionServer.Result.Counter             UInt32
  VisionServer.Result.Pass                Bool
  VisionServer.Result.CycleTimeMs         Double
  VisionServer.Result.Timestamp           String
  VisionServer.Tools.<Nombre>.Value       Double
  VisionServer.Tools.<Nombre>.Pass        Bool
  VisionServer.Tools.<Nombre>.Text        String      (lectores de código)
  VisionServer.Safety.Enabled             Bool        monitor de zonas en ejecución
  VisionServer.Safety.Danger              Bool        persona en zona de peligro (o falla del monitor)
  VisionServer.Safety.Warning             Bool
  VisionServer.Safety.PersonCount         UInt32
  VisionServer.Safety.Zones.<Nombre>.Occupied  Bool
  VisionServer.Safety.Zones.<Nombre>.Count     UInt32
"""
import asyncio
import logging
import re

from asyncua import Server, ua

log = logging.getLogger("opcua")
logging.getLogger("asyncua").setLevel(logging.WARNING)

V = ua.VariantType


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name) or "Item"


def _unique_names(items):
    """[(item, nombre_seguro_unico)] — nombres repetidos reciben el id como sufijo."""
    used, out = set(), []
    for it in items:
        name = _safe(it["name"])
        if name in used:
            name = f"{name}_{_safe(it['id'])}"
        used.add(name)
        out.append((it, name))
    return out


class OpcUaServer:
    def __init__(self, engine, cfg, safety=None):
        self.engine = engine
        self.safety = safety
        self.cfg = cfg
        self.server = None
        self.idx = None
        self.nodes = {}
        self.tool_nodes = {}
        self.zone_nodes = {}
        self.tools_folder = None
        self.zones_folder = None
        self._tasks = []
        self._loop = None
        self._build_lock = asyncio.Lock()
        self._recipe_req = None  # último valor visto en Control.RecipeNumber
        self.running = False
        self.error = None

    def _nid(self, path):
        return ua.NodeId(f"VisionServer.{path}", self.idx)

    async def _var(self, parent, path, value, vtype, writable=False):
        node = await parent.add_variable(self._nid(path), path.split(".")[-1], ua.Variant(value, vtype))
        if writable:
            await node.set_writable()
        self.nodes[path] = (node, vtype)
        return node

    async def start(self):
        c = self.cfg["opcua"]
        self._loop = asyncio.get_running_loop()
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://0.0.0.0:{c['port']}/visionserver/")
        self.server.set_server_name("VisionServer")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.idx = await self.server.register_namespace(c["namespace"])

        objects = self.server.nodes.objects
        root = await objects.add_object(ua.NodeId("VisionServer", self.idx), "VisionServer")
        ctrl = await root.add_folder(self._nid("Control"), "Control")
        status = await root.add_folder(self._nid("Status"), "Status")
        res = await root.add_folder(self._nid("Result"), "Result")
        self.tools_folder = await root.add_folder(self._nid("Tools"), "Tools")
        safety = await root.add_folder(self._nid("Safety"), "Safety")
        self.zones_folder = await safety.add_folder(self._nid("Safety.Zones"), "Zones")

        rec = self.engine.recipe
        await self._var(ctrl, "Control.Trigger", False, V.Boolean, writable=True)
        await self._var(ctrl, "Control.ResetCounter", False, V.Boolean, writable=True)
        await self._var(ctrl, "Control.RecipeNumber", int(rec["number"]), V.Int32, writable=True)
        await self._var(status, "Status.Heartbeat", 0, V.UInt32)
        await self._var(status, "Status.InspectionRunning", self.engine.running, V.Boolean)
        await self._var(status, "Status.Ready", self.engine.running, V.Boolean)
        await self._var(status, "Status.Busy", False, V.Boolean)
        await self._var(status, "Status.CameraOk", False, V.Boolean)
        await self._var(status, "Status.Error", "", V.String)
        await self._var(status, "Status.ActiveRecipeNumber", int(rec["number"]), V.Int32)
        await self._var(status, "Status.ActiveRecipeName", rec["name"], V.String)
        await self._var(res, "Result.Counter", 0, V.UInt32)
        await self._var(res, "Result.Pass", False, V.Boolean)
        await self._var(res, "Result.CycleTimeMs", 0.0, V.Double)
        await self._var(res, "Result.Timestamp", "", V.String)
        await self._var(safety, "Safety.Enabled", False, V.Boolean)
        await self._var(safety, "Safety.Danger", False, V.Boolean)
        await self._var(safety, "Safety.Warning", False, V.Boolean)
        await self._var(safety, "Safety.PersonCount", 0, V.UInt32)
        await self.rebuild()

        await self.server.start()
        self.running = True
        self._tasks = [asyncio.create_task(self._poll_control()), asyncio.create_task(self._heartbeat())]
        self.engine.add_listener(self._on_result)
        if self.safety:
            self.safety.add_listener(self._on_safety)
        log.info("OPC UA escuchando en opc.tcp://<IP>:%s/visionserver/", c["port"])

    # ---------- nodos dinámicos (herramientas de la receta activa y zonas) ----------
    async def rebuild(self):
        async with self._build_lock:
            for n in list(self.tool_nodes.values()) + list(self.zone_nodes.values()):
                await self.server.delete_nodes([n["folder"]], recursive=True)
            self.tool_nodes, self.zone_nodes = {}, {}
            for t, name in _unique_names(self.engine.tools):
                p = f"Tools.{name}"
                folder = await self.tools_folder.add_folder(self._nid(p), name)
                self.tool_nodes[t["id"]] = {
                    "folder": folder,
                    "value": await folder.add_variable(self._nid(f"{p}.Value"), "Value", ua.Variant(0.0, V.Double)),
                    "pass": await folder.add_variable(self._nid(f"{p}.Pass"), "Pass", ua.Variant(False, V.Boolean)),
                    "text": await folder.add_variable(self._nid(f"{p}.Text"), "Text", ua.Variant("", V.String)),
                }
            for z, name in _unique_names(self.engine.cfg["safety"].get("zones", [])):
                p = f"Safety.Zones.{name}"
                folder = await self.zones_folder.add_folder(self._nid(p), name)
                self.zone_nodes[z["id"]] = {
                    "folder": folder,
                    "occupied": await folder.add_variable(self._nid(f"{p}.Occupied"), "Occupied",
                                                          ua.Variant(False, V.Boolean)),
                    "count": await folder.add_variable(self._nid(f"{p}.Count"), "Count", ua.Variant(0, V.UInt32)),
                }
            rec = self.engine.recipe
            await self._write("Status.ActiveRecipeNumber", int(rec["number"]))
            await self._write("Status.ActiveRecipeName", rec["name"])
            await self._write("Control.RecipeNumber", int(rec["number"]))
            self._recipe_req = int(rec["number"])

    async def rebuild_tools(self):
        if self.running:
            await self.rebuild()

    @staticmethod
    async def _set(node, value, vtype):
        await node.write_value(ua.DataValue(ua.Variant(value, vtype)))

    async def _write(self, path, value):
        node, vtype = self.nodes[path]
        await self._set(node, value, vtype)

    # ---------- ciclo de control ----------
    async def _poll_control(self):
        trig_node = self.nodes["Control.Trigger"][0]
        reset_node = self.nodes["Control.ResetCounter"][0]
        recipe_node = self.nodes["Control.RecipeNumber"][0]
        loop = asyncio.get_running_loop()
        while True:
            try:
                wanted = await recipe_node.read_value()
                if wanted != self._recipe_req:  # solo cuando el PLC cambia el valor
                    self._recipe_req = wanted
                    if wanted != self.engine.recipe["number"]:
                        r = await loop.run_in_executor(None, self.engine.select_recipe, wanted)
                        if r is None:
                            await self._write("Status.Error", f"Receta {wanted} no existe")
                            await self._write("Control.RecipeNumber", int(self.engine.recipe["number"]))
                            self._recipe_req = int(self.engine.recipe["number"])
                        # si existe, el listener de receta (main.py) reconstruye los nodos
                if await trig_node.read_value():
                    await self._write("Status.Busy", True)
                    await self._write("Status.Ready", False)
                    r = await loop.run_in_executor(None, self.engine.inspect, "opcua")
                    if r is None:
                        await self._write("Status.Error", "Inspección detenida: presione Ejecutar")
                    await self._write("Control.Trigger", False)  # acuse de recibo al PLC
                    await self._write("Status.Busy", False)
                    await self._write("Status.Ready", self.engine.running)
                if await reset_node.read_value():
                    self.engine.counter = 0
                    await self._write("Result.Counter", 0)
                    await self._write("Control.ResetCounter", False)
            except Exception:
                log.exception("Error en ciclo de control OPC UA")
            await asyncio.sleep(0.02)

    async def _heartbeat(self):
        hb = 0
        while True:
            hb = (hb + 1) % 2**32
            try:
                await self._write("Status.Heartbeat", hb)
                await self._write("Status.InspectionRunning", self.engine.running)
                if not self.engine.running:
                    await self._write("Status.Ready", False)
                cam_err = self.engine.camera.error
                await self._write("Status.CameraOk", cam_err is None)
                if cam_err:
                    await self._write("Status.Error", cam_err)
            except Exception:
                log.exception("Error en heartbeat")
            await asyncio.sleep(1)

    # ---------- publicación ----------
    def _on_result(self, result):
        """Llamado desde el hilo de visión."""
        if self._loop and self.running:
            asyncio.run_coroutine_threadsafe(self._publish(result), self._loop)

    async def _publish(self, r):
        await self._write("Result.Counter", r["counter"] % 2**32)
        await self._write("Result.Pass", bool(r["pass"]))
        await self._write("Result.CycleTimeMs", float(r["cycle_ms"]))
        await self._write("Result.Timestamp", r["timestamp"])
        await self._write("Status.Error", r.get("error") or "")
        for tr in r["tools"]:
            n = self.tool_nodes.get(tr["id"])
            if n:
                await self._set(n["value"], float(tr["value"]), V.Double)
                await self._set(n["pass"], bool(tr["pass"]), V.Boolean)
                await self._set(n["text"], tr.get("text", ""), V.String)

    def _on_safety(self, state):
        if self._loop and self.running:
            asyncio.run_coroutine_threadsafe(self._publish_safety(state), self._loop)

    async def _publish_safety(self, s):
        await self._write("Safety.Enabled", bool(s["enabled"]))
        await self._write("Safety.Danger", bool(s["danger"]))
        await self._write("Safety.Warning", bool(s["warning"]))
        await self._write("Safety.PersonCount", int(s["persons"]))
        for z in s["zones"]:
            n = self.zone_nodes.get(z["id"])
            if n:
                await self._set(n["occupied"], bool(z["occupied"]), V.Boolean)
                await self._set(n["count"], int(z["count"]), V.UInt32)

    async def stop(self):
        self.running = False
        self.engine.remove_listener(self._on_result)
        if self.safety:
            self.safety.remove_listener(self._on_safety)
        for t in self._tasks:
            t.cancel()
        if self.server:
            await self.server.stop()
