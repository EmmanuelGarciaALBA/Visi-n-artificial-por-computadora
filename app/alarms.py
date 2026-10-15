"""Alarmas de los 4 bloques: historial de eventos y envío de correos.

Cada vez que cambia el nivel de alarma de un bloque (ninguna / advertencia / peligro)
se registra un evento (el regreso a "ninguna" solo si dura CLEAR_S segundos). Los eventos se guardan en logs/alarmas.csv (se abre con Excel)
y los últimos se muestran en la interfaz.

Correo: se envía al subir de nivel (si alcanza el nivel mínimo configurado en el bloque),
como máximo uno cada `cooldown_min` minutos por bloque, con la imagen anotada adjunta.
Opcionalmente también cuando la alarma se normaliza.
"""
import csv
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2

from . import notify
from .config import BASE_DIR

log = logging.getLogger("alarms")

LOG_PATH = Path(os.environ.get("VS_LOGS") or BASE_DIR / "logs") / "alarmas.csv"
RANK = {"ninguna": 0, "advertencia": 1, "peligro": 2}
LEVEL_TEXT = {"ninguna": "NORMAL", "advertencia": "ADVERTENCIA", "peligro": "PELIGRO"}
CLEAR_S = 3.0  # tiempo sin alarma para registrar que se normalizó
FIELDS = ["id", "fecha", "bloque", "nivel", "tipo", "texto", "correo"]


