"""Pruebas de la Fase 5: datos fuera de auditoría, bitácora y cancelación."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import bcrypt
import pytest
from sqlalchemy import text

from auth import _validar_credenciales
from database import get_engine, transaccion
from services.bitacora import registrar_evento, verificar_bitacora
from services.cancelaciones import CancelacionError, cancelar_orden
from services.cocina import cambiar_estado_partida
from services.ventas import (
    VentaError,
    cobrar_orden_mesa,
    guardar_orden_mesa,
    registrar_comanda,
)

PRODUCTO = {
    "id_producto": 1,
    "cantidad": 1,
    "precio_unitario": 120.00,
    "id_area_preparacion": 1,
}
PASSWORD_ADMIN = "ClaveAdmin123"


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None) -> list:
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=4)
    ).decode("utf-8")


def _preparar_admin(password: str = PASSWORD_ADMIN) -> None:
    _ejecutar(
        "UPDATE usuarios SET password_hash = :hash WHERE nombre_usuario = 'admin'",
        {"hash": _hash(password)},
    )


def _crear_usuario(nombre: str, password: str, ids_roles: list[int]) -> int:
    with get_engine().begin() as conn:
        id_usuario = conn.execute(
            text(
                """
                INSERT INTO usuarios (nombre_usuario, password_hash, activo)
                VALUES (:nombre, :hash, TRUE)
                RETURNING id_usuario
                """
            ),
            {"nombre": nombre, "hash": _hash(password)},
        ).scalar_one()
        for id_rol in ids_roles:
            conn.execute(
                text(
                    "INSERT INTO usuarios_roles (id_usuario, id_rol) "
                    "VALUES (:u, :r)"
                ),
                {"u": int(id_usuario), "r": int(id_rol)},
            )
    return int(id_usuario)


def _crear_cuenta(mesa: int = 1, cantidad: int = 1):
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=[{**PRODUCTO, "cantidad": cantidad}],
        id_usuario=1,
        id_empleado=1,
    )
    id_orden = int(resultado["id_orden"])
    ids = [
        int(fila[0])
        for fila in _filas(
            "SELECT id_detalle FROM detalles_orden WHERE id_orden = :o "
            "ORDER BY id_detalle",
            {"o": id_orden},
        )
    ]
    return id_orden, ids


def _estado_orden(id_orden: int) -> str:
    return str(
        _escalar(
            """
            SELECT e.nombre
            FROM ordenes o JOIN estados_orden e ON e.id_estado = o.id_estado
            WHERE o.id_orden = :o
            """,
            {"o": id_orden},
        )
    )


def _stock(ids_insumos: list[int]) -> dict[int, Decimal]:
    filas = _filas(
        "SELECT id_insumo, stock_actual FROM insumos "
        "WHERE id_insumo = ANY(CAST(:ids AS integer[]))",
        {"ids": ids_insumos},
    )
    return {int(f[0]): Decimal(str(f[1])) for f in filas}


def _activar_para_cobro(id_detalle: int) -> None:
    cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)
    cambiar_estado_partida(id_detalle, "En preparación", "Listo", 1)


def test_auditoria_eliminada_y_tablas_nuevas_existen():
    assert _escalar("SELECT to_regclass('public.auditoria')") is None
    for tabla in (
        "detalle_notas",
        "detalle_consumo",
        "detalle_mermas",
        "detalle_reposiciones",
        "mermas_pendientes",
        "bitacora",
        "motivos_cancelacion",
        "cancelaciones",
    ):
        assert _escalar(
            "SELECT to_regclass(:tabla)", {"tabla": f"public.{tabla}"}
        ) is not None


def test_bitacora_encadena_y_bloquea_update_delete_truncate():
    id1 = registrar_evento(
        None,
        tipo_evento="USUARIO",
        entidad="prueba",
        id_entidad=1,
        datos_nuevos={"n": 1},
    )
    id2 = registrar_evento(
        None,
        tipo_evento="USUARIO",
        entidad="prueba",
        id_entidad=2,
        datos_nuevos={"n": 2},
    )
    filas = _filas(
        "SELECT id_bitacora, hash_anterior, hash FROM bitacora "
        "ORDER BY id_bitacora"
    )
    assert int(filas[0][0]) == id1
    assert filas[0][1] is None
    assert filas[0][2] is not None
    assert int(filas[1][0]) == id2
    assert filas[1][1] == filas[0][2]
    assert filas[1][2] != filas[0][2]
    assert verificar_bitacora() is None

    with pytest.raises(Exception, match="solo lectura"):
        _ejecutar("UPDATE bitacora SET entidad = 'alterada'")
    with pytest.raises(Exception, match="solo lectura"):
        _ejecutar("DELETE FROM bitacora")
    with pytest.raises(Exception, match="solo lectura"):
        _ejecutar("TRUNCATE bitacora")
    assert _escalar("SELECT COUNT(*) FROM bitacora") == 2


def test_alterar_datos_detecta_primer_id_inconsistente():
    id1 = registrar_evento(
        None, tipo_evento="USUARIO", entidad="prueba", datos_nuevos={"n": 1}
    )
    id2 = registrar_evento(
        None, tipo_evento="USUARIO", entidad="prueba", datos_nuevos={"n": 2}
    )
    registrar_evento(
        None, tipo_evento="USUARIO", entidad="prueba", datos_nuevos={"n": 3}
    )

    _ejecutar("ALTER TABLE bitacora DISABLE TRIGGER trg_bitacora_update_delete")
    try:
        _ejecutar(
            "UPDATE bitacora SET datos_nuevos = CAST(:d AS jsonb) "
            "WHERE id_bitacora = :id",
            {"d": '{"n": 99}', "id": id2},
        )
    finally:
        _ejecutar("ALTER TABLE bitacora ENABLE TRIGGER trg_bitacora_update_delete")

    assert verificar_bitacora() == id2
    assert id1 < id2


def test_alterar_hash_detecta_fila():
    id1 = registrar_evento(
        None, tipo_evento="USUARIO", entidad="prueba", datos_nuevos={"n": 1}
    )
    registrar_evento(
        None, tipo_evento="USUARIO", entidad="prueba", datos_nuevos={"n": 2}
    )

    _ejecutar("ALTER TABLE bitacora DISABLE TRIGGER trg_bitacora_update_delete")
    try:
        _ejecutar(
            "UPDATE bitacora SET hash = :hash WHERE id_bitacora = :id",
            {"hash": "0" * 64, "id": id1},
        )
    finally:
        _ejecutar("ALTER TABLE bitacora ENABLE TRIGGER trg_bitacora_update_delete")

    assert verificar_bitacora() == id1


def test_inserciones_concurrentes_mantienen_cadena():
    total = 10
    barrera = Barrier(total)

    def _insertar(i: int) -> int:
        barrera.wait()
        return registrar_evento(
            None,
            tipo_evento="USUARIO",
            entidad="concurrente",
            id_entidad=i,
            datos_nuevos={"i": i},
        )

    with ThreadPoolExecutor(max_workers=total) as pool:
        ids = list(pool.map(_insertar, range(total)))

    assert len(set(ids)) == total
    filas = _filas(
        "SELECT id_bitacora, hash_anterior, hash FROM bitacora "
        "ORDER BY id_bitacora"
    )
    assert len(filas) == total
    assert filas[0][1] is None
    for anterior, siguiente in zip(filas, filas[1:]):
        assert siguiente[1] == anterior[2]
    assert verificar_bitacora() is None


def test_pago_exitoso_deja_un_evento_y_fallido_no_deja_extra():
    id_orden, ids = _crear_cuenta()
    _activar_para_cobro(ids[0])

    cobrar_orden_mesa(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        id_usuario=1,
    )
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PAGO'"
    ) == 1

    with pytest.raises(VentaError):
        cobrar_orden_mesa(
            id_orden=id_orden,
            id_mesa=1,
            id_metodo_pago=1,
            id_usuario=1,
        )
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PAGO'"
    ) == 1

    with pytest.raises(RuntimeError):
        with transaccion() as conn:
            registrar_evento(
                conn,
                tipo_evento="PAGO",
                entidad="pagos",
                id_entidad=999,
                id_usuario=1,
            )
            raise RuntimeError("falla simulada")
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PAGO'"
    ) == 1


def test_login_correcto_y_fallido_sin_guardar_password():
    _preparar_admin()

    assert _validar_credenciales("admin", "incorrecta") is None
    sesion = _validar_credenciales("admin", PASSWORD_ADMIN)
    assert sesion is not None
    assert sesion["id_usuario"] == 1

    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'LOGIN_FALLIDO'"
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'LOGIN'"
    ) == 1
    assert _escalar(
        """
        SELECT COUNT(*)
        FROM bitacora
        WHERE COALESCE(datos_anteriores::text, '') LIKE '%ClaveAdmin123%'
           OR COALESCE(datos_nuevos::text, '') LIKE '%ClaveAdmin123%'
           OR COALESCE(datos_anteriores::text, '') LIKE '%$2b$%'
           OR COALESCE(datos_nuevos::text, '') LIKE '%$2b$%'
        """
    ) == 0


def test_cancelacion_rechaza_mesero_y_password_incorrecta_sin_cambiar_orden():
    _preparar_admin()
    _crear_usuario("mesero_test", "ClaveMesero123", [3])  # Mesero
    id_orden, ids = _crear_cuenta()

    with pytest.raises(CancelacionError, match="Autorización inválida"):
        cancelar_orden(
            id_orden=id_orden,
            id_motivo=1,
            id_usuario_solicita=1,
            usuario_autoriza="mesero_test",
            password_autoriza="ClaveMesero123",
        )
    with pytest.raises(CancelacionError, match="Autorización inválida"):
        cancelar_orden(
            id_orden=id_orden,
            id_motivo=1,
            id_usuario_solicita=1,
            usuario_autoriza="admin",
            password_autoriza="incorrecta",
        )

    assert _estado_orden(id_orden) == "Pendiente"
    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == len(ids)
    assert _escalar("SELECT COUNT(*) FROM cancelaciones") == 0
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'LOGIN_FALLIDO'"
    ) >= 2


def test_cancelar_orden_pendiente_y_en_preparacion():
    _preparar_admin()
    ids_insumos = [
        int(f[0])
        for f in _filas("SELECT id_insumo FROM recetas WHERE id_producto = 1")
    ]
    stock_inicial = _stock(ids_insumos)

    id_orden, ids = _crear_cuenta(cantidad=2)
    cambiar_estado_partida(ids[1], "Pendiente", "En preparación", 1)

    resultado = cancelar_orden(
        id_orden=id_orden,
        id_motivo=1,
        id_usuario_solicita=1,
        usuario_autoriza="admin",
        password_autoriza=PASSWORD_ADMIN,
        comentario="Cliente se retiró",
    )

    assert len(resultado["devueltas"]) == 1
    assert len(resultado["mermadas"]) == 1
    assert _estado_orden(id_orden) == "Cancelada"
    for id_detalle in ids:
        assert _escalar(
            "SELECT ed.nombre FROM detalles_orden d "
            "JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado "
            "WHERE d.id_detalle = :d",
            {"d": id_detalle},
        ) == "Cancelado"
    assert _escalar("SELECT COUNT(*) FROM cancelaciones") == 1
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'CANCELACION'"
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM detalle_mermas WHERE id_detalle = :d",
        {"d": ids[1]},
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM mermas_pendientes WHERE id_detalle = :d",
        {"d": ids[1]},
    ) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM mermas m "
        "JOIN tipos_merma t ON t.id_tipo_merma = m.id_tipo_merma "
        "WHERE t.nombre = 'Cancelación de orden'"
    ) > 0

    receta = {
        int(f[0]): Decimal(str(f[1]))
        for f in _filas(
            "SELECT id_insumo, cantidad_usada FROM recetas WHERE id_producto = 1"
        )
    }
    for id_insumo, cantidad in receta.items():
        # Solo se pierde la partida mermada; la pendiente regresó completa.
        assert _stock(ids_insumos)[id_insumo] == (
            stock_inicial[id_insumo] - cantidad
        )


def test_no_cancela_pagada_ni_cancelada():
    _preparar_admin()
    id_orden, ids = _crear_cuenta()
    _activar_para_cobro(ids[0])
    cobrar_orden_mesa(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        id_usuario=1,
    )

    with pytest.raises(CancelacionError, match="reembolso"):
        cancelar_orden(
            id_orden=id_orden,
            id_motivo=1,
            id_usuario_solicita=1,
            usuario_autoriza="admin",
            password_autoriza=PASSWORD_ADMIN,
        )

    id_orden2, _ = _crear_cuenta(mesa=2)
    _ejecutar(
        "UPDATE ordenes o SET id_estado = e.id_estado "
        "FROM estados_orden e WHERE e.nombre = 'Cancelada' "
        "AND o.id_orden = :o",
        {"o": id_orden2},
    )
    with pytest.raises(CancelacionError, match="ya está cancelada"):
        cancelar_orden(
            id_orden=id_orden2,
            id_motivo=1,
            id_usuario_solicita=1,
            usuario_autoriza="admin",
            password_autoriza=PASSWORD_ADMIN,
        )
    assert _escalar("SELECT COUNT(*) FROM cancelaciones") == 0


def test_cancelar_no_libera_mesa_con_otra_cuenta_abierta():
    _preparar_admin()
    id_orden, _ = _crear_cuenta(mesa=1)
    otra = registrar_comanda(
        partidas=[dict(PRODUCTO)],
        id_tipo_orden=2,
        id_usuario=1,
        id_empleado=1,
    )
    _ejecutar(
        "UPDATE ordenes SET id_mesa = 1 WHERE id_orden = :o",
        {"o": int(otra["id_orden"])},
    )

    cancelar_orden(
        id_orden=id_orden,
        id_motivo=3,
        id_usuario_solicita=1,
        usuario_autoriza="admin",
        password_autoriza=PASSWORD_ADMIN,
    )

    assert _escalar(
        "SELECT estado FROM mesas WHERE id_mesa = 1"
    ) == "Ocupada"


def test_motivo_otro_exige_comentario():
    _preparar_admin()
    id_orden, _ = _crear_cuenta()

    with pytest.raises(CancelacionError, match="«Otro»"):
        cancelar_orden(
            id_orden=id_orden,
            id_motivo=5,
            id_usuario_solicita=1,
            usuario_autoriza="admin",
            password_autoriza=PASSWORD_ADMIN,
            comentario="   ",
        )

    assert _estado_orden(id_orden) == "Pendiente"
    assert _escalar("SELECT COUNT(*) FROM cancelaciones") == 0


def test_cancelar_producto_sin_receta_registra_merma_con_costo_cero():
    _preparar_admin()
    with get_engine().begin() as conn:
        id_producto = conn.execute(
            text(
                """
                INSERT INTO productos
                    (nombre, descripcion, id_categoria, id_area_preparacion,
                     precio_actual, disponible)
                VALUES ('Sin receta', 'Producto de prueba', 3, 2, 10.00, TRUE)
                RETURNING id_producto
                """
            )
        ).scalar_one()

    resultado = guardar_orden_mesa(
        id_mesa=1,
        partidas=[
            {
                "id_producto": int(id_producto),
                "cantidad": 1,
                "precio_unitario": 10.00,
                "id_area_preparacion": 2,
            }
        ],
        id_usuario=1,
        id_empleado=1,
    )
    id_orden = int(resultado["id_orden"])
    id_detalle = int(
        _escalar(
            "SELECT MIN(id_detalle) FROM detalles_orden WHERE id_orden = :o",
            {"o": id_orden},
        )
    )
    cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)

    cancelar_orden(
        id_orden=id_orden,
        id_motivo=1,
        id_usuario_solicita=1,
        usuario_autoriza="admin",
        password_autoriza=PASSWORD_ADMIN,
    )

    assert _escalar(
        "SELECT COUNT(*) FROM detalle_mermas WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1
    assert _escalar(
        """
        SELECT COUNT(*)
        FROM mermas m
        JOIN tipos_merma t ON t.id_tipo_merma = m.id_tipo_merma
        WHERE t.nombre = 'Cancelación de orden'
          AND m.costo_estimado_perdida = 0
        """
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM mermas_pendientes WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 0


def test_cancelar_personalizada_revierte_consumo_exacto_guardado():
    _preparar_admin()
    ids_insumos = [
        int(f[0])
        for f in _filas("SELECT id_insumo FROM recetas WHERE id_producto = 1")
    ]
    stock_inicial = _stock(ids_insumos)

    resultado = guardar_orden_mesa(
        id_mesa=1,
        partidas=[
            {
                **PRODUCTO,
                "modificadores": [1, 3],
            }
        ],
        id_usuario=1,
        id_empleado=1,
    )
    id_orden = int(resultado["id_orden"])
    id_detalle = int(
        _escalar(
            "SELECT MIN(id_detalle) FROM detalles_orden WHERE id_orden = :o",
            {"o": id_orden},
        )
    )
    assert _escalar(
        "SELECT COUNT(*) FROM detalle_consumo WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1

    # La receta y los modificadores cambian DESPUÉS de la venta; la reversa
    # debe usar la foto guardada, no la receta vigente.
    _ejecutar(
        "UPDATE modificadores_producto SET cantidad = 0.500 "
        "WHERE id_modificador = 3"
    )
    _ejecutar(
        "UPDATE modificadores_producto SET activo = FALSE "
        "WHERE id_modificador = 1"
    )
    _ejecutar(
        "UPDATE recetas SET cantidad_usada = 0.200 WHERE id_producto = 1 "
        "AND id_insumo = 1"
    )

    cancelar_orden(
        id_orden=id_orden,
        id_motivo=2,
        id_usuario_solicita=1,
        usuario_autoriza="admin",
        password_autoriza=PASSWORD_ADMIN,
    )

    assert _stock(ids_insumos) == stock_inicial
