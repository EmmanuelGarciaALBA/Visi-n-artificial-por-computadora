"""Servidor TCP/IP de texto (ASCII), estilo cámaras inteligentes industriales.

Cada comando termina en CR, LF o CRLF. Respuestas terminan en CRLF.

  TRIGGER  (o T)   -> ejecuta una inspección y responde con el resultado
  GET              -> último resultado sin inspeccionar
  STATUS           -> READY|STOPPED;<camara_ok 0/1>;<modo>   (TRIGGER con STOPPED -> ERR;INSPECTION_STOPPED)
  RESET            -> reinicia el contador
  RECIPE <n>       -> carga la receta número n (o por nombre)  -> RCP;<numero>;<nombre>
  RECIPE?          -> receta activa                            -> RCP;<numero>;<nombre>
  SAFETY           -> SAF;<peligro 0/1>;<advertencia 0/1>;<personas>;<zona1 0/1>;<zona2 0/1>;...
  PING             -> PONG

Formato CSV (separador configurable, por defecto ';'):
  RES;<contador>;<1=OK|0=NOK>;<ciclo_ms>;<herr1>;<herr2>;...
  Cada herramienta envía su valor numérico; los lectores de código envían el texto leído.
Formato JSON: el resultado completo en una línea.

Con push_results=true, cada inspección (y cada cambio en zonas de seguridad) se envía
a todos los clientes conectados.
"""
import asyncio
import json
import logging

log = logging.getLogger("tcp")


class TcpServer:
    def __init__(self, engine, cfg, safety=None):
        self.engine = engine
        self.safety = safety
        self._last_saf = None
        self.cfg = cfg
        self.server = None
        self.clients = set()
        self._loop = None
        self.running = False

    def format_result(self, r):
        c = self.cfg["tcp"]
        if r is None:
            return "ERR;NO_RESULT"
        if c.get("format") == "json":
            return json.dumps(r, ensure_ascii=False)
        sep = c.get("separator", ";")
        fields = ["RES", str(r["counter"]), "1" if r["pass"] else "0", f"{r['cycle_ms']:.2f}"]
        for t in r["tools"]:
            if t["type"] == "code_reader":
                text = t.get("text", "")
                for ch in (sep, "\r", "\n"):
                    text = text.replace(ch, " ")
                fields.append(text)
            else:
                fields.append(f"{t['value']:g}")
        return sep.join(fields)

    def format_safety(self, s):
        if self.cfg["tcp"].get("format") == "json":
            return json.dumps(s, ensure_ascii=False)
        fields = ["SAF", "1" if s["danger"] else "0", "1" if s["warning"] else "0", str(s["persons"])]
        fields += ["1" if z["occupied"] else "0" for z in s["zones"]]
        return self.cfg["tcp"].get("separator", ";").join(fields)

    async def start(self):
        self._loop = asyncio.get_running_loop()
        port = self.cfg["tcp"]["port"]
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", port)
        self.running = True
        self.engine.add_listener(self._on_result)
        if self.safety:
            self.safety.add_listener(self._on_safety)
        log.info("TCP escuchando en el puerto %s", port)

    async def _send(self, writer, line):
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    async def _handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("Cliente TCP conectado: %s", peer)
        self.clients.add(writer)
        buf = b""
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                buf += data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    cmd = raw.decode("utf-8", "ignore").strip()
                    if cmd:
                        await self._send(writer, await self._command(cmd))
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            log.info("Cliente TCP desconectado: %s", peer)

    async def _command(self, line):
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.upper(), arg.strip()
        if cmd in ("T", "TRIGGER"):
            r = await asyncio.get_running_loop().run_in_executor(None, self.engine.inspect, "tcp")
            return self.format_result(r) if r else "ERR;INSPECTION_STOPPED"
        if cmd == "GET":
            return self.format_result(self.engine.last_result)
        if cmd == "STATUS":
            ok = "1" if self.engine.camera.error is None else "0"
            state = "READY" if self.engine.running else "STOPPED"
            return f"{state};{ok};{self.engine.cfg['trigger']['mode']}"
        if cmd == "RESET":
            self.engine.counter = 0
            return "OK"
        if cmd == "RECIPE?" or (cmd == "RECIPE" and not arg):
            r = self.engine.recipe
            return f"RCP;{r['number']};{r['name']}"
        if cmd == "RECIPE":
            r = await asyncio.get_running_loop().run_in_executor(None, self.engine.select_recipe, arg)
            return f"RCP;{r['number']};{r['name']}" if r else f"ERR;RECIPE_NOT_FOUND;{arg}"
        if cmd == "SAFETY":
            if not self.safety:
                return "ERR;SAFETY_UNAVAILABLE"
            return self.format_safety(self.safety.state)
        if cmd == "PING":
            return "PONG"
        return f"ERR;UNKNOWN_COMMAND;{cmd}"

    def _on_result(self, result):
        # Las respuestas a TRIGGER ya se envían al solicitante; el push es para el resto.
        if self.running and self.cfg["tcp"].get("push_results") and result["source"] != "tcp":
            asyncio.run_coroutine_threadsafe(self._broadcast(self.format_result(result)), self._loop)

    def _on_safety(self, state):
        # Solo se empuja cuando cambia algo relevante, no en cada cuadro
        if not (self.running and self.cfg["tcp"].get("push_results")):
            return
        line = self.format_safety(state) if self.cfg["tcp"].get("format") != "json" else None
        key = line or (state["danger"], state["warning"], state["persons"],
                       tuple(z["occupied"] for z in state["zones"]))
        if key != self._last_saf:
            self._last_saf = key
            asyncio.run_coroutine_threadsafe(self._broadcast(self.format_safety(state)), self._loop)

    async def _broadcast(self, line):
        for w in list(self.clients):
            try:
                await self._send(w, line)
            except Exception:
                self.clients.discard(w)

    async def stop(self):
        self.running = False
        self.engine.remove_listener(self._on_result)
        if self.safety:
            self.safety.remove_listener(self._on_safety)
        if self.server:
            self.server.close()
            for w in list(self.clients):
                w.close()
            await self.server.wait_closed()
