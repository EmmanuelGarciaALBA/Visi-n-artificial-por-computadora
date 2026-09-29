"""Usuarios y sesiones de la interfaz web.

- Las contraseñas se guardan con hash PBKDF2-SHA256 + sal en users.json (nunca en texto plano).
- La sesión es una cookie firmada con HMAC (clave en secret.key), válida SESSION_HOURS horas.
- Administrar usuarios:  python tools/usuarios.py
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

from .config import BASE_DIR

USERS_PATH = BASE_DIR / "users.json"
SECRET_PATH = BASE_DIR / "secret.key"
COOKIE_NAME = "vs_session"
SESSION_HOURS = 12
ROLES = ("admin", "operador")
_ITER = 200_000
_lock = threading.Lock()


def _secret() -> bytes:
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except ValueError:
        return False


def load_users() -> dict:
    with _lock:
        if not USERS_PATH.exists():
            return {}
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))


def save_users(users: dict) -> None:
    with _lock:
        tmp = USERS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(USERS_PATH)


def set_user(username: str, password: str, role: str = "admin") -> None:
    if role not in ROLES:
        raise ValueError(f"Rol inválido: {role} (usa {', '.join(ROLES)})")
    users = load_users()
    users[username] = {"hash": hash_password(password), "role": role}
    save_users(users)


def authenticate(username: str, password: str):
    """Devuelve el rol si usuario/contraseña son correctos, si no None.
    El usuario no distingue mayúsculas; la contraseña sí."""
    users = load_users()
    for name, u in users.items():
        if name.lower() == username.strip().lower() and _check_hash(password, u["hash"]):
            return name, u["role"]
    _check_hash(password, hash_password("x"))  # tiempo similar si el usuario no existe
    return None


def make_token(username: str, role: str) -> str:
    payload = f"{username}|{role}|{int(time.time()) + SESSION_HOURS * 3600}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_token(token: str):
    """Devuelve {"user", "role"} si la cookie es válida y vigente, si no None."""
    try:
        username, role, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 3)
    except Exception:
        return None
    payload = f"{username}|{role}|{exp}"
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or int(exp) < time.time():
        return None
    if username not in load_users():  # usuario eliminado -> sesión inválida
        return None
    return {"user": username, "role": role}
