"""Pruebas de la Fase 6: proveedores, lotes, caducidad y costo promedio."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import get_engine, transaccion
from modules.compras import _registrar_compra
from modules.inventario import _rechazar_mermas, _subir_mermas
from services.lotes import (
    ESTADO_CADUCADO,
    ESTADO_POR_VENCER,
    ESTADO_VIGENTE,
    existencias_por_lote,
    lotes_en_alerta,
    stock_utilizable,
)
from services.mermas import MermaError, registrar_merma_detalle, registrar_merma_lote
from services.proveedores import (
    ProveedorError,
    crear_proveedor,
    listar_historial_precios,
    vincular_insumo,
)
from services.ventas import (
    Faltante,
    StockInsuficienteError,
    descontar_inventario,
    guardar_orden_mesa,
    porciones_disponibles,
)

HOY = date.today()
PARTIDA = {
    "id_producto": 1,
    "cantidad": 1,
    "precio_unitario": 120.00,
    "id_area_preparacion": 1,
}
PROVEEDOR = {"id_proveedor": 1, "nombre": "Carnes del Norte"}
AUTH = {"id_usuario": 1, "id_empleado": 1}


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _crear_lote(
    *, id_insumo: int, cantidad: str, fecha: date | None
) -> int:
    with transaccion() as conn:
        id_compra = conn.execute(
            text(
                """
                INSERT INTO compras (id_proveedor, id_empleado, total)
                VALUES (1, 8, 0)
                RETURNING id_compra
                """
            )
        ).scalar_one()
        id_detalle = conn.execute(
            text(
                """
                INSERT INTO detalle_compra
                    (id_compra, id_insumo, cantidad, costo_unitario)
                VALUES (:compra, :insumo, :cantidad, 10)
                RETURNING id_detalle_compra
                """
            ),
            {
                "compra": int(id_compra),
                "insumo": int(id_insumo),
                "cantidad": Decimal(cantidad),
            },
        ).scalar_one()
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO lotes_insumo
                        (id_insumo, id_detalle_compra, cantidad_recibida,
                         fecha_caducidad)
                    VALUES (:insumo, :detalle, :cantidad, :fecha)
                    RETURNING id_lote
                    """
                ),
                {
                    "insumo": int(id_insumo),
                    "detalle": int(id_detalle),
                    "cantidad": Decimal(cantidad),
                    "fecha": fecha,
                },
            ).scalar_one()
        )


def _item_compra(id_insumo: int, cantidad: str = "1") -> dict:
    return {
        "linea": 1,
        "id_insumo": id_insumo,
        "nombre": f"Insumo {id_insumo}",
        "unidad": "kg",
        "cantidad": float(cantidad),
        "costo_unitario": 200.0,
        "fecha_caducidad": None,
        "requiere_caducidad": True,
    }


def test_proveedor_duplicado_historial_y_evento():
    id_proveedor = crear_proveedor(
        nombre="Proveedor Fase 6",
        telefono="5550000000",
        direccion="CDMX",
        correo="fase6@example.com",
        condiciones="Crédito 30 días",
        id_usuario=1,
    )
    with pytest.raises(ProveedorError, match="Ya existe un proveedor"):
        crear_proveedor(
            nombre="proveedor fase 6",
            telefono=None,
            direccion=None,
            correo=None,
            condiciones=None,
            id_usuario=1,
        )

    assert _escalar(
        "SELECT COUNT(*) FROM proveedores WHERE LOWER(nombre) = LOWER(:n)",
        {"n": "Proveedor Fase 6"},
    ) == 1
    with pytest.raises(IntegrityError):
        _ejecutar(
            "INSERT INTO proveedores (nombre) VALUES ('proveedor fase 6')"
        )

    vincular_insumo(
        id_proveedor=id_proveedor,
        id_insumo=1,
        costo_acordado=Decimal("100.00"),
        id_usuario=1,
    )
    vincular_insumo(
        id_proveedor=id_proveedor,
        id_insumo=1,
        costo_acordado=Decimal("110.00"),
        id_usuario=1,
    )
    historial = listar_historial_precios(id_proveedor)
    assert len(historial) == 1
    assert historial.iloc[0]["insumo"] == "Carne Res"
    assert Decimal(str(historial.iloc[0]["costo_anterior"])) == Decimal("100.00")
    assert Decimal(str(historial.iloc[0]["costo_nuevo"])) == Decimal("110.00")
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PROVEEDOR'"
    ) >= 3


