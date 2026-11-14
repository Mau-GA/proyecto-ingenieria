"""Pruebas de regresión de la Fase 2 (recetas, catálogo y órdenes)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import text

from database import get_engine
from services.catalogo import CatalogoError, actualizar_producto
from services.recetas import (
    RecetaError,
    agregar_insumo_receta,
    cambiar_cantidad_receta,
    quitar_insumo_receta,
)
from services.ventas import (
    StockInsuficienteError,
    VentaError,
    cobrar_orden_mesa,
    eliminar_partida,
    guardar_orden_mesa,
    modificar_cantidad_partida,
    porciones_disponibles,
)

PRODUCTO = {
    "id_producto": 1,
    "cantidad": 1,
    "precio_unitario": 120.00,
    "id_area_preparacion": 1,
}


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None) -> list:
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _crear_cuenta(mesa: int = 1, producto: dict | None = None):
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=[dict(producto or PRODUCTO)],
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


def _receta(id_producto: int) -> dict[int, Decimal]:
    filas = _filas(
        "SELECT id_insumo, cantidad_usada FROM recetas WHERE id_producto = :p",
        {"p": id_producto},
    )
    return {
        int(id_insumo): Decimal(str(cantidad))
        for id_insumo, cantidad in filas
    }


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


def test_doble_cobro_simultaneo_solo_registra_un_pago():
    id_orden, _ = _crear_cuenta()
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = 4 WHERE id_orden = :o",
        {"o": id_orden},
    )

    barrera = Barrier(2)

    def cobrar():
        barrera.wait(timeout=10)
        try:
            cobrar_orden_mesa(
                id_orden=id_orden,
                id_mesa=1,
                id_metodo_pago=1,
                id_usuario=1,
            )
            return ("ok", "")
        except Exception as exc:  # se inspecciona el mensaje en la aserción
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: cobrar(), range(2)))

    exitos = [r for r in resultados if r[0] == "ok"]
    errores = [r for r in resultados if r[0] == "error"]
    assert len(exitos) == 1
    assert len(errores) == 1
    assert errores[0][1] == "La orden ya fue cobrada o cancelada."
    assert _escalar(
        "SELECT COUNT(*) FROM pagos WHERE id_orden = :o", {"o": id_orden}
    ) == 1


def test_receta_agregar_cambiar_quitar_y_validaciones():
    id_producto = 10  # Cafe Americano: receta inicial con un solo insumo
    id_insumo = 11    # Limones, no está en esa receta
    antes = _escalar(
        "SELECT COUNT(*) FROM recetas WHERE id_producto = :p",
        {"p": id_producto},
    )

    agregar_insumo_receta(id_producto, id_insumo, Decimal("0.500"))
    assert _escalar(
        "SELECT COUNT(*) FROM recetas WHERE id_producto = :p",
        {"p": id_producto},
    ) == antes + 1

    cambiar_cantidad_receta(id_producto, id_insumo, Decimal("0.750"))
    assert Decimal(
        str(
            _escalar(
                "SELECT cantidad_usada FROM recetas "
                "WHERE id_producto = :p AND id_insumo = :i",
                {"p": id_producto, "i": id_insumo},
            )
        )
    ) == Decimal("0.750")

    quitar_insumo_receta(id_producto, id_insumo)
    assert _escalar(
        "SELECT COUNT(*) FROM recetas WHERE id_producto = :p",
        {"p": id_producto},
    ) == antes

    agregar_insumo_receta(id_producto, id_insumo, Decimal("0.500"))
    with pytest.raises(RecetaError, match="Ese insumo ya está en la receta"):
        agregar_insumo_receta(id_producto, id_insumo, Decimal("0.100"))
    with pytest.raises(RecetaError, match="mayor que cero"):
        agregar_insumo_receta(id_producto, 12, Decimal("0"))
    assert _escalar(
        "SELECT COUNT(*) FROM recetas WHERE id_producto = :p",
        {"p": id_producto},
    ) == antes + 1


def test_editar_receta_no_cambia_ventas_pasadas_y_nueva_usa_receta_nueva():
    id_producto = 10
    id_insumo = 13  # Cafe Molido: 0.03 por porción en el seed
    id_orden_vieja, _ = _crear_cuenta(mesa=1, producto={
        "id_producto": id_producto,
        "cantidad": 1,
        "precio_unitario": 40.00,
        "id_area_preparacion": 2,
    })
    cantidad_vieja = Decimal(
        str(
            _escalar(
                "SELECT cantidad FROM movimientos_inventario "
                "WHERE id_orden = :o AND id_insumo = :i",
                {"o": id_orden_vieja, "i": id_insumo},
            )
        )
    )
    assert cantidad_vieja == Decimal("0.030")

    cambiar_cantidad_receta(id_producto, id_insumo, Decimal("0.100"))
    _crear_cuenta(mesa=2, producto={
        "id_producto": id_producto,
        "cantidad": 1,
        "precio_unitario": 40.00,
        "id_area_preparacion": 2,
    })

    assert Decimal(
        str(
            _escalar(
                "SELECT cantidad FROM movimientos_inventario "
                "WHERE id_orden = :o AND id_insumo = :i",
                {"o": id_orden_vieja, "i": id_insumo},
            )
        )
    ) == Decimal("0.030")
    assert Decimal(
        str(
            _escalar(
                "SELECT cantidad FROM movimientos_inventario "
                "WHERE id_orden = (SELECT MAX(id_orden) FROM ordenes) "
                "AND id_insumo = :i",
                {"i": id_insumo},
            )
        )
    ) == Decimal("0.100")


def test_cambio_de_precio_solo_aplica_a_ordenes_nuevas():
    id_orden_vieja, id_detalle_viejo = _crear_cuenta(mesa=1)
    producto = _filas(
        "SELECT nombre, descripcion, id_categoria, id_area_preparacion "
        "FROM productos WHERE id_producto = 1"
    )[0]
    actualizar_producto(
        id_producto=1,
        nombre=producto[0],
        descripcion=producto[1],
        id_categoria=int(producto[2]),
        id_area_preparacion=int(producto[3]),
        precio=Decimal("999.00"),
        disponible=True,
    )
    id_orden_nueva, id_detalle_nuevo = _crear_cuenta(
        mesa=2,
        producto={
            "id_producto": 1,
            "cantidad": 1,
            "precio_unitario": 999.00,
            "id_area_preparacion": 1,
        },
    )

    assert Decimal(
        str(
            _escalar(
                "SELECT precio_unitario FROM detalles_orden "
                "WHERE id_detalle = :d",
                {"d": id_detalle_viejo},
            )
        )
    ) == Decimal("120.00")
    assert Decimal(
        str(
            _escalar(
                "SELECT precio_unitario FROM detalles_orden "
                "WHERE id_detalle = :d",
                {"d": id_detalle_nuevo},
            )
        )
    ) == Decimal("999.00")
    assert id_orden_vieja != id_orden_nueva


def test_porciones_disponibles_calcula_minimo_y_sin_receta():
    receta = _receta(1)
    for id_insumo, cantidad in receta.items():
        _ejecutar(
            "UPDATE insumos SET stock_actual = :s WHERE id_insumo = :i",
            {"s": cantidad * 2, "i": id_insumo},
        )
    assert porciones_disponibles(None)[1] == 2

    with get_engine().begin() as conn:
        id_sin_receta = int(
            conn.execute(
                text(
                    """
                    INSERT INTO productos
                        (nombre, descripcion, id_categoria,
                         id_area_preparacion, precio_actual, disponible)
                    VALUES ('Producto sin receta', NULL, 1, 1, 10.00, TRUE)
                    RETURNING id_producto
                    """
                )
            ).scalar_one()
        )
    assert porciones_disponibles(None)[id_sin_receta] is None

    _ejecutar(
        "UPDATE insumos SET stock_actual = 0 WHERE id_insumo = 1"
    )
    assert porciones_disponibles(None)[1] == 0


def test_producto_agotado_no_deja_orden_partida_ni_movimiento():
    _ejecutar("UPDATE insumos SET stock_actual = 0 WHERE id_insumo = 1")
    ordenes_antes = _escalar("SELECT COUNT(*) FROM ordenes")
    detalles_antes = _escalar("SELECT COUNT(*) FROM detalles_orden")
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    with pytest.raises(StockInsuficienteError):
        guardar_orden_mesa(
            id_mesa=1,
            partidas=[dict(PRODUCTO)],
            id_usuario=1,
            id_empleado=1,
        )

    assert _escalar("SELECT COUNT(*) FROM ordenes") == ordenes_antes
    assert _escalar("SELECT COUNT(*) FROM detalles_orden") == detalles_antes
    assert (
        _escalar("SELECT COUNT(*) FROM movimientos_inventario")
        == movimientos_antes
    )
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Disponible"


@pytest.mark.parametrize("id_estado", [2, 3, 4])
def test_eliminar_partida_no_pendiente_es_rechazada(id_estado: int):
    id_orden, id_detalle = _crear_cuenta()
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = :e WHERE id_detalle = :d",
        {"e": id_estado, "d": id_detalle},
    )
    stock_antes = _stock(_receta(1))

    with pytest.raises(
        VentaError,
        match="Este producto ya está en preparación; si se perdió, repórtalo como merma",
    ):
        eliminar_partida(
            id_detalle=id_detalle,
            id_usuario=1,
            id_empleado=1,
        )

    assert _stock(_receta(1)) == stock_antes
    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == 1


def test_eliminar_partida_pendiente_devuelve_exactamente_lo_descontado():
    receta = _receta(1)
    stock_inicial = _stock(receta)
    _, id_detalle = _crear_cuenta()

    assert _stock(receta) == {
        id_insumo: stock_inicial[id_insumo] - cantidad
        for id_insumo, cantidad in receta.items()
    }

    eliminar_partida(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
    )

    assert _stock(receta) == stock_inicial


def test_modificar_cantidad_sube_baja_y_rollback_sin_stock():
    receta = _receta(1)
    stock_inicial = _stock(receta)
    id_orden, id_detalle = _crear_cuenta()

    modificar_cantidad_partida(
        id_detalle=id_detalle,
        nueva_cantidad=3,
        id_usuario=1,
        id_empleado=1,
    )
    stock_subida = _stock(receta)
    assert _escalar(
        "SELECT cantidad FROM detalles_orden WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 3
    assert Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    ) == Decimal("360.00")
    for id_insumo, cantidad in receta.items():
        assert (
            stock_subida[id_insumo] - stock_inicial[id_insumo]
            == _movimientos_firmados(id_orden, id_insumo)
        )

    modificar_cantidad_partida(
        id_detalle=id_detalle,
        nueva_cantidad=1,
        id_usuario=1,
        id_empleado=1,
    )
    stock_bajada = _stock(receta)
    assert _escalar(
        "SELECT cantidad FROM detalles_orden WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1
    assert Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    ) == Decimal("120.00")
    for id_insumo in receta:
        assert (
            stock_bajada[id_insumo] - stock_inicial[id_insumo]
            == _movimientos_firmados(id_orden, id_insumo)
        )

    # Para el ROLLBACK se deja solo el stock que ya consumió la partida actual
    # en Carne Res; subir a 3 requiere más y debe fallar completo.
    _ejecutar(
        "UPDATE insumos SET stock_actual = 0.200 WHERE id_insumo = 1"
    )
    stock_antes_error = _stock(receta)
    movimientos_antes = _escalar(
        "SELECT COUNT(*) FROM movimientos_inventario WHERE id_orden = :o",
        {"o": id_orden},
    )
    with pytest.raises(StockInsuficienteError):
        modificar_cantidad_partida(
            id_detalle=id_detalle,
            nueva_cantidad=3,
            id_usuario=1,
            id_empleado=1,
        )
    assert _stock(receta) == stock_antes_error
    assert _escalar(
        "SELECT cantidad FROM detalles_orden WHERE id_detalle = :d",
        {"d": id_detalle},
    ) == 1
    assert Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    ) == Decimal("120.00")
    assert _escalar(
        "SELECT COUNT(*) FROM movimientos_inventario WHERE id_orden = :o",
        {"o": id_orden},
    ) == movimientos_antes


def test_editar_producto_con_nombre_duplicado_no_modifica_nada():
    original = _filas(
        "SELECT nombre, precio_actual FROM productos WHERE id_producto = 1"
    )[0]

    with pytest.raises(CatalogoError, match="Ya existe un producto con ese nombre"):
        actualizar_producto(
            id_producto=1,
            nombre="Hamburguesa BBQ",  # ya existe en el seed (producto 2)
            descripcion="Intento duplicado",
            id_categoria=1,
            id_area_preparacion=1,
            precio=Decimal("1.00"),
            disponible=False,
        )

    actual = _filas(
        "SELECT nombre, precio_actual FROM productos WHERE id_producto = 1"
    )[0]
    assert actual[0] == original[0]
    assert Decimal(str(actual[1])) == Decimal(str(original[1]))
