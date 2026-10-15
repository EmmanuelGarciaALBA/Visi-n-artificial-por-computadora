"""Servidor OPC UA: expone el estado de los 4 bloques para SCADA/PLC.

NodeId de tipo string en el namespace de la config:  ns=<idx>;s=VisionServer.<ruta>

  VisionServer.Status.Heartbeat            UInt32   +1 cada segundo
  VisionServer.Status.CamerasOk            Bool     todas las cámaras habilitadas con imagen
  VisionServer.Cameras.<Nombre>.Ok / .Fps  Bool / Double

  Por bloque  <B> = Safety | PPE | Andon | Stock:
  VisionServer.<B>.Enabled                 Bool     bloque en ejecución
  VisionServer.<B>.Alarm                   Bool     alarma de peligro
  VisionServer.<B>.Warning                 Bool     advertencia
  VisionServer.<B>.Level                   Int32    0 normal | 1 advertencia | 2 peligro
  VisionServer.<B>.AlarmText / .Error      String
  VisionServer.<B>.RecipeNumber            Int32 RW el PLC escribe el número de receta a cargar
  VisionServer.<B>.ActiveRecipeName        String
  VisionServer.<B>.ResetCounters           Bool  RW True -> reinicia contadores -> vuelve a False
  VisionServer.<B>.<valores del bloque>    (ver opc_summary de cada bloque)
  VisionServer.<B>.<Carpeta>.<Herramienta>.<campo>   (Zones / Areas / Towers / Positions)
"""
import asyncio
import logging
import re

from asyncua import Server, ua

log = logging.getLogger("opcua")
logging.getLogger("asyncua").setLevel(logging.WARNING)

V = ua.VariantType
LEVEL_NUM = {"ninguna": 0, "advertencia": 1, "peligro": 2}


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


def _coerce(value, vtype):
    if vtype == V.Boolean:
        return bool(value)
    if vtype in (V.UInt32, V.Int32):
        v = int(value or 0)
        return max(0, v) % 2 ** 32 if vtype == V.UInt32 else v
    if vtype == V.Double:
        return float(value or 0)
    return str(value or "")