def test_compra_sin_fecha_o_fecha_pasada_hace_rollback():
    compras_antes = _escalar("SELECT COUNT(*) FROM compras")
    lotes_antes = _escalar("SELECT COUNT(*) FROM lotes_insumo")
    stock_antes = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 1"))
    )

    with pytest.raises(ValueError, match="obligatoria"):
        _registrar_compra(PROVEEDOR, [_item_compra(1)], 0.0, AUTH)
    with pytest.raises(ValueError, match="posterior a hoy"):
        _registrar_compra(
            PROVEEDOR,
            [_item_compra(1)],
            0.0,
            AUTH,
            fechas_caducidad={1: HOY},
        )

    assert _escalar("SELECT COUNT(*) FROM compras") == compras_antes
    assert _escalar("SELECT COUNT(*) FROM lotes_insumo") == lotes_antes
    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 1"))
    ) == stock_antes


def test_compra_valida_crea_lote_actualiza_promedio_y_bitacora():
    stock_antes = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 1"))
    )
    costo_antes = Decimal(
        str(
            _escalar(
                "SELECT costo_unitario_referencia FROM insumos "
                "WHERE id_insumo = 1"
            )
        )
    )
    fecha = HOY + timedelta(days=10)
    id_compra = _registrar_compra(
        PROVEEDOR,
        [_item_compra(1, "10")],
        0.0,
        AUTH,
        fechas_caducidad={1: fecha},
    )

    stock_nuevo = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 1"))
    )
    costo_nuevo = Decimal(
        str(
            _escalar(
                "SELECT costo_unitario_referencia FROM insumos "
                "WHERE id_insumo = 1"
            )
        )
    )
    esperado = (
        (stock_antes * costo_antes) + (Decimal("10") * Decimal("200"))
    ) / (stock_antes + Decimal("10"))
    assert stock_nuevo == stock_antes + Decimal("10")
    assert costo_nuevo == esperado.quantize(Decimal("0.01"))
    assert _escalar(
        "SELECT cantidad_recibida FROM lotes_insumo WHERE id_insumo = 1 "
        "AND fecha_caducidad = :fecha",
        {"fecha": fecha},
    ) == Decimal("10.000")
    assert _escalar(
        "SELECT COUNT(*) FROM movimientos_inventario WHERE id_compra = :c",
        {"c": id_compra},
    ) == 1

    datos = _escalar(
        "SELECT datos_nuevos FROM bitacora WHERE tipo_evento = 'COMPRA' "
        "ORDER BY id_bitacora DESC LIMIT 1"
    )
    assert float(datos["costos"][0]["costo_anterior"]) == float(costo_antes)
    assert float(datos["costos"][0]["costo_nuevo"]) == float(costo_nuevo)


def test_existencias_por_lote_reparte_fefo_y_deja_sin_lote():
    _crear_lote(id_insumo=1, cantidad="10", fecha=HOY + timedelta(days=10))
    _crear_lote(id_insumo=1, cantidad="10", fecha=HOY + timedelta(days=5))
    _ejecutar(
        "UPDATE insumos SET stock_actual = 15 WHERE id_insumo = 1"
    )

    with get_engine().connect() as conn:
        filas = [
            f for f in existencias_por_lote(conn, [1]) if f["id_lote"] is not None
        ]
        total = sum(
            (Decimal(str(f["cantidad_restante"])) for f in filas), Decimal("0")
        )
        sin_lote = [
            f for f in existencias_por_lote(conn, [1]) if f["id_lote"] is None
        ][0]

    assert [Decimal(str(f["cantidad_restante"])) for f in filas] == [
        Decimal("10.000"),
        Decimal("5.000"),
    ]
    assert total == Decimal("15.000")
    assert Decimal(str(sin_lote["cantidad_restante"])) == Decimal("0.000")
    assert sin_lote["estado"] == "Sin lote"


def test_estados_y_stock_utilizable_solo_bloquean_caducados():
    _crear_lote(id_insumo=1, cantidad="10", fecha=HOY + timedelta(days=10))
    _crear_lote(id_insumo=1, cantidad="10", fecha=HOY + timedelta(days=2))
    _crear_lote(id_insumo=1, cantidad="10", fecha=HOY - timedelta(days=1))
    _ejecutar("UPDATE insumos SET stock_actual = 30 WHERE id_insumo = 1")

    with get_engine().connect() as conn:
        filas = existencias_por_lote(conn, [1])
        estados = {
            f["fecha_caducidad"]: f["estado"]
            for f in filas
            if f["id_lote"] is not None
        }
        utilizable = stock_utilizable(conn, 1)

    assert estados[HOY + timedelta(days=10)] == ESTADO_VIGENTE
    assert estados[HOY + timedelta(days=2)] == ESTADO_POR_VENCER
    assert estados[HOY - timedelta(days=1)] == ESTADO_CADUCADO
    assert utilizable == Decimal("20.000")
    assert len(lotes_en_alerta()) == 2


