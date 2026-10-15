"""Servidor TCP/IP de texto (ASCII), estilo cámaras inteligentes industriales.

Cada comando termina en CR, LF o CRLF. Respuestas terminan en CRLF. Separador configurable (';').
<bloque> = SAFETY (o ZONAS) | EPP | ANDON | STOCK

  SAFETY           -> SAF;<peligro 0/1>;<advertencia 0/1>;<personas>;<zona1 0/1>;<zona2 0/1>;...
  EPP              -> EPP;<alarma 0/1>;<personas>;<sin_epp>;<sin_casco>;<sin_guantes>;<sin_lentes>;<area1 ok 0/1>;...
  ANDON            -> AND;<encendidas>;<rojas>;<amarillas>;<verdes>;<torreta1>;<torreta2>;...
                      cada torreta: letras de las luces encendidas R=rojo A=amarillo V=verde, '-' = apagada
  STOCK            -> STK;<alarma 0/1>;<cajas>;<racks>;<pos1 cajas:estado>;...   estado: OK|BAJO|VACIO|EXCESO|SINRACK
  (bloque detenido -> ERR;STOPPED;<prefijo>)
  STATUS           -> STA;<zonas 0/1>;<epp 0/1>;<andon 0/1>;<stock 0/1>;<cámaras con imagen>/<cámaras>
  RECIPE <bloque> <n>  -> RCP;<prefijo>;<n>;<nombre>   (o ERR;RECIPE_NOT_FOUND;<n>)
  RECIPE <bloque>      -> receta activa
  RESET <bloque>       -> reinicia contadores -> OK
  PING                 -> PONG
Formato JSON: el estado completo del bloque en una línea.

Con push_results=true, cada cambio en la respuesta de un bloque se envía a todos los clientes.
"""
import asyncio
import json
import logging

log = logging.getLogger("tcp")


class TcpServer:
    def __init__(self, hub):
        self.hub = hub
        self.server = None
        self.clients = set()
        self._loop = None
        self._last = {}
        self.running = False
        self.error = None

    @property
    def cfg(self):
        return self.hub.cfg

    def _clean(self, text):
        for ch in (self.cfg["tcp"].get("separator", ";"), "\r", "\n"):
            text = text.replace(ch, " ")
        return text

    def format_state(self, m, s=None):
        s = s or m.state
        if self.cfg["tcp"].get("format") == "json":
            return json.dumps(s, ensure_ascii=False, default=str)
        sep = self.cfg["tcp"].get("separator", ";")
        if not s["running"]:
            return sep.join(["ERR", "STOPPED", m.tcp_prefix])
        return sep.join([m.tcp_prefix] + [self._clean(str(f)) for f in m.tcp_fields(s)])

    async def start(self):
        self._loop = asyncio.get_running_loop()
        port = self.cfg["tcp"]["port"]
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", port)
        self.running = True
        for m in self.hub.modules.values():
            m.add_listener(self._on_state)
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
        sep = self.cfg["tcp"].get("separator", ";")
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        if cmd == "PING":
            return "PONG"
        if cmd == "STATUS":
            mods = [("1" if self.hub.modules[n].running else "0") for n in ("safety", "ppe", "andon", "stock")]
            cams = [c for c in self.hub.cameras.status() if c["enabled"]]
            return sep.join(["STA", *mods, f"{sum(1 for c in cams if c['ok'])}/{len(cams)}"])
        if cmd in ("RECIPE", "RECIPE?", "RESET"):
            m = self.hub.module(args[0].rstrip("?")) if args else None
            if m is None:
                return sep.join(["ERR", "UNKNOWN_BLOCK", args[0] if args else ""])
            if cmd == "RESET":
                m.reset_counters()
                return "OK"
            if len(args) < 2 or cmd == "RECIPE?" or args[0].endswith("?"):
                r = m.recipe
                return sep.join(["RCP", m.tcp_prefix, str(r["number"]), self._clean(r["name"])])
            key = " ".join(args[1:])
            r = await asyncio.get_running_loop().run_in_executor(None, m.select_recipe, key)
            if r is None:
                return sep.join(["ERR", "RECIPE_NOT_FOUND", key])
            return sep.join(["RCP", m.tcp_prefix, str(r["number"]), self._clean(r["name"])])
        m = self.hub.module(cmd)
        if m is not None:
            return self.format_state(m)
        return sep.join(["ERR", "UNKNOWN_COMMAND", cmd])

    def _on_state(self, module, state):
        # Solo se empuja cuando cambia la respuesta del bloque, no en cada ciclo
        if not (self.running and self.cfg["tcp"].get("push_results")):
            return
        if self.cfg["tcp"].get("format") == "json":
            key = (state["running"], state["level"], json.dumps(state.get("summary"), sort_keys=True),
                   json.dumps([{k: v for k, v in t.items() if k not in ("activations",)} for t in state["tools"]],
                              sort_keys=True, default=str))
        else:
            key = self.format_state(module, state)
        if key != self._last.get(module.name):
            self._last[module.name] = key
            asyncio.run_coroutine_threadsafe(self._broadcast(self.format_state(module, state)), self._loop)

    async def _broadcast(self, line):
        for w in list(self.clients):
            try:
                await self._send(w, line)
            except Exception:
                self.clients.discard(w)

    async def stop(self):
        self.running = False
        for m in self.hub.modules.values():
            m.remove_listener(self._on_state)
        if self.server:
            self.server.close()
            for w in list(self.clients):
                w.close()
            await self.server.wait_closed()
