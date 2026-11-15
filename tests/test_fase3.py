"""Pruebas de regresión de la Fase 3 (personalización de productos)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from database import get_engine
from services.mermas import MermaError, registrar_merma_detalle
from services.modificadores import (
    ModificadorError,
    actualizar_modificador,
    cambiar_activo_modificador,
    crear_modificador,
    modificadores_de_producto,
)
from services.recetas import cambiar_cantidad_receta
from services.ventas import (
    StockInsuficienteError,
    VentaError,
    eliminar_partida,
    guardar_orden_mesa,
    modificar_cantidad_partida,
)

INSUMOS_HAMBURGUESA = (1, 2, 3, 4, 5)
INSUMOS_BBQ = (1, 2, 3, 7, 8)


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None) -> list:
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _contar(tabla: str) -> int:
    return int(_escalar(f"SELECT COUNT(*) FROM {tabla}"))


def _stock(ids_insumos) -> dict[int, Decimal]:
    return {
        int(id_insumo): Decimal(
            str(
                _escalar(
                    "SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                    {"i": int(id_insumo)},
                )
            )
        )
        for id_insumo in ids_insumos
    }


def _movimientos_firmados(id_orden: int, id_insumo: int) -> Decimal:
    valor = _escalar(
        """
        SELECT COALESCE(SUM(
            CASE WHEN t.efecto = 'Entrada' THEN m.cantidad ELSE -m.cantidad END
        ), 0)
        FROM movimientos_inventario m
        JOIN tipos_movimiento_inventario t
          ON t.id_tipo_movimiento = m.id_tipo_movimiento
        WHERE m.id_orden = :o AND m.id_insumo = :i
        """,
        {"o": id_orden, "i": id_insumo},
    )
    return Decimal(str(valor or 0))


def _assert_stock_igual_movimientos(
    id_orden: int, inicial: dict[int, Decimal]
) -> None:
    for id_insumo, cantidad_inicial in inicial.items():
        actual = _stock([id_insumo])[id_insumo]
        assert actual - cantidad_inicial == _movimientos_firmados(
            id_orden, id_insumo
        ), f"insumo {id_insumo}"


def _crear_cuenta(
    *,
    id_producto: int,
    precio: float,
    modificadores: list[int] | None = None,
    mesa: int = 1,
) -> tuple[int, int]:
    partida = {
        "id_producto": id_producto,
        "cantidad": 1,
        "precio_unitario": precio,
        "id_area_preparacion": 1,
    }
    if modificadores:
        partida["modificadores"] = modificadores
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=[partida],
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
    return id_orden, id_detalle


def _precio_detalle(id_detalle: int) -> Decimal:
    return Decimal(
        str(
            _escalar(
                "SELECT precio_unitario FROM detalles_orden "
                "WHERE id_detalle = :d",
                {"d": id_detalle},
            )
        )
    )


def test_quitar_insumo_baja_consumo_exacto_sin_cambiar_precio():
    inicial = _stock(INSUMOS_HAMBURGUESA)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[1]
    )

    final = _stock(INSUMOS_HAMBURGUESA)
    assert final[4] == inicial[4], "la lechuga quitada no debe descontarse"
    assert final[1] == inicial[1] - Decimal("0.20")
    assert final[2] == inicial[2] - Decimal("1.00")
    assert final[3] == inicial[3] - Decimal("0.05")
    assert final[5] == inicial[5] - Decimal("0.04")
    assert _precio_detalle(id_detalle) == Decimal("120.00")
    assert Decimal(str(_escalar(
        "SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}
    ))) == Decimal("120.00")
    _assert_stock_igual_movimientos(id_orden, inicial)


def test_extra_sube_precio_y_descuenta_su_cantidad():
    inicial = _stock(INSUMOS_HAMBURGUESA)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[3]
    )

    final = _stock(INSUMOS_HAMBURGUESA)
    assert final[3] == inicial[3] - Decimal("0.08")
    assert final[1] == inicial[1] - Decimal("0.20")
    assert final[4] == inicial[4] - Decimal("0.03")
    assert _precio_detalle(id_detalle) == Decimal("135.00")
    assert Decimal(str(_escalar(
        "SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}
    ))) == Decimal("135.00")
    _assert_stock_igual_movimientos(id_orden, inicial)


def test_sustituir_cambia_insumo_y_precio():
    inicial = _stock(INSUMOS_BBQ)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=2, precio=135.00, modificadores=[5]
    )

    final = _stock(INSUMOS_BBQ)
    assert final[7] == inicial[7], "la salsa BBQ original no debe descontarse"
    assert final[8] == inicial[8] - Decimal("0.03")
    assert final[1] == inicial[1] - Decimal("0.20")
    assert _precio_detalle(id_detalle) == Decimal("140.00")
    _assert_stock_igual_movimientos(id_orden, inicial)


def test_extra_sin_stock_revierte_toda_la_orden():
    _ejecutar(
        "UPDATE insumos SET stock_actual = 0.05 WHERE id_insumo = 3"
    )
    ordenes_antes = _contar("ordenes")
    detalles_antes = _contar("detalles_orden")
    fotos_antes = _contar("detalle_modificadores")
    auditoria_antes = _contar(
        "auditoria WHERE tabla_afectada = 'detalles_orden_consumo'"
    )

    with pytest.raises(StockInsuficienteError) as exc:
        _crear_cuenta(id_producto=1, precio=120.00, modificadores=[3])

    assert any(f.insumo == "Queso Cheddar" for f in exc.value.faltantes)
    assert _contar("ordenes") == ordenes_antes
    assert _contar("detalles_orden") == detalles_antes
    assert _contar("detalle_modificadores") == fotos_antes
    assert _contar(
        "auditoria WHERE tabla_afectada = 'detalles_orden_consumo'"
    ) == auditoria_antes


def test_rechaza_modificador_ajeno_inactivo_o_repetido_sin_insertar():
    ordenes_antes = _contar("ordenes")
    detalles_antes = _contar("detalles_orden")
    fotos_antes = _contar("detalle_modificadores")

    with pytest.raises(VentaError):
        _crear_cuenta(id_producto=1, precio=120.00, modificadores=[4])
    assert _contar("ordenes") == ordenes_antes
    assert _contar("detalles_orden") == detalles_antes
    assert _contar("detalle_modificadores") == fotos_antes

    with pytest.raises(VentaError):
        _crear_cuenta(id_producto=1, precio=120.00, modificadores=[1, 1])
    assert _contar("ordenes") == ordenes_antes
    assert _contar("detalles_orden") == detalles_antes
    assert _contar("detalle_modificadores") == fotos_antes

    cambiar_activo_modificador(1, False)
    assert all(
        int(m["id_modificador"]) != 1 for m in modificadores_de_producto(1)
    )
    with pytest.raises(VentaError):
        _crear_cuenta(id_producto=1, precio=120.00, modificadores=[1])
    assert _contar("ordenes") == ordenes_antes
    assert _contar("detalles_orden") == detalles_antes
    assert _contar("detalle_modificadores") == fotos_antes


def test_quitar_rechaza_insumo_que_no_esta_en_la_receta():
    antes = _contar("modificadores_producto")
    with pytest.raises(ModificadorError):
        crear_modificador(
            id_producto=10,
            tipo="Quitar",
            nombre="Sin lechuga en cafe",
            id_insumo=4,
        )
    assert _contar("modificadores_producto") == antes


def test_eliminar_partida_personalizada_devuelve_lo_descontado_aunque_cambie_receta():
    inicial = _stock(INSUMOS_HAMBURGUESA)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[1, 3]
    )

    actualizar_modificador(
        3,
        nombre="Extra queso",
        id_insumo=3,
        cantidad=0.500,
        precio_extra=99.00,
    )
    cambiar_activo_modificador(1, False)
    cambiar_cantidad_receta(1, 4, 0.500)
    cambiar_cantidad_receta(1, 3, 0.200)

    eliminar_partida(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
    )

    assert _stock(INSUMOS_HAMBURGUESA) == inicial
    assert _escalar(
        "SELECT COUNT(*) FROM detalle_modificadores WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria "
        "WHERE tabla_afectada = 'detalles_orden_consumo' "
        "AND id_registro_afectado = :d",
        {"d": id_detalle},
    ) == 0
    _assert_stock_igual_movimientos(id_orden, inicial)


def test_modificar_cantidad_de_personalizada_se_rechaza():
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[1]
    )
    stock_antes = _stock(INSUMOS_HAMBURGUESA)

    with pytest.raises(VentaError) as exc:
        modificar_cantidad_partida(
            id_detalle=id_detalle,
            nueva_cantidad=2,
            id_usuario=1,
            id_empleado=1,
        )

    assert str(exc.value) == (
        "Esta partida está personalizada; agrega otra partida para cambiar "
        "la cantidad."
    )
    assert _escalar(
        "SELECT cantidad FROM detalles_orden WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1
    assert _stock(INSUMOS_HAMBURGUESA) == stock_antes
    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == 1


def test_mermar_personalizada_no_toca_stock_y_repetir_mantiene_modificadores():
    inicial = _stock(INSUMOS_HAMBURGUESA)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[1, 3]
    )
    stock_tras_venta = _stock(INSUMOS_HAMBURGUESA)
    movimientos_tras_venta = {
        i: _movimientos_firmados(id_orden, i) for i in INSUMOS_HAMBURGUESA
    }

    registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se cayó",
        liberar_mesa_si_total=False,
    )

    assert _stock(INSUMOS_HAMBURGUESA) == stock_tras_venta
    for id_insumo in INSUMOS_HAMBURGUESA:
        assert _movimientos_firmados(id_orden, id_insumo) == (
            movimientos_tras_venta[id_insumo]
        )

    pendientes = _filas(
        """
        SELECT datos_nuevos ->> 'id_insumo' AS insumo,
               datos_nuevos ->> 'cantidad' AS cantidad
        FROM auditoria
        WHERE tabla_afectada = 'mermas_pendientes'
          AND datos_nuevos ->> 'id_detalle' = :detalle
        ORDER BY insumo
        """,
        {"detalle": str(id_detalle)},
    )
    por_insumo = {int(fila[0]): Decimal(str(fila[1])) for fila in pendientes}
    assert 4 not in por_insumo
    assert por_insumo[3] == Decimal("0.080")

    inicial_b = _stock(INSUMOS_HAMBURGUESA)
    id_orden_b, id_detalle_b = _crear_cuenta(
        id_producto=1,
        precio=120.00,
        modificadores=[1, 3],
        mesa=2,
    )
    stock_tras_venta_b = _stock(INSUMOS_HAMBURGUESA)
    registrar_merma_detalle(
        id_detalle=id_detalle_b,
        id_usuario=1,
        id_empleado=1,
        motivo="Se cayó",
        liberar_mesa_si_total=False,
        repetir=True,
    )

    stock_repetido = _stock(INSUMOS_HAMBURGUESA)
    assert stock_repetido[1] == stock_tras_venta_b[1] - Decimal("0.20")
    assert stock_repetido[2] == stock_tras_venta_b[2] - Decimal("1.00")
    assert stock_repetido[3] == stock_tras_venta_b[3] - Decimal("0.08")
    assert stock_repetido[4] == stock_tras_venta_b[4]
    assert stock_repetido[5] == stock_tras_venta_b[5] - Decimal("0.04")

    id_reposicion = int(
        _escalar(
            "SELECT MAX(id_detalle) FROM detalles_orden WHERE id_orden = :o",
            {"o": id_orden_b},
        )
    )
    foto = _filas(
        """
        SELECT tipo, nombre, cantidad_resta, cantidad_suma
        FROM detalle_modificadores
        WHERE id_detalle = :d
        ORDER BY id_detalle_modificador
        """,
        {"d": id_reposicion},
    )
    assert [(str(f[0]), str(f[1])) for f in foto] == [
        ("Quitar", "Sin lechuga"),
        ("Extra", "Extra queso"),
    ]
    _assert_stock_igual_movimientos(id_orden_b, inicial_b)


def test_cambio_de_precio_del_modificador_no_altera_partidas_vendidas():
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[3]
    )
    actualizar_modificador(
        3,
        nombre="Extra queso",
        id_insumo=3,
        cantidad=0.030,
        precio_extra=99.00,
    )

    assert _precio_detalle(id_detalle) == Decimal("135.00")
    assert Decimal(str(_escalar(
        "SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}
    ))) == Decimal("135.00")
    assert Decimal(
        str(
            _escalar(
                "SELECT precio_extra FROM detalle_modificadores "
                "WHERE id_detalle = :d",
                {"d": id_detalle},
            )
        )
    ) == Decimal("15.00")


def test_stock_cambia_igual_que_movimientos_en_venta_y_eliminacion():
    inicial = _stock(INSUMOS_HAMBURGUESA)
    id_orden, id_detalle = _crear_cuenta(
        id_producto=1, precio=120.00, modificadores=[3]
    )
    _assert_stock_igual_movimientos(id_orden, inicial)

    eliminar_partida(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
    )
    assert _stock(INSUMOS_HAMBURGUESA) == inicial
    _assert_stock_igual_movimientos(id_orden, inicial)
