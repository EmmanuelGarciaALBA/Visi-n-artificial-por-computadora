"""Administración de usuarios de la interfaz web.

  python tools/usuarios.py listar
  python tools/usuarios.py agregar <usuario> [admin|operador]    (pide la contraseña)
  python tools/usuarios.py eliminar <usuario>
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.auth import ROLES, load_users, save_users, set_user  # noqa: E402


def main(argv):
    if not argv or argv[0] == "listar":
        for name, u in load_users().items():
            print(f"{name:20} {u['role']}")
        return
    cmd, name = argv[0], argv[1]
    if cmd == "agregar":
        role = argv[2] if len(argv) > 2 else "admin"
        if role not in ROLES:
            sys.exit(f"Rol inválido: {role}")
        # VS_PASSWORD permite automatizar sin que la contraseña quede en el historial
        pwd = os.environ.get("VS_PASSWORD") or getpass.getpass(f"Contraseña para {name}: ")
        if len(pwd) < 6:
            sys.exit("La contraseña debe tener al menos 6 caracteres")
        set_user(name, pwd, role)
        print(f"Usuario '{name}' guardado ({role})")
    elif cmd == "eliminar":
        users = load_users()
        if users.pop(name, None) is None:
            sys.exit(f"No existe el usuario '{name}'")
        save_users(users)
        print(f"Usuario '{name}' eliminado")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
