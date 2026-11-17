"""Pruebas de la Fase 7: pagos divididos y reportes de ventas/compras."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier

import bcrypt
import pytest
from sqlalchemy import text

from database import get_engine
from services.cancelaciones import CancelacionError, cancelar_orden
from services.compras_semanales import (
    csv_compras,
    pdf_compras,
    sugerencia_compras,
)
from services.ventas import (
    VentaError,
    cobrar_orden_mesa,
    eliminar_partida,
    guardar_orden_mesa,
    registrar_pago_parcial,
)
from services.ventas_reportes import COLUMNAS_CSV, csv_ventas, datos_ventas, pdf_ventas

PASSWORD_ADMIN = "ClaveAdmin123"
PARTIDA = {
    "id_producto": 1,
    "cantidad": 1,
    "precio_unitario": 300.00,
    "id_area_preparacion": 1,
}


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=4)
    ).decode("utf-8")


def _preparar_admin() -> None:
    _ejecutar(
        "UPDATE usuarios SET password_hash = :hash WHERE nombre_usuario = 'admin'",
        {"hash": _hash(PASSWORD_ADMIN)},
    )


def _crear_orden(precios: list[float], id_producto: int = 1, mesa: int = 1):
    partidas = [
        {
            "id_producto": id_producto,
            "cantidad": 1,
            "precio_unitario": precio,
            "id_area_preparacion": 1,
        }
        for precio in precios
    ]
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=partidas,
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


def _marcar_entregadas(id_orden: int) -> None:
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = 4 WHERE id_orden = :o",
        {"o": id_orden},
    )


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


def _pagos(id_orden: int) -> int:
    return int(
        _escalar(
            "SELECT COUNT(*) FROM pagos WHERE id_orden = :o", {"o": id_orden}
        )
    )


def _crear_orden_controlada(
    total: Decimal, estado: str, numero: int, fecha: datetime
) -> int:
    with get_engine().begin() as conn:
        id_estado = conn.execute(
            text("SELECT id_estado FROM estados_orden WHERE nombre = :n"),
            {"n": estado},
        ).scalar_one()
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO ordenes
                        (id_tipo_orden, id_usuario_registro, id_estado, total,
                         numero_orden_diaria, fecha_orden)
                    VALUES (2, 1, :estado, :total, :numero, :fecha)
                    RETURNING id_orden
                    """
                ),
                {
                    "estado": int(id_estado),
                    "total": total,
                    "numero": numero,
                    "fecha": fecha,
                },
            ).scalar_one()
        )


