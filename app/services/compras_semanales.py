"""Sugerencia de compras semanales a partir del consumo real (P7-3).

Consumo de cada semana = salidas por venta + mermas aplicadas - devoluciones
de ventas. Las devoluciones son los movimientos de ``Ajuste positivo`` con
``id_orden`` (los genera ``_revertir_inventario`` al eliminar/reducir una
partida o cancelar una orden). No se cuentan compras ni ajustes manuales.

Las cuatro semanas son ventanas móviles de 7 días que terminan hoy. La semana
más reciente pesa 4, la anterior 3, luego 2 y la más antigua 1. El stock de
seguridad es el ``stock_minimo`` del insumo y la existencia disponible usa
``stock_utilizable_por_insumo`` para excluir lotes caducados.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import get_engine
from services.lotes import stock_utilizable_por_insumo
from services.reportes_pdf import crear_pdf, parrafo, salida_pdf, tabla

CERO = Decimal("0")
TRES_DECIMALES = Decimal("0.001")


def _decimal(valor) -> Decimal:
    return Decimal(str(valor or 0))


def _sugerencia(conn: Connection, semanas: int) -> pd.DataFrame:
    hoy = conn.execute(text("SELECT CURRENT_DATE")).scalar_one()
    inicio = hoy - timedelta(days=7 * semanas - 1)

    movimientos = conn.execute(
        text(
            """
            SELECT m.id_insumo, m.fecha_movimiento::date AS fecha,
                   m.cantidad, m.id_orden, t.nombre AS tipo
            FROM movimientos_inventario m
            JOIN tipos_movimiento_inventario t
              ON t.id_tipo_movimiento = m.id_tipo_movimiento
            WHERE m.fecha_movimiento >= :inicio
              AND m.fecha_movimiento < CURRENT_DATE + INTERVAL '1 day'
              AND (
                  t.nombre IN ('Salida por venta', 'Merma')
                  OR (t.nombre = 'Ajuste positivo' AND m.id_orden IS NOT NULL)
              )
            ORDER BY m.fecha_movimiento, m.id_movimiento
            """
        ),
        {"inicio": inicio},
    ).mappings().all()

    consumos: dict[int, list[Decimal]] = {}
    for fila in movimientos:
        delta = (hoy - fila["fecha"]).days
        if delta < 0:
            continue
        indice = semanas - 1 - (delta // 7)
        if indice < 0 or indice >= semanas:
            continue
        id_insumo = int(fila["id_insumo"])
        valores = consumos.setdefault(id_insumo, [CERO] * semanas)
        cantidad = _decimal(fila["cantidad"])
        if str(fila["tipo"]) == "Ajuste positivo":
            cantidad = -cantidad
        valores[indice] += cantidad

    insumos = conn.execute(
        text(
            """
            SELECT i.id_insumo, i.nombre AS insumo, u.abreviatura AS unidad,
                   i.stock_minimo, i.costo_unitario_referencia
            FROM insumos i
            JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
            WHERE i.activo = TRUE
            ORDER BY i.nombre
            """
        )
    ).mappings().all()
    ids = [int(fila["id_insumo"]) for fila in insumos]
    utilizables = stock_utilizable_por_insumo(conn, ids) if ids else {}

    proveedores = {}
    for fila in conn.execute(
        text(
            """
            SELECT DISTINCT ON (pi.id_insumo)
                   pi.id_insumo, p.id_proveedor, p.nombre,
                   pi.costo_acordado
            FROM proveedor_insumos pi
            JOIN proveedores p ON p.id_proveedor = pi.id_proveedor
            LEFT JOIN detalle_compra dc ON dc.id_insumo = pi.id_insumo
            LEFT JOIN compras c
              ON c.id_compra = dc.id_compra
             AND c.id_proveedor = pi.id_proveedor
            WHERE pi.activo = TRUE AND p.activo = TRUE
            ORDER BY pi.id_insumo,
                     c.fecha_compra DESC NULLS LAST,
                     pi.costo_acordado ASC
            """
        )
    ).mappings().all():
        proveedores[int(fila["id_insumo"])] = dict(fila)

    ultimo_precio: dict[int, Decimal] = {}
    for fila in conn.execute(
        text(
            """
            SELECT DISTINCT ON (dc.id_insumo)
                   dc.id_insumo, dc.costo_unitario
            FROM detalle_compra dc
            JOIN compras c ON c.id_compra = dc.id_compra
            ORDER BY dc.id_insumo, c.fecha_compra DESC,
                     dc.id_detalle_compra DESC
            """
        )
    ).mappings().all():
        ultimo_precio[int(fila["id_insumo"])] = _decimal(
            fila["costo_unitario"]
        )

    denominador = Decimal(str(semanas * (semanas + 1) // 2))
    filas_salida: list[dict] = []
    for insumo in insumos:
        id_insumo = int(insumo["id_insumo"])
        consumos_insumo = consumos.get(id_insumo, [CERO] * semanas)
        esperado = (
            sum(
                (consumos_insumo[i] * Decimal(i + 1) for i in range(semanas)),
                CERO,
            )
            / denominador
        ).quantize(TRES_DECIMALES, rounding=ROUND_HALF_UP)
        minimo = _decimal(insumo["stock_minimo"])
        utilizable = utilizables.get(id_insumo, CERO)
        necesidad = max(CERO, esperado + minimo - utilizable).quantize(
            TRES_DECIMALES, rounding=ROUND_HALF_UP
        )

        if necesidad <= 0:
            continue

        proveedor = proveedores.get(id_insumo)
        nombre_proveedor = (
            str(proveedor["nombre"]) if proveedor else "Sin proveedor asignado"
        )
        if id_insumo in ultimo_precio:
            costo = ultimo_precio[id_insumo]
            origen = "último precio de compra"
        elif proveedor:
            costo = _decimal(proveedor["costo_acordado"])
            origen = "costo acordado"
        elif insumo["costo_unitario_referencia"] is not None:
            costo = _decimal(insumo["costo_unitario_referencia"])
            origen = "costo de referencia"
        else:
            costo = CERO
            origen = "sin costo"

        costo_estimado = (necesidad * costo).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        fila = {
            "id_insumo": id_insumo,
            "insumo": str(insumo["insumo"]),
            "unidad": str(insumo["unidad"]),
            "proveedor": nombre_proveedor,
            "stock_seguridad": minimo,
            "stock_utilizable": _decimal(utilizable),
            "necesidad": necesidad,
            "costo_unitario": costo,
            "origen_costo": origen,
            "costo_estimado": costo_estimado,
        }
        for indice, cantidad in enumerate(consumos_insumo, start=1):
            fila[f"consumo_semana_{indice}"] = cantidad.quantize(
                TRES_DECIMALES, rounding=ROUND_HALF_UP
            )
        filas_salida.append(fila)

    columnas = [
        "id_insumo",
        "insumo",
        "unidad",
        "proveedor",
        *[f"consumo_semana_{i}" for i in range(1, semanas + 1)],
        "stock_seguridad",
        "stock_utilizable",
        "necesidad",
        "costo_unitario",
        "origen_costo",
        "costo_estimado",
    ]
    if not filas_salida:
        return pd.DataFrame(columns=columnas)
    return pd.DataFrame(filas_salida)[columnas].sort_values(
        ["proveedor", "insumo"]
    ).reset_index(drop=True)


def sugerencia_compras(
    conn: Connection | None = None, semanas: int = 4
) -> pd.DataFrame:
    """Calcula la sugerencia para los insumos activos con necesidad > 0."""
    if int(semanas) < 1:
        raise ValueError("El número de semanas debe ser al menos 1.")
    if conn is not None:
        return _sugerencia(conn, int(semanas))
    with get_engine().connect() as propia:
        return _sugerencia(propia, int(semanas))


COLUMNAS_CSV = {
    "proveedor": "Proveedor",
    "insumo": "Insumo",
    "unidad": "Unidad",
    "consumo_semana_1": "Consumo semana 1",
    "consumo_semana_2": "Consumo semana 2",
    "consumo_semana_3": "Consumo semana 3",
    "consumo_semana_4": "Consumo semana 4",
    "stock_seguridad": "Stock de seguridad",
    "stock_utilizable": "Stock utilizable",
    "necesidad": "Necesidad",
    "costo_unitario": "Costo unitario",
    "origen_costo": "Origen del costo",
    "costo_estimado": "Costo estimado",
}


def csv_compras(sugerencia: pd.DataFrame) -> bytes:
    """CSV ordenado por proveedor con las columnas visibles del reporte."""
    columnas = [c for c in COLUMNAS_CSV if c in sugerencia.columns]
    return (
        sugerencia[columnas]
        .rename(columns=COLUMNAS_CSV)
        .to_csv(index=False)
        .encode("utf-8-sig")
    )


def pdf_compras(sugerencia: pd.DataFrame, semanas: int = 4) -> bytes:
    """PDF agrupado por proveedor, con subtotal y total general."""
    pdf = crear_pdf(
        "Compras semanales sugeridas",
        f"Consumo ponderado de las últimas {int(semanas)} semanas "
        "(la más reciente pesa más).",
    )
    if sugerencia.empty:
        parrafo(pdf, "No hay insumos con necesidad de compra.")
        return salida_pdf(pdf)

    total_general = Decimal("0")
    for proveedor, grupo in sugerencia.groupby("proveedor", sort=True):
        parrafo(pdf, f"Proveedor: {proveedor}", negrita=True, tamano=11)
        filas = []
        subtotal = Decimal("0")
        for _, fila in grupo.iterrows():
            subtotal += _decimal(fila["costo_estimado"])
            filas.append(
                [
                    fila["insumo"],
                    f"{float(fila['necesidad']):.3f} {fila['unidad']}",
                    f"${float(fila['costo_unitario']):.2f}",
                    f"${float(fila['costo_estimado']):.2f}",
                ]
            )
        total_general += subtotal
        tabla(
            pdf,
            ["Insumo", "Necesidad", "Costo unitario", "Costo estimado"],
            filas,
            anchos=[80, 40, 35, 35],
        )
        parrafo(pdf, f"Subtotal: ${subtotal:.2f}", negrita=True)

    parrafo(pdf, f"Total general: ${total_general:.2f}", negrita=True, tamano=11)
    return salida_pdf(pdf)