class AlarmManager:
    def __init__(self, hub):
        self.hub = hub
        self.events = deque(maxlen=400)
        self._prev = {}  # bloque -> (nivel, texto)
        self._last_email = {}  # bloque -> time.time() del último correo de alarma
        self._clear_since = {}  # bloque -> desde cuándo está sin alarma (aún no registrado)
        self._emailed = {}  # bloque -> se envió correo de la alarma en curso (para avisar al normalizar)
        self._listeners = []
        self._lock = threading.Lock()
        self._seq = 0
        self._load_history()

    def add_listener(self, fn):
        """fn(event) en cada evento nuevo o actualizado (p. ej. resultado del correo)."""
        self._listeners.append(fn)

    def _notify(self, ev):
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("Error notificando evento")

    # ---------------------------------------------------------------- historial
    def _load_history(self):
        if not LOG_PATH.exists():
            return
        try:
            with open(LOG_PATH, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))[-self.events.maxlen:]
            for r in rows:
                self.events.append({"id": int(r["id"]), "time": r["fecha"], "module": r["bloque"],
                                    "title": self._title(r["bloque"]), "level": r["nivel"], "kind": r["tipo"],
                                    "text": r["texto"], "email": r.get("correo") or None})
            self._seq = max((e["id"] for e in self.events), default=0)
        except Exception:
            log.exception("No se pudo leer el historial de alarmas")

    def _write_csv(self, ev):
        try:
            LOG_PATH.parent.mkdir(exist_ok=True)
            new = not LOG_PATH.exists()
            with open(LOG_PATH, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(FIELDS)
                w.writerow([ev["id"], ev["time"], ev["module"], ev["level"], ev["kind"], ev["text"], ev["email"] or ""])
        except Exception:
            log.exception("No se pudo escribir logs/alarmas.csv")

    def _title(self, name):
        m = self.hub.modules.get(name) if getattr(self.hub, "modules", None) else None
        return m.title if m else name

    def list(self, module=None, limit=100):
        evs = [e for e in self.events if module in (None, "", e["module"])]
        return list(reversed(evs))[:limit]

    def _add(self, module, level, kind, text):
        with self._lock:
            self._seq += 1
            ev = {"id": self._seq, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "module": module.name,
                  "title": module.title, "level": level, "kind": kind, "text": text, "email": None}
            self.events.append(ev)
        return ev

    # ---------------------------------------------------------------- evaluación
    def update(self, module, state):
        """Lo llama cada bloque después de cada ciclo."""
        name = module.name
        level = state["level"] if state["running"] else "ninguna"
        text = state.get("text") or ""
        prev_level, prev_text = self._prev.get(name, ("ninguna", ""))
        now = time.time()
        if level == "ninguna" and prev_level != "ninguna" and state["running"]:
            # Se registra "normalizado" solo si se mantiene CLEAR_S: un parpadeo no llena el historial
            since = self._clear_since.setdefault(name, now)
            if now - since < CLEAR_S:
                return
        self._clear_since.pop(name, None)
        if level == prev_level:
            if level != "ninguna":
                self._prev[name] = (level, text or prev_text)
            return
        self._prev[name] = (level, text)
        ecfg = module.cfg["alarm"]["email"]
        send_mail = False
        if level != "ninguna":
            kind = "alarma" if RANK[level] > RANK[prev_level] else "cambio"
            ev = self._add(module, level, kind, text or LEVEL_TEXT[level])
            cooldown = float(ecfg.get("cooldown_min", 10)) * 60
            send_mail = (ecfg.get("enabled") and kind == "alarma"
                         and RANK[level] >= RANK.get(ecfg.get("min_level", "peligro"), 2)
                         and time.time() - self._last_email.get(name, 0) >= cooldown)
            if send_mail:
                self._last_email[name] = time.time()
                self._emailed[name] = True
        else:
            why = "bloque detenido" if not state["running"] else "normalizado"
            ev = self._add(module, "ninguna", "normalizado",
                           why.capitalize() + (f" · antes: {prev_text}" if prev_text else ""))
            send_mail = ecfg.get("enabled") and ecfg.get("on_clear") and self._emailed.pop(name, False)
        if send_mail and notify.recipients(ecfg.get("to")):
            ev["email"] = "enviando…"
            self._notify(ev)
            self._email(module, state, ev, ecfg)
        else:
            self._write_csv(ev)
            self._notify(ev)
        log.info("Alarma %s: %s %s", module.title, LEVEL_TEXT[ev["level"]], ev["text"])

    # ---------------------------------------------------------------- correo
    def _snapshot(self, module, state):
        cams = [cid for cid, c in state.get("cameras", {}).items() if c.get("level") != "ninguna"] \
            or list(state.get("cameras", {}))
        for cid in cams:
            _, img = self.hub.cameras.latest(cid)
            if img is not None:
                ok, buf = cv2.imencode(".jpg", module.annotate(cid, img), [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    return buf.tobytes()
        return None

    def _message(self, module, state, ev):
        cams = ", ".join(c["name"] for c in state.get("cameras", {}).values()) or "-"
        rec = state.get("recipe", {})
        urls = "\n".join(self.hub.web_urls())
        head = "ALARMA NORMALIZADA" if ev["level"] == "ninguna" else f"ALARMA DE {LEVEL_TEXT[ev['level']]}"
        subject = f"[VisionServer] {head} · {module.title}: {ev['text'][:90]}"
        body = (f"VisionServer - {head}\n\n"
                f"Bloque:   {module.title}\n"
                f"Fecha:    {ev['time']}\n"
                f"Detalle:  {ev['text']}\n"
                f"Receta:   {rec.get('number', '')} · {rec.get('name', '')}\n"
                f"Cámaras:  {cams}\n\n"
                f"Interfaz web:\n{urls}\n\n"
                "Mensaje automático. Función de asistencia: no sustituye los sistemas de seguridad certificados.")
        return subject, body

    def _email(self, module, state, ev, ecfg):
        subject, body = self._message(module, state, ev)
        image = self._snapshot(module, state) if ecfg.get("attach_image", True) else None

        def done(err):
            ev["email"] = f"error: {err}" if err else f"enviado a {ecfg.get('to')}"
            self._write_csv(ev)
            self._notify(ev)

        notify.send_async(self.hub.cfg["email"], ecfg.get("to"), subject, body, image, done)

    def test_email(self, module, to=None):
        """Envía un correo de prueba (bloqueante). Devuelve los destinatarios."""
        ecfg = module.cfg["alarm"]["email"] if module else {}
        to = to or ecfg.get("to")
        if not notify.recipients(to):
            raise RuntimeError("Escribe al menos un destinatario")
        title = module.title if module else "Sistema"
        state = module.state if module else {}
        image = self._snapshot(module, state) if module and ecfg.get("attach_image", True) else None
        notify.send(self.hub.cfg["email"], to, f"[VisionServer] Correo de prueba · {title}",
                    f"Este es un correo de prueba del bloque {title} de VisionServer.\n\n"
                    f"Si lo recibiste, las alarmas por correo están listas.\n\n" + "\n".join(self.hub.web_urls()),
                    image)
        return notify.recipients(to)