def _agregar_detalle_controlado(
    id_orden: int, id_producto: int, precio: Decimal, estado: str
) -> None:
    with get_engine().begin() as conn:
        id_estado = conn.execute(
            text("SELECT id_estado FROM estados_detalle_orden WHERE nombre = :n"),
            {"n": estado},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO detalles_orden
                    (id_orden, id_producto, cantidad, precio_unitario,
                     id_area_preparacion, id_estado)
                VALUES (:orden, :producto, 1, :precio, 1, :estado)
                """
            ),
            {
                "orden": id_orden,
                "producto": id_producto,
                "precio": precio,
                "estado": int(id_estado),
            },
        )


def _agregar_pago_controlado(
    id_orden: int,
    monto: Decimal,
    fecha: datetime,
    id_metodo: int = 1,
    referencia: str | None = None,
) -> None:
    _ejecutar(
        """
        INSERT INTO pagos
            (id_orden, id_metodo_pago, monto, referencia_pago,
             fecha_pago, id_usuario_registro)
        VALUES (:orden, :metodo, :monto, :ref, :fecha, 1)
        """,
        {
            "orden": id_orden,
            "metodo": id_metodo,
            "monto": monto,
            "ref": referencia,
            "fecha": fecha,
        },
    )


def test_pago_dividido_cierra_orden_y_libera_mesa():
    id_orden, _ = _crear_orden([300.0])

    primero = registrar_pago_parcial(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        monto=100,
        id_usuario=1,
    )
    assert primero["saldo_restante"] == Decimal("200.00")
    assert primero["pagada"] is False
    assert _estado_orden(id_orden) != "Pagada"
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Ocupada"

    _marcar_entregadas(id_orden)
    segundo = registrar_pago_parcial(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=2,
        monto=200,
        id_usuario=1,
        referencia_pago="TARJETA-123",
    )
    assert segundo["saldo_restante"] == Decimal("0.00")
    assert segundo["pagada"] is True
    assert len(segundo["pagos"]) == 2
    assert _estado_orden(id_orden) == "Pagada"
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Disponible"
    assert _pagos(id_orden) == 2
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PAGO'"
    ) == 2


def test_abonos_invalidos_no_insertan_nada():
    id_orden, _ = _crear_orden([300.0])

    with pytest.raises(VentaError, match="supera el saldo"):
        registrar_pago_parcial(
            id_orden=id_orden,
            id_mesa=1,
            id_metodo_pago=1,
            monto=301,
            id_usuario=1,
        )
    for monto in (0, -5):
        with pytest.raises(VentaError, match="mayor a cero"):
            registrar_pago_parcial(
                id_orden=id_orden,
                id_mesa=1,
                id_metodo_pago=1,
                monto=monto,
                id_usuario=1,
            )
    with pytest.raises(VentaError, match="requiere referencia"):
        registrar_pago_parcial(
            id_orden=id_orden,
            id_mesa=1,
            id_metodo_pago=2,
            monto=100,
            id_usuario=1,
        )

    assert _pagos(id_orden) == 0
    assert _escalar(
        "SELECT COUNT(*) FROM bitacora WHERE tipo_evento = 'PAGO'"
    ) == 0


def test_abono_final_bloquea_si_hay_partidas_pendientes():
    id_orden, _ = _crear_orden([300.0])

    with pytest.raises(VentaError, match="productos pendientes"):
        registrar_pago_parcial(
            id_orden=id_orden,
            id_mesa=1,
            id_metodo_pago=1,
            monto=300,
            id_usuario=1,
        )
    assert _pagos(id_orden) == 0
    assert _estado_orden(id_orden) != "Pagada"

    _marcar_entregadas(id_orden)
    resultado = registrar_pago_parcial(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        monto=300,
        id_usuario=1,
    )
    assert resultado["pagada"] is True


def test_dos_abonos_concurrentes_solo_entra_el_que_cabe():
    id_orden, _ = _crear_orden([300.0])
    barrera = Barrier(2)

    def abonar():
        barrera.wait()
        try:
            registrar_pago_parcial(
                id_orden=id_orden,
                id_mesa=1,
                id_metodo_pago=1,
                monto=200,
                id_usuario=1,
            )
            return "ok"
        except VentaError:
            return "rechazado"

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: abonar(), range(2)))

    assert sorted(resultados) == ["ok", "rechazado"]
    assert _pagos(id_orden) == 1
    assert Decimal(str(_escalar(
        "SELECT SUM(monto) FROM pagos WHERE id_orden = :o", {"o": id_orden}
    ))) == Decimal("200.00")
    assert _estado_orden(id_orden) != "Pagada"


def test_pagos_parciales_bloquean_reducciones_y_cancelacion():
    _preparar_admin()
    id_orden, ids = _crear_orden([50.0, 250.0])
    registrar_pago_parcial(
        id_orden=id_orden,
        id_mesa=1,
        id_metodo_pago=1,
        monto=100,
        id_usuario=1,
    )

    with pytest.raises(VentaError, match="no se puede reducir"):
        eliminar_partida(id_detalle=ids[1], id_usuario=1, id_empleado=1)
    assert _escalar(
        "SELECT COUNT(*) FROM detalles_orden WHERE id_orden = :o",
        {"o": id_orden},
    ) == 2

    guardar_orden_mesa(
        id_mesa=1,
        partidas=[
            {
                "id_producto": 1,
                "cantidad": 1,
                "precio_unitario": 100.0,
                "id_area_preparacion": 1,
            }
        ],
        id_usuario=1,
        id_empleado=1,
    )
    assert _escalar(
        "SELECT total FROM ordenes WHERE id_orden = :o", {"o": id_orden}
    ) == Decimal("400.00")

    with pytest.raises(CancelacionError, match="pagos registrados"):
        cancelar_orden(
            id_orden=id_orden,
            id_motivo=1,
            id_usuario_solicita=1,
            usuario_autoriza="admin",
            password_autoriza=PASSWORD_ADMIN,
        )


def test_cobro_completo_y_doble_cobro_concurrente_siguen_igual():
    id_orden, _ = _crear_orden([300.0])
    _marcar_entregadas(id_orden)
    barrera = Barrier(2)

    def cobrar():
        barrera.wait()
        try:
            return cobrar_orden_mesa(
                id_orden=id_orden,
                id_mesa=1,
                id_metodo_pago=1,
                id_usuario=1,
            )
        except VentaError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: cobrar(), range(2)))

    exitos = [r for r in resultados if r is not None]
    assert exitos == [Decimal("300.00")]
    assert _pagos(id_orden) == 1
    assert _estado_orden(id_orden) == "Pagada"
    assert _escalar("SELECT estado FROM mesas WHERE id_mesa = 1") == "Disponible"


def _datos_reporte_controlados():
    hoy = date.today()
    fecha = datetime.combine(hoy, time(10, 0))
    orden_100 = _crear_orden_controlada(
        Decimal("100.00"), "Pagada", 101, fecha
    )
    _agregar_detalle_controlado(orden_100, 1, Decimal("100.00"), "Entregado")
    _agregar_pago_controlado(orden_100, Decimal("100.00"), fecha, 1)

    orden_300 = _crear_orden_controlada(
        Decimal("300.00"), "Pagada", 102, datetime.combine(hoy, time(14, 0))
    )
    _agregar_detalle_controlado(orden_300, 2, Decimal("300.00"), "Entregado")
    _agregar_pago_controlado(
        orden_300,
        Decimal("300.00"),
        datetime.combine(hoy, time(14, 0)),
        2,
        "TARJETA-1",
    )

    orden_500 = _crear_orden_controlada(
        Decimal("500.00"), "Cancelada", 103, datetime.combine(hoy, time(16, 0))
    )
    _agregar_detalle_controlado(orden_500, 3, Decimal("500.00"), "Cancelado")
    return hoy


def test_reporte_ventas_metricas_y_filtro_de_horas():
    hoy = _datos_reporte_controlados()

    datos = datos_ventas(hoy, hoy, 0, 23)
    assert datos["metricas"]["total_vendido"] == 400.0
    assert datos["metricas"]["ordenes"] == 2
    assert datos["metricas"]["ticket_promedio"] == 200.0
    assert datos["metricas"]["unidades"] == 2
    assert "Hamburguesa Doble" not in set(datos["detalles"]["producto"])

    filtrado = datos_ventas(hoy, hoy, 13, 17)
    assert filtrado["metricas"]["total_vendido"] == 300.0
    assert filtrado["metricas"]["ordenes"] == 1
    assert filtrado["metricas"]["ticket_promedio"] == 300.0
    assert set(filtrado["detalles"]["producto"]) == {"Hamburguesa BBQ"}

    por_producto = datos_ventas(hoy, hoy, 0, 23, id_producto=2)
    assert por_producto["metricas"]["total_vendido"] == 300.0
    assert por_producto["metricas"]["ordenes"] == 1
    assert por_producto["metricas"]["unidades"] == 1
    assert set(por_producto["detalles"]["producto"]) == {"Hamburguesa BBQ"}


def test_reporte_sin_ventas_no_divide_entre_cero():
    datos = datos_ventas(date(2000, 1, 1), date(2000, 1, 2), 0, 23)
    assert datos["metricas"] == {
        "total_vendido": 0.0,
        "ordenes": 0,
        "ticket_promedio": 0.0,
        "unidades": 0,
    }
    assert pdf_ventas(datos, {"desde": "2000-01-01", "hasta": "2000-01-02"}).startswith(
        b"%PDF"
    )


def test_csv_y_pdf_de_ventas():
    hoy = _datos_reporte_controlados()
    datos = datos_ventas(hoy, hoy, 0, 23)

    csv = csv_ventas(datos).decode("utf-8-sig")
    encabezado = csv.splitlines()[0].split(",")
    assert encabezado == COLUMNAS_CSV
    assert "Hamburguesa Clasica" in csv
    assert "Hamburguesa BBQ" in csv

    pdf = pdf_ventas(
        datos,
        {
            "desde": hoy,
            "hasta": hoy,
            "hora_desde": 0,
            "hora_hasta": 23,
            "producto": "Todos los productos",
        },
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 500


def _movimiento(
    id_insumo: int,
    cantidad: str,
    dias_atras: int,
    id_tipo: int = 2,
    id_orden: int | None = None,
) -> None:
    _ejecutar(
        """
        INSERT INTO movimientos_inventario
            (id_insumo, id_tipo_movimiento, cantidad, fecha_movimiento,
             motivo, id_empleado, id_orden)
        VALUES (:insumo, :tipo, :cantidad,
                CURRENT_DATE - CAST(:dias AS integer),
                'Prueba fase 7', 1, :orden)
        """,
        {
            "insumo": id_insumo,
            "tipo": id_tipo,
            "cantidad": Decimal(cantidad),
            "dias": int(dias_atras),
            "orden": id_orden,
        },
    )


def test_compras_semanales_ponderacion_necesidad_y_ultimo_costo():
    for cantidad, dias in (("10", 27), ("20", 20), ("30", 13), ("40", 6)):
        _movimiento(1, cantidad, dias, 2)
    _ejecutar(
        "UPDATE insumos SET stock_actual = 5, stock_minimo = 10 "
        "WHERE id_insumo = 1"
    )
    with get_engine().begin() as conn:
        id_compra = conn.execute(
            text(
                """
                INSERT INTO compras (id_proveedor, id_empleado, total,
                                     fecha_compra)
                VALUES (1, 1, 60, CURRENT_TIMESTAMP)
                RETURNING id_compra
                """
            )
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO detalle_compra
                    (id_compra, id_insumo, cantidad, costo_unitario)
                VALUES (:compra, 1, 8, 7.50)
                """
            ),
            {"compra": int(id_compra)},
        )

    sugerencia = sugerencia_compras(semanas=4)
    fila = sugerencia[sugerencia["id_insumo"] == 1].iloc[0]
    assert fila["consumo_semana_1"] == Decimal("10.000")
    assert fila["consumo_semana_2"] == Decimal("20.000")
    assert fila["consumo_semana_3"] == Decimal("30.000")
    assert fila["consumo_semana_4"] == Decimal("40.000")
    assert fila["necesidad"] == Decimal("35.000")
    assert fila["proveedor"] == "Carnes del Norte"
    assert fila["origen_costo"] == "último precio de compra"
    assert fila["costo_unitario"] == Decimal("7.50")
    assert fila["costo_estimado"] == Decimal("262.50")


