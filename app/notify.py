"""Envío de alarmas por correo electrónico (SMTP).

Proveedores:
  gmail    smtp.gmail.com:587 STARTTLS. Requiere verificación en 2 pasos y una
           "contraseña de aplicación" (myaccount.google.com/apppasswords), no la contraseña normal.
  outlook  smtp.office365.com:587 STARTTLS. Microsoft 365 / Outlook.com. La cuenta debe tener
           habilitado "SMTP autenticado" (el administrador de Microsoft 365 lo activa por buzón).
  smtp     servidor de la empresa: servidor, puerto, seguridad (starttls | ssl | ninguna), usuario.
Los correos se envían en un hilo aparte: una falla de red nunca detiene la inspección.
"""
import logging
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

log = logging.getLogger("email")

PROVIDERS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
    "outlook": {"host": "smtp.office365.com", "port": 587, "security": "starttls"},
}

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="email")


def server_settings(ecfg):
    """(host, port, security, user, password, from_address) según el proveedor."""
    prov = ecfg.get("provider", "gmail")
    address = ecfg.get("address", "").strip()
    if prov in PROVIDERS:
        p = PROVIDERS[prov]
        return p["host"], p["port"], p["security"], address, ecfg.get("password", ""), address
    return (ecfg.get("host", "").strip(), int(ecfg.get("port") or 587), ecfg.get("security", "starttls"),
            ecfg.get("user", "").strip(), ecfg.get("password", ""), address)


def recipients(text):
    return [a.strip() for a in str(text or "").replace(";", ",").split(",") if a.strip()]


def is_configured(ecfg):
    host, _, _, _, _, sender = server_settings(ecfg)
    return bool(host and sender)


def send(ecfg, to, subject, body, image=None, image_name="imagen.jpg"):
    """Envía el correo (bloqueante). Lanza una excepción con un mensaje claro si falla."""
    host, port, security, user, password, sender = server_settings(ecfg)
    if not host or not sender:
        raise RuntimeError("Falta configurar el correo (pestaña Sistema → Correo)")
    to = recipients(to) if isinstance(to, str) else to
    if not to:
        raise RuntimeError("No hay destinatarios")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((ecfg.get("from_name") or "VisionServer", sender))
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid(domain="visionserver.local")
    msg.set_content(body)
    if image:
        msg.add_attachment(image, maintype="image", subtype="jpeg", filename=image_name)
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
        with smtp:
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        hint = {"gmail": " (Gmail: usa una contraseña de aplicación)",
                "outlook": " (Outlook: la cuenta debe tener habilitado SMTP autenticado)"}
        raise RuntimeError(f"Usuario o contraseña rechazados por el servidor{hint.get(ecfg.get('provider'), '')}: "
                           f"{e.smtp_code} {e.smtp_error.decode(errors='ignore')[:160]}") from None
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"No se pudo enviar el correo por {host}:{port}: {e}") from None
    log.info("Correo enviado a %s: %s", ", ".join(to), subject)


def send_async(ecfg, to, subject, body, image=None, done=None):
    """Envía en segundo plano. done(error: str | None) al terminar."""
    def job():
        try:
            send(ecfg, to, subject, body, image)
            err = None
        except Exception as e:
            log.warning("Correo no enviado: %s", e)
            err = str(e)
        if done:
            try:
                done(err)
            except Exception:
                log.exception("Error tras enviar correo")
    _pool.submit(job)
