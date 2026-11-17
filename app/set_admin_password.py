"""
Utilidad de una sola vez: reemplaza el hash placeholder del usuario 'admin'
(sembrado en db/seed.sql) por un hash bcrypt real generado a partir de una
contraseña que tú eliges.

Cómo ejecutarlo (con los contenedores ya levantados):
    docker compose exec app python set_admin_password.py

Te pedirá la nueva contraseña por teclado (no queda en el historial de bash).
"""
import getpass

import bcrypt

from database import execute

if __name__ == "__main__":
    password = getpass.getpass("Nueva contraseña para el usuario 'admin': ")
    confirmacion = getpass.getpass("Confírmala: ")

    if password != confirmacion:
        raise SystemExit("Las contraseñas no coinciden. Nada fue actualizado.")
    if len(password) < 8:
        raise SystemExit("Usa al menos 8 caracteres.")

    nuevo_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    execute("UPDATE usuarios SET password_hash = :h WHERE nombre_usuario = 'admin'", {"h": nuevo_hash})
    print("Listo: la contraseña del usuario 'admin' fue actualizada.")
