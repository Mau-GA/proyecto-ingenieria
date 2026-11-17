"""Módulo de Reportes: ventas, compras semanales y bitácora forense."""
import datetime as dt
import json
from decimal import Decimal

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
from services.compras_semanales import (
    csv_compras,
    pdf_compras,
    sugerencia_compras,
)
from services.ventas_reportes import csv_ventas, datos_ventas, pdf_ventas


def _render_ventas() -> None:
    st.caption(
        "Criterio: solo órdenes Pagada con pagos dentro del rango. El dinero "
        "sale de pagos (fecha_pago) y los productos de sus partidas no "
        "canceladas."
    )
    col1, col2 = st.columns(2)
    with col1:
        desde = st.date_input(
            "Desde",
            value=dt.date.today() - dt.timedelta(days=30),
            key="ventas_desde",
        )
    with col2:
        hasta = st.date_input(
            "Hasta", value=dt.date.today(), key="ventas_hasta"
        )
    hora_desde, hora_hasta = st.slider(
        "Rango de horas",
        min_value=0,
        max_value=23,
        value=(0, 23),
        key="ventas_horas",
    )
    productos = run_query(
        "SELECT id_producto, nombre FROM productos "
        "WHERE activo = TRUE ORDER BY nombre"
    )
    opciones = [0]
    if not productos.empty:
        opciones += [int(v) for v in productos["id_producto"]]
    nombres = {0: "Todos los productos"}
    nombres.update(
        {
            int(fila["id_producto"]): str(fila["nombre"])
            for _, fila in productos.iterrows()
        }
    )
    id_producto = st.selectbox(
        "Producto (opcional)",
        opciones,
        format_func=lambda i: nombres.get(int(i), str(i)),
        key="ventas_producto",
    )

    datos = datos_ventas(
        desde,
        hasta,
        hora_desde=hora_desde,
        hora_hasta=hora_hasta,
        id_producto=id_producto or None,
    )
    metricas = datos["metricas"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total vendido", f"${metricas['total_vendido']:.2f}")
    c2.metric("Órdenes", metricas["ordenes"])
    c3.metric("Ticket promedio", f"${metricas['ticket_promedio']:.2f}")
    c4.metric("Unidades vendidas", metricas["unidades"])

    if metricas["ordenes"] == 0:
        st.info("No hay ventas pagadas en el rango y filtros seleccionados.")

    st.markdown("#### Ventas por día")
    por_dia = datos["por_dia"].set_index("fecha")["total"] if not datos["por_dia"].empty else None
    if por_dia is not None:
        st.line_chart(por_dia)
    else:
        st.caption("Sin datos para graficar.")

    st.markdown("#### Ventas por hora del día")
    st.bar_chart(datos["por_hora"].set_index("hora")["total"])

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### Top 10 por unidades")
        if datos["top_unidades"].empty:
            st.caption("Sin productos vendidos.")
        else:
            st.bar_chart(
                datos["top_unidades"].set_index("producto")["unidades"]
            )
            st.dataframe(
                datos["top_unidades"], hide_index=True, use_container_width=True
            )
    with col_b:
        st.markdown("#### Top 10 por importe")
        if datos["top_importe"].empty:
            st.caption("Sin productos vendidos.")
        else:
            st.bar_chart(
                datos["top_importe"].set_index("producto")["importe"]
                .apply(lambda v: float(v))
            )
            st.dataframe(
                datos["top_importe"], hide_index=True, use_container_width=True
            )

    st.markdown("#### Ventas por método de pago")
    if datos["por_metodo"].empty:
        st.caption("Sin pagos en el rango.")
    else:
        st.bar_chart(datos["por_metodo"].set_index("metodo")["total"])
        st.dataframe(
            datos["por_metodo"], hide_index=True, use_container_width=True
        )

    st.markdown("#### Exportación")
    filtros = {
        "desde": desde,
        "hasta": hasta,
        "hora_desde": hora_desde,
        "hora_hasta": hora_hasta,
        "producto": nombres[int(id_producto)],
    }
    col_csv, col_pdf = st.columns(2)
    with col_csv:
        st.download_button(
            "Descargar detalle CSV",
            data=csv_ventas(datos),
            file_name="reporte_ventas_detalle.csv",
            mime="text/csv",
            key="descargar_ventas_csv",
        )
    with col_pdf:
        st.download_button(
            "Descargar resumen PDF",
            data=pdf_ventas(datos, filtros),
            file_name="reporte_ventas.pdf",
            mime="application/pdf",
            key="descargar_ventas_pdf",
        )

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


def _render_compras_semanales() -> None:
    """Reporte de solo lectura; reutiliza el permiso existente reportes.ver."""
    st.caption(
        "Solo lectura: este reporte no crea compras. Reutiliza el permiso "
        "reportes.ver porque Reportes ya está restringido a Administrador y "
        "Gerente."
    )
    semanas = 4
    sugerencia = sugerencia_compras(semanas=semanas)
    if sugerencia.empty:
        st.success("No hay insumos con necesidad de compra.")
        return

    columnas = [
        "insumo",
        "unidad",
        "consumo_semana_1",
        "consumo_semana_2",
        "consumo_semana_3",
        "consumo_semana_4",
        "stock_seguridad",
        "stock_utilizable",
        "necesidad",
        "costo_unitario",
        "origen_costo",
        "costo_estimado",
    ]
    etiquetas = {
        "insumo": "Insumo",
        "unidad": "Unidad",
        "consumo_semana_1": "Semana 1",
        "consumo_semana_2": "Semana 2",
        "consumo_semana_3": "Semana 3",
        "consumo_semana_4": "Semana 4",
        "stock_seguridad": "Stock seguridad",
        "stock_utilizable": "Stock utilizable",
        "necesidad": "Necesidad",
        "costo_unitario": "Costo unitario",
        "origen_costo": "Origen costo",
        "costo_estimado": "Costo estimado",
    }
    total_general = Decimal("0")
    for proveedor, grupo in sugerencia.groupby("proveedor", sort=True):
        subtotal = sum(
            (Decimal(str(v)) for v in grupo["costo_estimado"]), Decimal("0")
        )
        total_general += subtotal
        st.markdown(f"#### {proveedor}")
        st.dataframe(
            grupo[columnas].rename(columns=etiquetas),
            hide_index=True,
            use_container_width=True,
        )
        st.markdown(f"**Subtotal: ${subtotal:.2f}**")

    st.metric("Total general", f"${total_general:.2f}")
    col_csv, col_pdf = st.columns(2)
    with col_csv:
        st.download_button(
            "Descargar compras CSV",
            data=csv_compras(sugerencia),
            file_name="compras_semanales.csv",
            mime="text/csv",
            key="descargar_compras_csv",
        )
    with col_pdf:
        st.download_button(
            "Descargar compras PDF",
            data=pdf_compras(sugerencia, semanas=semanas),
            file_name="compras_semanales.pdf",
            mime="application/pdf",
            key="descargar_compras_pdf",
        )


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
        tab_ventas, tab_compras, tab_bitacora = st.tabs(
            ["💰 Ventas", "🛒 Compras semanales", "🔐 Bitácora"]
        )
        with tab_ventas:
            _render_ventas()
        with tab_compras:
            _render_compras_semanales()
        with tab_bitacora:
            _render_bitacora()
    else:
        tab_ventas, tab_compras = st.tabs(["💰 Ventas", "🛒 Compras semanales"])
        with tab_ventas:
            _render_ventas()
        with tab_compras:
            _render_compras_semanales()
