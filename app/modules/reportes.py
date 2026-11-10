"""Módulo de Reportes: ventas por rango de fechas y productos más vendidos."""
import datetime as dt

import streamlit as st

from database import run_query
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE


def render():
    requerir_rol(ADMIN, GERENTE)
    requerir_accion("reportes.ver")
    st.subheader("📊 Reportes")

    col1, col2 = st.columns(2)
    with col1:
        desde = st.date_input("Desde", value=dt.date.today() - dt.timedelta(days=30))
    with col2:
        hasta = st.date_input("Hasta", value=dt.date.today())

    resumen = run_query(
        """
        SELECT COUNT(DISTINCT o.id_orden) AS ordenes, COALESCE(SUM(p.monto), 0) AS total_cobrado
        FROM ordenes o
        JOIN pagos p ON p.id_orden = o.id_orden
        WHERE DATE(p.fecha_pago) BETWEEN :desde AND :hasta
        """,
        {"desde": desde, "hasta": hasta},
    ).iloc[0]

    c1, c2 = st.columns(2)
    c1.metric("Órdenes cobradas", int(resumen["ordenes"]))
    c2.metric("Total cobrado", f"${float(resumen['total_cobrado']):.2f}")

    st.markdown("#### Ventas por método de pago")
    por_metodo = run_query(
        """
        SELECT mp.nombre AS metodo, COUNT(*) AS pagos, SUM(p.monto) AS total
        FROM pagos p JOIN metodos_pago mp ON mp.id_metodo_pago = p.id_metodo_pago
        WHERE DATE(p.fecha_pago) BETWEEN :desde AND :hasta
        GROUP BY mp.nombre ORDER BY total DESC
        """,
        {"desde": desde, "hasta": hasta},
    )
    st.dataframe(por_metodo, use_container_width=True, hide_index=True)

    st.markdown("#### Productos más vendidos")
    top_productos = run_query(
        """
        SELECT pr.nombre AS producto, SUM(d.cantidad) AS unidades_vendidas, SUM(d.subtotal) AS total_vendido
        FROM detalles_orden d
        JOIN productos pr ON pr.id_producto = d.id_producto
        JOIN ordenes o ON o.id_orden = d.id_orden
        WHERE DATE(o.fecha_orden) BETWEEN :desde AND :hasta
        GROUP BY pr.nombre ORDER BY unidades_vendidas DESC LIMIT 10
        """,
        {"desde": desde, "hasta": hasta},
    )
    st.dataframe(top_productos, use_container_width=True, hide_index=True)

    st.markdown("#### Insumos con stock bajo")
    stock_bajo = run_query(
        """
        SELECT nombre, stock_actual, stock_minimo
        FROM insumos WHERE activo = TRUE AND stock_actual <= stock_minimo
        ORDER BY (stock_actual - stock_minimo)
        """
    )
    if stock_bajo.empty:
        st.success("Ningún insumo por debajo de su stock mínimo.")
    else:
        st.dataframe(stock_bajo, use_container_width=True, hide_index=True)