def test_compras_sin_proveedor_y_necesidad_cero():
    with get_engine().begin() as conn:
        id_nuevo = int(
            conn.execute(
                text(
                    """
                    INSERT INTO insumos
                        (nombre, id_categoria, id_unidad_medida,
                         stock_actual, stock_minimo,
                         costo_unitario_referencia, activo)
                    VALUES ('Insumo sin proveedor', 4, 1, 0, 10, 5.00, TRUE)
                    RETURNING id_insumo
                    """
                )
            ).scalar_one()
        )

    sugerencia = sugerencia_compras(semanas=4)
    fila = sugerencia[sugerencia["id_insumo"] == id_nuevo].iloc[0]
    assert fila["proveedor"] == "Sin proveedor asignado"
    assert fila["necesidad"] == Decimal("10.000")
    assert fila["origen_costo"] == "costo de referencia"
    assert 1 not in set(sugerencia["id_insumo"])


def test_devoluciones_de_ventas_no_cuentan_como_consumo():
    _preparar_admin()
    _, ids = _crear_orden([35.0], id_producto=7)
    eliminar_partida(id_detalle=ids[0], id_usuario=1, id_empleado=1)

    id_orden, _ = _crear_orden([35.0], id_producto=7)
    cancelar_orden(
        id_orden=id_orden,
        id_motivo=1,
        id_usuario_solicita=1,
        usuario_autoriza="admin",
        password_autoriza=PASSWORD_ADMIN,
    )

    sugerencia = sugerencia_compras(semanas=4)
    assert 10 not in set(sugerencia["id_insumo"])


def test_exportaciones_de_compras():
    _movimiento(8, "40", 6, 2)
    sugerencia = sugerencia_compras(semanas=4)
    assert not sugerencia.empty

    csv = csv_compras(sugerencia).decode("utf-8-sig")
    assert "Proveedor,Insumo,Unidad" in csv
    assert "Bebidas Express" in csv

    pdf = pdf_compras(sugerencia, semanas=4)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 500
