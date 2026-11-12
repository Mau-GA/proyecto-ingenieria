"""
Ruta: proyectorestaurante/app/crear_usuarios_prueba.py

Crea o actualiza una cuenta de prueba por cada rol operativo del sistema,
con contraseña encriptada con bcrypt y vinculada a una ficha de empleado.

Cómo ejecutarlo (con los contenedores ya levantados):
    docker compose exec app python crear_usuarios_prueba.py

El script es idempotente: si la cuenta ya existe, actualiza su contraseña,
la reactiva, le asigna el rol indicado y la vuelve a vincular a un empleado
libre (respeta el UNIQUE de usuarios.id_empleado).
"""
from __future__ import annotations

from dataclasses import dataclass

import bcrypt
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import transaccion


@dataclass(frozen=True)
class CuentaPrueba:
    usuario: str
    rol: str
    password: str
    puesto_preferido: str


CUENTAS = (
    CuentaPrueba("gerente1", "Gerente", "us93153", "Gerente"),
    CuentaPrueba("mesero1", "Mesero", "us93154", "Mesero"),
    CuentaPrueba("cajero1", "Caja", "us93155", "Caja"),
    CuentaPrueba("cocina1", "Cocina", "us93156", "Cocinero"),
    CuentaPrueba("bar1", "Bar", "us93157", "Bartender"),
)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _id_rol(conn: Connection, nombre: str) -> int:
    id_rol = conn.execute(
        text("SELECT id_rol FROM roles WHERE nombre = :nombre AND activo = TRUE"),
        {"nombre": nombre},
    ).scalar_one_or_none()
    if id_rol is None:
        raise RuntimeError(f"No existe el rol '{nombre}' en la tabla roles.")
    return int(id_rol)


def _usuario_existente(conn: Connection, nombre_usuario: str) -> dict | None:
    fila = conn.execute(
        text(
            """
            SELECT id_usuario, id_empleado
            FROM usuarios
            WHERE nombre_usuario = :usuario
            """
        ),
        {"usuario": nombre_usuario},
    ).mappings().first()
    return dict(fila) if fila else None


def _elegir_empleado(
    conn: Connection,
    puesto_preferido: str,
    id_usuario_actual: int | None,
    id_empleado_actual: int | None,
) -> dict:
    """
    Elige una ficha de empleado activa que no esté vinculada a OTRO usuario.
    Da prioridad a los empleados cuyo puesto coincide con el rol; si no hay
    disponibles, usa cualquier empleado libre.
    """
    fila = conn.execute(
        text(
            """
            SELECT e.id_empleado, e.nombre, e.apellido, p.nombre AS puesto
            FROM empleados e
            JOIN puestos p ON p.id_puesto = e.id_puesto
            WHERE e.estatus = 'Activo'
              AND (
                    e.id_empleado = :id_empleado_actual
                    OR NOT EXISTS (
                        SELECT 1
                        FROM usuarios u
                        WHERE u.id_empleado = e.id_empleado
                          AND u.id_usuario <> :id_usuario_actual
                    )
              )
            ORDER BY (p.nombre = :puesto_preferido) DESC, e.id_empleado
            LIMIT 1
            """
        ),
        {
            "id_empleado_actual": id_empleado_actual,
            "id_usuario_actual": id_usuario_actual if id_usuario_actual is not None else -1,
            "puesto_preferido": puesto_preferido,
        },
    ).mappings().first()
    if fila is None:
        raise RuntimeError(
            "No hay fichas de empleado activas disponibles para vincular. "
            "Registra más empleados o libera alguna cuenta existente."
        )
    return dict(fila)


def _upsert_usuario(
    conn: Connection, cuenta: CuentaPrueba, id_rol: int, password_hash: str
) -> tuple[dict, str]:
    existente = _usuario_existente(conn, cuenta.usuario)
    id_usuario = int(existente["id_usuario"]) if existente else None
    id_empleado_actual = (
        int(existente["id_empleado"])
        if existente and existente["id_empleado"] is not None
        else None
    )
    empleado = _elegir_empleado(
        conn, cuenta.puesto_preferido, id_usuario, id_empleado_actual
    )

    if existente:
        conn.execute(
            text(
                """
                UPDATE usuarios
                SET id_empleado = :id_empleado,
                    password_hash = :password_hash,
                    activo = TRUE
                WHERE id_usuario = :id_usuario
                """
            ),
            {
                "id_empleado": empleado["id_empleado"],
                "password_hash": password_hash,
                "id_usuario": id_usuario,
            },
        )
        accion = "actualizado"
    else:
        resultado = conn.execute(
            text(
                """
                INSERT INTO usuarios (id_empleado, nombre_usuario, password_hash, activo)
                VALUES (:id_empleado, :usuario, :password_hash, TRUE)
                RETURNING id_usuario
                """
            ),
            {
                "id_empleado": empleado["id_empleado"],
                "usuario": cuenta.usuario,
                "password_hash": password_hash,
            },
        )
        id_usuario = int(resultado.scalar_one())
        accion = "creado"

    conn.execute(
        text("DELETE FROM usuarios_roles WHERE id_usuario = :id_usuario"),
        {"id_usuario": id_usuario},
    )
    conn.execute(
        text(
            """
            INSERT INTO usuarios_roles (id_usuario, id_rol)
            VALUES (:id_usuario, :id_rol)
            """
        ),
        {"id_usuario": id_usuario, "id_rol": id_rol},
    )

    empleado["accion"] = accion
    empleado["id_usuario"] = id_usuario
    return empleado, accion


def main() -> None:
    resumen: list[dict] = []

    with transaccion() as conn:
        for cuenta in CUENTAS:
            id_rol = _id_rol(conn, cuenta.rol)
            password_hash = _hash_password(cuenta.password)
            empleado, accion = _upsert_usuario(conn, cuenta, id_rol, password_hash)

            resumen.append(
                {
                    "Usuario": cuenta.usuario,
                    "Rol": cuenta.rol,
                    "Contraseña": cuenta.password,
                    "Empleado": (
                        f"{empleado['nombre']} {empleado['apellido']} "
                        f"({empleado['puesto']}, id {empleado['id_empleado']})"
                    ),
                    "Acción": accion,
                }
            )

    print("\nUsuarios de prueba listos:\n")
    print(pd.DataFrame(resumen).to_string(index=False))
    print(
        "\nYa puedes iniciar sesión en la aplicación con cualquiera de estas cuentas."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"No se pudieron crear los usuarios de prueba: {exc}") from exc