def test_venta_bloqueada_por_lote_caducado_muestra_bloqueo():
    _crear_lote(id_insumo=4, cantidad="10", fecha=HOY - timedelta(days=1))
    _ejecutar("UPDATE insumos SET stock_actual = 10 WHERE id_insumo = 4")

    with pytest.raises(StockInsuficienteError) as exc:
        with get_engine().connect() as conn:
            descontar_inventario(
                conn,
                id_orden=1,
                partidas=[dict(PARTIDA)],
                id_empleado=1,
            )

    faltante = next(f for f in exc.value.faltantes if f.insumo == "Lechuga")
    assert isinstance(faltante, Faltante)
    assert faltante.disponible == Decimal("0")
    assert faltante.bloqueado == Decimal("10.000")


def test_porciones_disponibles_descuenta_caducados():
    _crear_lote(id_insumo=4, cantidad="10", fecha=HOY - timedelta(days=1))
    _ejecutar("UPDATE insumos SET stock_actual = 10 WHERE id_insumo = 4")
    assert porciones_disponibles(None)[1] == 0


def test_merma_de_lote_se_marca_desechado_al_subir():
    id_lote = _crear_lote(
        id_insumo=4, cantidad="5", fecha=HOY - timedelta(days=1)
    )
    _ejecutar("UPDATE insumos SET stock_actual = 5 WHERE id_insumo = 4")
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    ids = registrar_merma_lote(
        id_lote=id_lote,
        id_usuario=1,
        id_empleado=1,
        motivo="Caducidad",
    )
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_lote = :lote "
        "AND id_movimiento IS NULL",
        {"lote": id_lote},
    ) == 1
    assert _escalar(
        "SELECT desechado FROM lotes_insumo WHERE id_lote = :lote",
        {"lote": id_lote},
    ) is False
    with pytest.raises(MermaError, match="merma pendiente"):
        registrar_merma_lote(
            id_lote=id_lote, id_usuario=1, id_empleado=1, motivo="Caducidad"
        )

    _subir_mermas(ids)

    assert _escalar(
        "SELECT desechado FROM lotes_insumo WHERE id_lote = :lote",
        {"lote": id_lote},
    ) is True
    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 4"))
    ) == Decimal("0.000")
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == (
        movimientos_antes + 1
    )


def test_rechazar_merma_de_lote_no_lo_marca_desechado():
    id_lote = _crear_lote(
        id_insumo=4, cantidad="3", fecha=HOY - timedelta(days=1)
    )
    _ejecutar("UPDATE insumos SET stock_actual = 3 WHERE id_insumo = 4")
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")
    ids = registrar_merma_lote(
        id_lote=id_lote, id_usuario=1, id_empleado=1
    )

    _rechazar_mermas(ids)

    assert _escalar(
        "SELECT desechado FROM lotes_insumo WHERE id_lote = :lote",
        {"lote": id_lote},
    ) is False
    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = 4"))
    ) == Decimal("3.000")
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[]))",
        {"ids": ids},
    ) == 0


def test_merma_de_partida_ya_vendida_solo_valoriza_sin_movimiento():
    _ejecutar(
        "UPDATE ordenes SET id_estado = "
        "(SELECT id_estado FROM estados_orden WHERE nombre = 'Pagada') "
        "WHERE id_orden = 1"
    )
    id_detalle = int(
        _escalar("SELECT MIN(id_detalle) FROM detalles_orden WHERE id_orden = 1")
    )
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    ids = registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Producto ya vendido",
        origen="orden",
        autorizada=True,
    )

    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[])) "
        "AND costo_estimado_perdida > 0 AND id_movimiento IS NULL",
        {"ids": ids},
    ) == len(ids)


def test_compra_mantiene_stock_igual_a_movimientos():
    id_insumo = 2
    stock_antes = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    )
    neto_antes = Decimal(
        str(
            _escalar(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN t.efecto = 'Entrada' THEN m.cantidad
                         ELSE -m.cantidad END
                ), 0)
                FROM movimientos_inventario m
                JOIN tipos_movimiento_inventario t
                  ON t.id_tipo_movimiento = m.id_tipo_movimiento
                WHERE m.id_insumo = :i
                """,
                {"i": id_insumo},
            )
        )
    )
    _registrar_compra(
        PROVEEDOR,
        [_item_compra(id_insumo, "7")],
        0.0,
        AUTH,
        fechas_caducidad={1: HOY + timedelta(days=8)},
    )
    stock_despues = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    )
    neto_despues = Decimal(
        str(
            _escalar(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN t.efecto = 'Entrada' THEN m.cantidad
                         ELSE -m.cantidad END
                ), 0)
                FROM movimientos_inventario m
                JOIN tipos_movimiento_inventario t
                  ON t.id_tipo_movimiento = m.id_tipo_movimiento
                WHERE m.id_insumo = :i
                """,
                {"i": id_insumo},
            )
        )
    )
    assert stock_despues - stock_antes == Decimal("7.000")
    assert neto_despues - neto_antes == Decimal("7.000")
