"""Humo de pantallas: cada módulo debe renderizar sin excepciones."""
from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import text
from streamlit.testing.v1 import AppTest

from database import get_engine

RAIZ = Path(__file__).resolve().parents[1]
APP = RAIZ / "app.py"
if not APP.exists():
    APP = RAIZ / "app" / "app.py"

MODULOS_ADMIN_GERENTE = [
    "foodhouse",
    "mesas",
    "comandas",
    "ordenes",
    "mermas",
    "productos",
    "inventario",
    "compras",
    "usuarios",
    "reportes",
]
MODULOS_MESERO = ["mesas", "ordenes", "mermas"]
MODULOS_COCINA = ["comandas", "mermas"]
MODULOS_CAJA = ["foodhouse", "ordenes", "mermas"]


def _hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=4)
    ).decode("utf-8")


def _id_rol(nombre: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT id_rol FROM roles WHERE nombre = :n"),
                {"n": nombre},
            ).scalar_one()
        )


def _crear_usuario(nombre: str, rol: str, id_empleado: int) -> dict:
    with get_engine().begin() as conn:
        id_usuario = conn.execute(
            text(
                """
                INSERT INTO usuarios
                    (id_empleado, nombre_usuario, password_hash, activo)
                VALUES (:empleado, :nombre, :hash, TRUE)
                RETURNING id_usuario
                """
            ),
            {
                "empleado": int(id_empleado),
                "nombre": nombre,
                "hash": _hash("ClavePrueba123"),
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO usuarios_roles (id_usuario, id_rol) "
                "VALUES (:usuario, :rol)"
            ),
            {"usuario": int(id_usuario), "rol": _id_rol(rol)},
        )
    return {
        "id_usuario": int(id_usuario),
        "nombre_usuario": nombre,
        "roles": [rol],
        "id_empleado": int(id_empleado),
    }


def _auth_admin() -> dict:
    with get_engine().connect() as conn:
        fila = conn.execute(
            text(
                """
                SELECT u.id_usuario, u.id_empleado, r.nombre AS rol
                FROM usuarios u
                JOIN usuarios_roles ur ON ur.id_usuario = u.id_usuario
                JOIN roles r ON r.id_rol = ur.id_rol
                WHERE u.nombre_usuario = 'admin'
                """
            )
        ).mappings().all()
    return {
        "id_usuario": int(fila[0]["id_usuario"]),
        "nombre_usuario": "admin",
        "roles": [str(f["rol"]) for f in fila],
        "id_empleado": int(fila[0]["id_empleado"]),
    }


def _validar_modulos(auth: dict, modulos: list[str]) -> None:
    for modulo in modulos:
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.session_state["auth"] = auth
        at.session_state["modulo_activo"] = modulo
        at.run()
        assert at.session_state["modulo_activo"] == modulo, (
            f"{auth['nombre_usuario']} no abrió {modulo}"
        )
        assert len(at.exception) == 0, (
            f"{auth['nombre_usuario']} falló en {modulo}: "
            f"{[e.value for e in at.exception]}"
        )


def test_admin_recorre_todos_los_modulos():
    _validar_modulos(_auth_admin(), MODULOS_ADMIN_GERENTE)


def test_gerente1_recorre_todos_los_modulos():
    _validar_modulos(
        _crear_usuario("gerente1", "Gerente", 1),
        MODULOS_ADMIN_GERENTE,
    )


def test_mesero1_abre_sus_modulos():
    _validar_modulos(
        _crear_usuario("mesero1", "Mesero", 2),
        MODULOS_MESERO,
    )


def test_cocina1_abre_sus_modulos():
    _validar_modulos(
        _crear_usuario("cocina1", "Cocina", 3),
        MODULOS_COCINA,
    )


def test_cajero1_abre_sus_modulos():
    _validar_modulos(
        _crear_usuario("cajero1", "Caja", 4),
        MODULOS_CAJA,
    )


def test_ordenes_con_cuentas_abiertas_no_falla():
    """Regresión: con cuentas abiertas de mesa y de delivery, Órdenes no debe
    anidar expanders (StreamlitAPIException: Expanders may not be nested...)."""
    from decimal import Decimal

    from services.ventas import guardar_orden_mesa, registrar_comanda

    auth = _auth_admin()
    partida = {
        "id_producto": 1,
        "cantidad": 1,
        "precio_unitario": Decimal("120"),
        "id_area_preparacion": 1,
    }
    guardar_orden_mesa(
        id_mesa=1,
        partidas=[dict(partida)],
        id_usuario=auth["id_usuario"],
        id_empleado=auth["id_empleado"],
    )
    with get_engine().connect() as conn:
        id_tipo = conn.execute(
            text("SELECT id_tipo_orden FROM tipos_orden WHERE nombre = 'Plataforma Externa'")
        ).scalar_one()
        id_plataforma = conn.execute(
            text("SELECT MIN(id_plataforma) FROM plataformas_externas")
        ).scalar_one()
    registrar_comanda(
        partidas=[dict(partida)],
        id_tipo_orden=id_tipo,
        id_usuario=auth["id_usuario"],
        id_empleado=auth["id_empleado"],
        id_plataforma=id_plataforma,
        referencia_externa="PRUEBA-1",
        nombre_cliente="Cliente Prueba",
    )
    _validar_modulos(auth, ["ordenes"])
