"""Datos, CSV y PDF del reporte de ventas (P7-2).

Criterio documentado: una venta cuenta solo si su orden está en estado
``Pagada`` y tiene al menos un pago cuya ``fecha_pago`` cae en el rango de
fechas y horas elegido. El dinero se suma únicamente de esos pagos. Los
productos y unidades salen de las partidas de esas órdenes, excluyendo las
partidas en estado ``Cancelado``. Si se filtra por producto, solo se incluyen
órdenes que tengan al menos una partida activa de ese producto.

La hora se extrae en PostgreSQL con ``EXTRACT(HOUR FROM fecha_pago)``, por lo
que se respeta la zona horaria configurada en la base.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from database import run_query
from services.reportes_pdf import crear_pdf, parrafo, salida_pdf, tabla

COLUMNAS_CSV = [
    "Orden",
    "ID orden",
    "Fecha orden",
    "Producto",
    "Cantidad",
    "Precio unitario",
    "Subtotal",
    "Métodos de pago",
    "Pagado en rango",
]


def _dinero(valor) -> float:
    return float(Decimal(str(valor or 0)))


def _vacio() -> dict:
    return {
        "pagos": pd.DataFrame(),
        "detalles": pd.DataFrame(),
        "por_dia": pd.DataFrame(columns=["fecha", "total"]),
        "por_hora": pd.DataFrame(
            {"hora": list(range(24)), "total": [0.0] * 24}
        ),
        "por_metodo": pd.DataFrame(columns=["metodo", "total"]),
        "top_unidades": pd.DataFrame(columns=["producto", "unidades"]),
        "top_importe": pd.DataFrame(columns=["producto", "importe"]),
        "detalle": pd.DataFrame(columns=COLUMNAS_CSV),
        "metricas": {
            "total_vendido": 0.0,
            "ordenes": 0,
            "ticket_promedio": 0.0,
            "unidades": 0,
        },
    }


def datos_ventas(
    desde: date,
    hasta: date,
    hora_desde: int = 0,
    hora_hasta: int = 23,
    id_producto: int | None = None,
) -> dict:
    """Consulta y prepara todos los datos del reporte de ventas."""
    producto = int(id_producto or 0)
    params = {
        "desde": desde,
        "hasta": hasta,
        "hora_desde": int(hora_desde),
        "hora_hasta": int(hora_hasta),
        "producto": producto,
    }

    pagos = run_query(
        """
        SELECT p.id_pago, p.id_orden, p.monto, p.fecha_pago,
               p.referencia_pago, mp.nombre AS metodo,
               o.numero_orden_diaria, o.fecha_orden
        FROM pagos p
        JOIN ordenes o ON o.id_orden = p.id_orden
        JOIN estados_orden e ON e.id_estado = o.id_estado
        JOIN metodos_pago mp ON mp.id_metodo_pago = p.id_metodo_pago
        WHERE e.nombre = 'Pagada'
          AND DATE(p.fecha_pago) BETWEEN :desde AND :hasta
          AND EXTRACT(HOUR FROM p.fecha_pago)
              BETWEEN :hora_desde AND :hora_hasta
          AND (
              :producto = 0
              OR EXISTS (
                  SELECT 1
                  FROM detalles_orden dx
                  JOIN estados_detalle_orden ex ON ex.id_estado = dx.id_estado
                  WHERE dx.id_orden = o.id_orden
                    AND dx.id_producto = :producto
                    AND ex.nombre <> 'Cancelado'
              )
          )
        ORDER BY p.fecha_pago, p.id_pago
        """,
        params,
    )
    if pagos.empty:
        return _vacio()

    ids_orden = [int(v) for v in pagos["id_orden"].drop_duplicates()]
    detalles = run_query(
        """
        SELECT d.id_orden, o.numero_orden_diaria, o.fecha_orden,
               d.id_producto, pr.nombre AS producto, d.cantidad,
               d.precio_unitario, d.subtotal
        FROM detalles_orden d
        JOIN productos pr ON pr.id_producto = d.id_producto
        JOIN ordenes o ON o.id_orden = d.id_orden
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        WHERE d.id_orden = ANY(CAST(:ids AS integer[]))
          AND ed.nombre <> 'Cancelado'
          AND (:producto = 0 OR d.id_producto = :producto)
        ORDER BY o.numero_orden_diaria, d.id_detalle
        """,
        {"ids": ids_orden, "producto": producto},
    )

    pagos = pagos.copy()
    pagos["monto_num"] = pagos["monto"].apply(_dinero)
    pagos["fecha"] = pagos["fecha_pago"].dt.date
    pagos["hora"] = pagos["fecha_pago"].dt.hour

    total_vendido = float(pagos["monto_num"].sum())
    ordenes = int(pagos["id_orden"].nunique())
    ticket_promedio = total_vendido / ordenes if ordenes else 0.0
    unidades = int(detalles["cantidad"].sum()) if not detalles.empty else 0

    por_dia = (
        pagos.groupby("fecha", as_index=False)["monto_num"]
        .sum()
        .rename(columns={"monto_num": "total"})
    )
    por_hora_base = (
        pagos.groupby("hora", as_index=False)["monto_num"]
        .sum()
        .rename(columns={"monto_num": "total"})
    )
    por_hora = (
        pd.DataFrame({"hora": list(range(24))})
        .merge(por_hora_base, on="hora", how="left")
        .fillna({"total": 0.0})
    )
    por_metodo = (
        pagos.groupby("metodo", as_index=False)["monto_num"]
        .sum()
        .rename(columns={"monto_num": "total"})
        .sort_values("total", ascending=False)
    )

    if detalles.empty:
        top_unidades = pd.DataFrame(columns=["producto", "unidades"])
        top_importe = pd.DataFrame(columns=["producto", "importe"])
    else:
        agrupado = (
            detalles.groupby("producto", as_index=False)
            .agg(unidades=("cantidad", "sum"), importe=("subtotal", "sum"))
        )
        top_unidades = agrupado.nlargest(10, "unidades")[
            ["producto", "unidades"]
        ].reset_index(drop=True)
        top_importe = agrupado.nlargest(10, "importe")[
            ["producto", "importe"]
        ].reset_index(drop=True)
        top_importe["importe"] = top_importe["importe"].apply(_dinero)

    resumen_pagos = (
        pagos.groupby("id_orden", as_index=False)
        .agg(
            metodos_pago=("metodo", lambda s: ", ".join(sorted(set(map(str, s))))),
            pagado_en_rango=("monto_num", "sum"),
        )
    )
    detalle = detalles.merge(resumen_pagos, on="id_orden", how="left")
    detalle = detalle.rename(
        columns={
            "numero_orden_diaria": "Orden",
            "id_orden": "ID orden",
            "fecha_orden": "Fecha orden",
            "producto": "Producto",
            "cantidad": "Cantidad",
            "precio_unitario": "Precio unitario",
            "subtotal": "Subtotal",
            "metodos_pago": "Métodos de pago",
            "pagado_en_rango": "Pagado en rango",
        }
    )
    for columna in COLUMNAS_CSV:
        if columna not in detalle.columns:
            detalle[columna] = None
    detalle = detalle[COLUMNAS_CSV]

    return {
        "pagos": pagos,
        "detalles": detalles,
        "por_dia": por_dia,
        "por_hora": por_hora,
        "por_metodo": por_metodo,
        "top_unidades": top_unidades,
        "top_importe": top_importe,
        "detalle": detalle,
        "metricas": {
            "total_vendido": total_vendido,
            "ordenes": ordenes,
            "ticket_promedio": ticket_promedio,
            "unidades": unidades,
        },
    }


def csv_ventas(datos: dict) -> bytes:
    """CSV de detalle por orden y producto (UTF-8 con BOM para Excel)."""
    detalle = datos["detalle"]
    return detalle.to_csv(index=False).encode("utf-8-sig")


def pdf_ventas(datos: dict, filtros: dict) -> bytes:
    """PDF resumen: filtros, métricas y tablas; no incrusta gráficas."""
    metricas = datos["metricas"]
    subtitulo = (
        f"Del {filtros.get('desde')} al {filtros.get('hasta')} · "
        f"Horas {int(filtros.get('hora_desde', 0)):02d}:00 a "
        f"{int(filtros.get('hora_hasta', 23)):02d}:00 · "
        f"Producto: {filtros.get('producto') or 'Todos'}"
    )
    pdf = crear_pdf("Reporte de ventas", subtitulo)
    parrafo(pdf, "Métricas", negrita=True, tamano=11)
    parrafo(
        pdf,
        f"Total vendido: ${metricas['total_vendido']:.2f} · "
        f"Órdenes pagadas: {metricas['ordenes']} · "
        f"Ticket promedio: ${metricas['ticket_promedio']:.2f} · "
        f"Unidades vendidas: {metricas['unidades']}",
    )

    parrafo(pdf, "Top 10 productos por unidades", negrita=True, tamano=11)
    filas_unidades = [
        [fila["producto"], int(fila["unidades"])]
        for _, fila in datos["top_unidades"].iterrows()
    ]
    tabla(pdf, ["Producto", "Unidades"], filas_unidades, anchos=[140, 30])

    parrafo(pdf, "Top 10 productos por importe", negrita=True, tamano=11)
    filas_importe = [
        [fila["producto"], f"${float(fila['importe']):.2f}"]
        for _, fila in datos["top_importe"].iterrows()
    ]
    tabla(pdf, ["Producto", "Importe"], filas_importe, anchos=[140, 30])

    parrafo(pdf, "Ventas por día", negrita=True, tamano=11)
    filas_dia = [
        [str(fila["fecha"]), f"${float(fila['total']):.2f}"]
        for _, fila in datos["por_dia"].iterrows()
    ]
    tabla(pdf, ["Fecha", "Total"], filas_dia, anchos=[100, 50])

    parrafo(pdf, "Ventas por método de pago", negrita=True, tamano=11)
    filas_metodo = [
        [fila["metodo"], f"${float(fila['total']):.2f}"]
        for _, fila in datos["por_metodo"].iterrows()
    ]
    tabla(pdf, ["Método", "Total"], filas_metodo, anchos=[100, 50])
    return salida_pdf(pdf)
