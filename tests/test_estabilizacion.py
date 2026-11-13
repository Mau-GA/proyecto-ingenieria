"""Pruebas de regresión de las fallas F2, F3, F4, F5, F6, F9 y F17."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import get_engine
from modules.inventario import _rechazar_mermas, _registrar_ajuste, _subir_mermas
from modules.usuarios import _crear_usuario_con_roles, _hash_password
from services.mermas import (
    MermaError,
    registrar_merma_detalle,
    registrar_merma_insumo,
    repetir_detalle_mermado,
)
from services.ventas import (
    VentaError,
    calcular_consumo,
    cobrar_orden_mesa,
    guardar_orden_mesa,
)

PARTIDA = {
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


def _crear_cuenta(mesa: int = 1) -> tuple[int, int]:
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=[dict(PARTIDA)],
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


def test_seed_consistente_y_sin_ordenes_abiertas():
    abiertas = _escalar(
        """
        SELECT COUNT(*)
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        WHERE e.es_final = FALSE
        """
    )
    assert abiertas == 0

    pagadas_sin_partidas = _escalar(
        """
        SELECT COUNT(*)
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        WHERE e.nombre = 'Pagada'
          AND NOT EXISTS (
              SELECT 1 FROM detalles_orden d WHERE d.id_orden = o.id_orden
          )
        """
    )
    assert pagadas_sin_partidas == 0

    totales = _escalar(
        """
        SELECT COUNT(*)
        FROM (
            SELECT o.id_orden, o.total
            FROM ordenes o
            LEFT JOIN detalles_orden d ON d.id_orden = o.id_orden
            GROUP BY o.id_orden, o.total
            HAVING o.total <> COALESCE(SUM(d.subtotal), 0)
        ) diferencias
        """
    )
    assert totales == 0

    pagos = _escalar(
        """
        SELECT COUNT(*)
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        WHERE e.nombre = 'Pagada'
          AND o.total <> (
              SELECT COALESCE(SUM(p.monto), 0)
              FROM pagos p WHERE p.id_orden = o.id_orden
          )
        """
    )
    assert pagos == 0

    estados_partidas = _filas(
        """
        SELECT DISTINCT ed.nombre
        FROM detalles_orden d
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        """
    )
    assert [fila[0] for fila in estados_partidas] == ["Entregado"]

    assert _escalar("SELECT COUNT(*) FROM mesas WHERE estado <> 'Disponible'") == 0
    assert _escalar(
        "SELECT COUNT(*) FROM ordenes WHERE numero_orden_diaria IS NULL"
    ) == 0
    assert _escalar(
        """
        SELECT COUNT(*)
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        WHERE o.id_orden = 18 AND e.nombre = 'Cancelada'
        """
    ) == 1


def test_doble_cobro_se_bloquea_sin_segundo_pago():
    id_orden, _ = _crear_cuenta()
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = 4 WHERE id_orden = :o",
        {"o": id_orden},
    )

    primera = cobrar_orden_mesa(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        id_usuario=1,
    )
    assert primera > 0

    with pytest.raises(
        VentaError, match="La orden ya fue cobrada o cancelada"
    ):
        cobrar_orden_mesa(
            id_orden=id_orden,
            id_mesa=1,
            id_metodo_pago=1,
            id_usuario=1,
        )

    assert _escalar(
        "SELECT COUNT(*) FROM pagos WHERE id_orden = :o", {"o": id_orden}
    ) == 1


def test_mesa_con_dos_ordenes_sigue_ocupada_hasta_cobrar_la_ultima():
    id_a, _ = _crear_cuenta(mesa=1)
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = 4 WHERE id_orden = :o",
        {"o": id_a},
    )

    numero = _escalar(
        "SELECT MAX(numero_orden_diaria) + 1 FROM ordenes "
        "WHERE DATE(fecha_orden) = CURRENT_DATE"
    )
    with get_engine().begin() as conn:
        id_b = conn.execute(
            text(
                """
                INSERT INTO ordenes
                    (id_tipo_orden, id_mesa, id_empleado_atiende,
                     id_usuario_registro, id_estado, total,
                     numero_orden_diaria)
                VALUES (1, 1, 1, 1, 1, 50, :numero)
                RETURNING id_orden
                """
            ),
            {"numero": int(numero or 1)},
        ).scalar_one()
    _ejecutar(
        """
        INSERT INTO detalles_orden
            (id_orden, id_producto, cantidad, precio_unitario,
             id_area_preparacion, id_estado)
        VALUES (:o, 1, 1, 50, 1, 4)
        """,
        {"o": int(id_b)},
    )

    cobrar_orden_mesa(
        id_orden=id_a,
        id_mesa=1,
        id_metodo_pago=1,
        id_usuario=1,
    )
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Ocupada"

    cobrar_orden_mesa(
        id_orden=int(id_b),
        id_mesa=1,
        id_metodo_pago=1,
        id_usuario=1,
    )
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Disponible"


def test_merma_de_partida_vendida_no_descuenta_stock_dos_veces():
    _, id_detalle = _crear_cuenta()
    insumo = int(
        _escalar(
            "SELECT id_insumo FROM recetas WHERE id_producto = 1 "
            "ORDER BY id_insumo LIMIT 1"
        )
    )
    stock_antes = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": insumo}))
    )
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    ids = registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se quemó",
        origen="orden",
        liberar_mesa_si_total=False,
    )

    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": insumo}))
    ) == stock_antes
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[])) "
        "AND id_movimiento IS NOT NULL",
        {"ids": ids},
    ) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria WHERE tabla_afectada = 'mermas_pendientes' "
        "AND id_registro_afectado = ANY(CAST(:ids AS integer[]))",
        {"ids": ids},
    ) == len(ids)

    _subir_mermas(ids)

    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": insumo}))
    ) == stock_antes
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[])) "
        "AND costo_estimado_perdida IS NOT NULL",
        {"ids": ids},
    ) == len(ids)



def test_rechazar_merma_pendiente_restaura_total():
    id_orden, id_detalle = _crear_cuenta()
    total_antes = Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    )

    ids = registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Error de captura",
        origen="orden",
        liberar_mesa_si_total=False,
    )
    assert Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    ) == Decimal("0.00")

    _rechazar_mermas(ids)

    assert Decimal(
        str(_escalar("SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}))
    ) == total_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[]))",
        {"ids": ids},
    ) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria WHERE tabla_afectada = "
        "'detalles_orden_merma' AND id_registro_afectado = :d",
        {"d": id_detalle},
    ) == 0


def test_rechazar_merma_con_reposicion_se_bloquea():
    _, id_detalle = _crear_cuenta()
    ids = registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se perdió",
        origen="orden",
        liberar_mesa_si_total=False,
        repetir=True,
    )

    with pytest.raises(
        VentaError, match="Esta merma ya tiene reposición; no se puede rechazar"
    ):
        _rechazar_mermas(ids)

    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[]))",
        {"ids": ids},
    ) == len(ids)


def test_merma_manual_de_insumo_descuenta_al_subir():
    id_insumo = 4
    stock_antes = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    )
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    ids = registrar_merma_insumo(
        id_insumo=id_insumo,
        cantidad=Decimal("0.100"),
        id_usuario=1,
        id_empleado=1,
        motivo="Caducidad",
    )

    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    ) == stock_antes
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[])) "
        "AND id_movimiento IS NULL",
        {"ids": ids},
    ) == 1

    _subir_mermas(ids)

    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    ) == stock_antes - Decimal("0.100")
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == (
        movimientos_antes + 1
    )
    assert _escalar(
        "SELECT id_movimiento FROM mermas WHERE id_merma = :id",
        {"id": ids[0]},
    ) is not None


def test_mermar_y_repetir_descuenta_receta_en_una_transaccion():
    id_orden, id_detalle = _crear_cuenta()
    with get_engine().connect() as conn:
        consumo = calcular_consumo(conn, [{"id_producto": 1, "cantidad": 1}])

    stock_antes = {
        int(i): Decimal(
            str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                         {"i": int(i)}))
        )
        for i in consumo
    }
    detalles_antes = _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    )

    ids = registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se perdió",
        origen="orden",
        liberar_mesa_si_total=False,
        repetir=True,
    )

    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == detalles_antes + 1
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria WHERE tabla_afectada = "
        "'detalles_orden_reposicion' AND id_registro_afectado = :d",
        {"d": id_detalle},
    ) == 1
    assert _escalar(
        "SELECT COUNT(*) FROM mermas WHERE id_merma = ANY(CAST(:ids AS integer[]))",
        {"ids": ids},
    ) == len(ids)
    for id_insumo, cantidad in consumo.items():
        assert Decimal(
            str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                         {"i": int(id_insumo)}))
        ) == stock_antes[int(id_insumo)] - cantidad



def test_repetir_detalle_mermado_descuenta_y_no_permite_dos_repeticiones():
    id_orden, id_detalle = _crear_cuenta()
    with get_engine().connect() as conn:
        consumo = calcular_consumo(conn, [{"id_producto": 1, "cantidad": 1}])
    stock_antes = {
        int(i): Decimal(
            str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                         {"i": int(i)}))
        )
        for i in consumo
    }

    registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se perdió",
        origen="orden",
        liberar_mesa_si_total=False,
    )
    detalles_antes = _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    )

    repetir_detalle_mermado(id_detalle=id_detalle)

    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == detalles_antes + 1
    for id_insumo, cantidad in consumo.items():
        assert Decimal(
            str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                         {"i": int(id_insumo)}))
        ) == stock_antes[int(id_insumo)] - cantidad

    with pytest.raises(MermaError, match="ya tiene una reposición"):
        repetir_detalle_mermado(id_detalle=id_detalle)


def test_mermar_y_repetir_sin_stock_no_deja_nada():
    id_orden, id_detalle = _crear_cuenta()
    insumo = int(
        _escalar(
            "SELECT id_insumo FROM recetas WHERE id_producto = 1 "
            "ORDER BY id_insumo LIMIT 1"
        )
    )
    _ejecutar(
        "UPDATE insumos SET stock_actual = 0 WHERE id_insumo = :i",
        {"i": insumo},
    )
    detalles_antes = _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    )
    mermas_antes = _escalar("SELECT COUNT(*) FROM mermas")

    with pytest.raises(MermaError, match="No se puede repetir"):
        registrar_merma_detalle(
            id_detalle=id_detalle,
            id_usuario=1,
            id_empleado=1,
            motivo="Se perdió",
            origen="orden",
            liberar_mesa_si_total=False,
            repetir=True,
        )

    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == detalles_antes
    assert _escalar("SELECT COUNT(*) FROM mermas") == mermas_antes
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria WHERE tabla_afectada = "
        "'mermas_pendientes' AND (datos_nuevos->>'id_detalle')::int = :d",
        {"d": id_detalle},
    ) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM auditoria WHERE tabla_afectada = "
        "'detalles_orden_reposicion' AND id_registro_afectado = :d",
        {"d": id_detalle},
    ) == 0


def test_ajuste_negativo_sin_stock_no_inserta_movimiento():
    id_insumo = 4
    stock = Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    )
    movimientos_antes = _escalar("SELECT COUNT(*) FROM movimientos_inventario")

    with pytest.raises(VentaError, match="No se puede aplicar el ajuste negativo"):
        _registrar_ajuste(
            id_insumo=id_insumo,
            tipo="Ajuste negativo (salida)",
            cantidad=stock + Decimal("1"),
            motivo="Prueba",
            id_empleado=1,
        )

    assert Decimal(
        str(_escalar("SELECT stock_actual FROM insumos WHERE id_insumo = :i",
                     {"i": id_insumo}))
    ) == stock
    assert _escalar("SELECT COUNT(*) FROM movimientos_inventario") == movimientos_antes


def test_producto_duplicado_viola_unicidad():
    with pytest.raises(IntegrityError):
        _ejecutar(
            """
            INSERT INTO productos
                (nombre, id_categoria, id_area_preparacion,
                 precio_actual, disponible)
            VALUES ('Hamburguesa Clasica', 1, 1, 100, TRUE)
            """
        )


def test_usuario_duplicado_y_rol_invalido_no_dejan_usuario():
    with pytest.raises(IntegrityError):
        _crear_usuario_con_roles(
            id_empleado=None,
            nombre_usuario="admin",
            correo=None,
            password_hash=_hash_password("clave12345"),
            ids_roles=[2],
        )

    with pytest.raises(IntegrityError):
        _crear_usuario_con_roles(
            id_empleado=None,
            nombre_usuario="usuario_rol_invalido",
            correo=None,
            password_hash=_hash_password("clave12345"),
            ids_roles=[99999],
        )

    assert _escalar(
        "SELECT COUNT(*) FROM usuarios WHERE nombre_usuario = "
        "'usuario_rol_invalido'"
    ) == 0