class OpcUaServer:
    def __init__(self, hub):
        self.hub = hub
        self.server = None
        self.idx = None
        self.nodes = {}  # ruta -> (nodo, tipo)
        self.dyn = {}  # bloque -> {tool_id: {campo: (nodo, tipo)}}
        self.cam_nodes = {}
        self.folders = {}
        self._dyn_folders = []
        self._last = {}  # nodo -> último valor escrito (solo se escriben cambios)
        self._tasks = []
        self._loop = None
        self._build_lock = asyncio.Lock()
        self._recipe_req = {}
        self.running = False
        self.error = None

    @property
    def cfg(self):
        return self.hub.cfg

    def _nid(self, path):
        return ua.NodeId(f"VisionServer.{path}", self.idx)

    async def _var(self, parent, path, value, vtype, writable=False):
        node = await parent.add_variable(self._nid(path), path.split(".")[-1], ua.Variant(_coerce(value, vtype), vtype))
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

        root = await self.server.nodes.objects.add_object(ua.NodeId("VisionServer", self.idx), "VisionServer")
        status = await root.add_folder(self._nid("Status"), "Status")
        await self._var(status, "Status.Heartbeat", 0, V.UInt32)
        await self._var(status, "Status.CamerasOk", False, V.Boolean)
        self.folders["Cameras"] = await root.add_folder(self._nid("Cameras"), "Cameras")
        for m in self.hub.modules.values():
            b = m.opc_folder
            f = await root.add_folder(self._nid(b), b)
            self.folders[m.name] = f
            for name, vt, val in (("Enabled", V.Boolean, False), ("Alarm", V.Boolean, False),
                                  ("Warning", V.Boolean, False), ("Level", V.Int32, 0),
                                  ("AlarmText", V.String, ""), ("Error", V.String, ""),
                                  ("ActiveRecipeName", V.String, m.recipe["name"])):
                await self._var(f, f"{b}.{name}", val, vt)
            await self._var(f, f"{b}.RecipeNumber", int(m.recipe["number"]), V.Int32, writable=True)
            await self._var(f, f"{b}.ResetCounters", False, V.Boolean, writable=True)
            for name, vt, _ in m.opc_summary:
                await self._var(f, f"{b}.{name}", 0 if vt != "String" else "", getattr(V, vt))
            self.folders[m.name + ".tools"] = await f.add_folder(self._nid(f"{b}.{m.opc_tools_folder}"),
                                                                 m.opc_tools_folder)
        await self.rebuild()
        await self.server.start()
        self.running = True
        self._tasks = [asyncio.create_task(self._poll_control()), asyncio.create_task(self._heartbeat())]
        for m in self.hub.modules.values():
            m.add_listener(self._on_state)
        log.info("OPC UA escuchando en opc.tcp://<IP>:%s/visionserver/", c["port"])

    # ---------------------------------------------------------------- nodos dinámicos
    async def rebuild(self):
        """Herramientas de la receta activa de cada bloque y cámaras."""
        async with self._build_lock:
            if self._dyn_folders:
                await self.server.delete_nodes(self._dyn_folders, recursive=True)
            self._dyn_folders, self.dyn, self.cam_nodes, self._last = [], {}, {}, {}
            for cam, name in _unique_names(self.cfg["cameras"]):
                p = f"Cameras.{name}"
                f = await self.folders["Cameras"].add_folder(self._nid(p), name)
                self._dyn_folders.append(f)
                self.cam_nodes[cam["id"]] = {
                    "ok": await f.add_variable(self._nid(f"{p}.Ok"), "Ok", ua.Variant(False, V.Boolean)),
                    "fps": await f.add_variable(self._nid(f"{p}.Fps"), "Fps", ua.Variant(0.0, V.Double)),
                }
            for m in self.hub.modules.values():
                tf = self.folders[m.name + ".tools"]
                self.dyn[m.name] = {}
                for t, name in _unique_names(m.recipe.get("tools", [])):
                    p = f"{m.opc_folder}.{m.opc_tools_folder}.{name}"
                    f = await tf.add_folder(self._nid(p), name)
                    self._dyn_folders.append(f)
                    fields = {}
                    for fname, vt, _ in m.opc_tool_fields:
                        vtype = getattr(V, vt)
                        node = await f.add_variable(self._nid(f"{p}.{fname}"), fname,
                                                    ua.Variant(_coerce(None, vtype), vtype))
                        fields[fname] = (node, vtype)
                    self.dyn[m.name][t["id"]] = fields
                rec = m.recipe
                await self._write(f"{m.opc_folder}.ActiveRecipeName", rec["name"])
                await self._write(f"{m.opc_folder}.RecipeNumber", int(rec["number"]))
                self._recipe_req[m.name] = int(rec["number"])

    async def rebuild_tools(self):
        if self.running:
            await self.rebuild()

    async def _set(self, node, value, vtype):
        value = _coerce(value, vtype)
        if self._last.get(node) == value:
            return
        self._last[node] = value
        await node.write_value(ua.DataValue(ua.Variant(value, vtype)))

    async def _write(self, path, value):
        node, vtype = self.nodes[path]
        await self._set(node, value, vtype)

    # ---------------------------------------------------------------- control desde el PLC
    async def _poll_control(self):
        loop = asyncio.get_running_loop()
        while True:
            for m in self.hub.modules.values():
                b = m.opc_folder
                try:
                    wanted = await self.nodes[f"{b}.RecipeNumber"][0].read_value()
                    if wanted != self._recipe_req.get(m.name):  # solo cuando el PLC cambia el valor
                        self._recipe_req[m.name] = wanted
                        if wanted != m.recipe["number"]:
                            r = await loop.run_in_executor(None, m.select_recipe, wanted)
                            if r is None:
                                await self._write(f"{b}.Error", f"Receta {wanted} no existe")
                                self._last.pop(self.nodes[f"{b}.RecipeNumber"][0], None)
                                await self._write(f"{b}.RecipeNumber", int(m.recipe["number"]))
                                self._recipe_req[m.name] = int(m.recipe["number"])
                            # si existe, el listener de receta (main.py) reconstruye los nodos
                    reset_node = self.nodes[f"{b}.ResetCounters"][0]
                    if await reset_node.read_value():
                        m.reset_counters()
                        await reset_node.write_value(ua.DataValue(ua.Variant(False, V.Boolean)))
                except Exception:
                    log.exception("Error en control OPC UA de %s", b)
            await asyncio.sleep(0.1)

    async def _heartbeat(self):
        hb = 0
        while True:
            hb = (hb + 1) % 2 ** 32
            try:
                await self._write("Status.Heartbeat", hb)
                st = self.hub.cameras.status()
                await self._write("Status.CamerasOk", all(c["ok"] for c in st if c["enabled"]))
                for c in st:
                    n = self.cam_nodes.get(c["id"])
                    if n:
                        await self._set(n["ok"], c["ok"], V.Boolean)
                        await self._set(n["fps"], c["fps"], V.Double)
            except Exception:
                log.exception("Error en heartbeat")
            await asyncio.sleep(1)

    # ---------------------------------------------------------------- publicación
    def _on_state(self, module, state):
        """Llamado desde el hilo del bloque."""
        if self._loop and self.running:
            asyncio.run_coroutine_threadsafe(self._publish(module, state), self._loop)

    async def _publish(self, m, s):
        if self._build_lock.locked():
            return
        b = m.opc_folder
        try:
            await self._write(f"{b}.Enabled", s["running"])
            await self._write(f"{b}.Alarm", s["alarm"])
            await self._write(f"{b}.Warning", s["warning"])
            await self._write(f"{b}.Level", LEVEL_NUM.get(s["level"], 0))
            await self._write(f"{b}.AlarmText", s.get("text", ""))
            await self._write(f"{b}.Error", s.get("error") or "")
            for name, _, fn in m.opc_summary:
                await self._write(f"{b}.{name}", fn(s))
            nodes = self.dyn.get(m.name, {})
            for t in s["tools"]:
                fields = nodes.get(t["id"])
                if fields:
                    for fname, _, fn in m.opc_tool_fields:
                        node, vtype = fields[fname]
                        await self._set(node, fn(t), vtype)
        except Exception:
            log.exception("Error publicando %s en OPC UA", b)

    async def stop(self):
        self.running = False
        for m in self.hub.modules.values():
            m.remove_listener(self._on_state)
        for t in self._tasks:
            t.cancel()
        if self.server:
            await self.server.stop()
