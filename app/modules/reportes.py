"""Módulo de Reportes: ventas, productos más vendidos y bitácora forense."""
import datetime as dt
import json

import streamlit as st

from database import run_query
from permissions import (
    puede_accion,
    requerir_rol,
    requerir_accion,
    ADMIN,
    GERENTE,
)
from services.bitacora import TIPOS_EVENTO, verificar_bitacora


def _render_ventas() -> None:
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


def _render_bitacora() -> None:
    requerir_accion("bitacora.ver")
    st.caption(
        "Registro inalterable de eventos. La cadena se verifica contra los "
        "hashes guardados; cualquier edición directa en la tabla se detecta."
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        desde = st.date_input(
            "Desde",
            value=dt.date.today() - dt.timedelta(days=30),
            key="bitacora_desde",
        )
    with c2:
        hasta = st.date_input(
            "Hasta",
            value=dt.date.today(),
            key="bitacora_hasta",
        )
    with c3:
        tipo = st.selectbox(
            "Tipo de evento",
            ["Todos"] + sorted(TIPOS_EVENTO),
            key="bitacora_tipo",
        )

    usuarios = run_query(
        """
        SELECT DISTINCT nombre_usuario
        FROM bitacora
        WHERE nombre_usuario IS NOT NULL
        ORDER BY nombre_usuario
        """
    )
    opciones_usuario = ["Todos"]
    if not usuarios.empty:
        opciones_usuario += usuarios["nombre_usuario"].tolist()
    usuario = st.selectbox(
        "Usuario",
        opciones_usuario,
        key="bitacora_usuario",
    )

    registros = run_query(
        """
        SELECT id_bitacora, fecha, nombre_usuario, tipo_evento, entidad,
               id_entidad, datos_anteriores, datos_nuevos, hash_anterior, hash
        FROM bitacora
        WHERE DATE(fecha) BETWEEN :desde AND :hasta
          AND (:tipo = 'Todos' OR tipo_evento = :tipo)
          AND (:usuario = 'Todos' OR nombre_usuario = :usuario)
        ORDER BY id_bitacora DESC
        LIMIT 500
        """,
        {
            "desde": desde,
            "hasta": hasta,
            "tipo": tipo,
            "usuario": usuario,
        },
    )

    if st.button("🔎 Verificar integridad de la cadena", key="verificar_bitacora"):
        id_alterado = verificar_bitacora()
        if id_alterado is None:
            st.success("Cadena de hashes intacta.")
        else:
            st.error(
                "La cadena de hashes está alterada. Primer registro inconsistente: "
                f"id_bitacora={id_alterado}."
            )

    if registros.empty:
        st.info("No hay eventos en el rango y filtros seleccionados.")
        return

    vista = registros.copy()
    for columna in ("datos_anteriores", "datos_nuevos"):
        vista[columna] = vista[columna].apply(
            lambda valor: "" if valor is None else json.dumps(
                valor, ensure_ascii=False, sort_keys=True
            )
        )
    st.caption(f"Mostrando {len(vista)} evento(s), del más reciente al más antiguo.")
    st.dataframe(vista, use_container_width=True, hide_index=True)


def render():
    requerir_rol(ADMIN, GERENTE)
    requerir_accion("reportes.ver")
    st.subheader("📊 Reportes")

    if puede_accion("bitacora.ver"):
        tab_ventas, tab_bitacora = st.tabs(["💰 Ventas", "🔐 Bitácora"])
        with tab_ventas:
            _render_ventas()
        with tab_bitacora:
            _render_bitacora()
    else:
        _render_ventas()
