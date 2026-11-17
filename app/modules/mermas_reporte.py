"""Captura y consulta de mermas reportadas por el personal.

Cualquier rol operativo (Mesero, Caja, Cocina, Bar) puede agregar una merma
manual de un producto del menú o de un insumo base, indicando cantidad exacta
y motivo. El reporte queda pendiente y solo el Gerente/Administrador lo puede
subir (descontar stock) o eliminar desde Inventario > Gestión de Mermas.

- Producto preparado: al subirlo se descuentan los insumos de su receta.
- Insumo base: al subirlo se descuenta directamente ese insumo del inventario.

Este módulo es estrictamente de captura y consulta: no borra ni aplica
descuentos.
"""
from decimal import Decimal

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from database import run_query
from permissions import (
    requerir_rol,
    requerir_accion,
    ADMIN,
    BAR,
    CAJA,
    COCINA,
    GERENTE,
    MESERO,
)
from services.mermas import (
    PREFIJO_INSUMO,
    PREFIJO_MOTIVO,
    registrar_merma_insumo,
    registrar_merma_producto,
)
from services.ventas import VentaError


def _elementos_para_mermar() -> list[dict]:
    """Todos los productos y todos los insumos activos en una sola lista."""
    productos = run_query(
        """
        SELECT p.id_producto, p.nombre
        FROM productos p
        WHERE p.activo = TRUE
        ORDER BY p.nombre
        """
    ).to_dict("records")
    insumos = run_query(
        """
        SELECT i.id_insumo, i.nombre, um.abreviatura AS unidad
        FROM insumos i
        JOIN unidades_medida um ON um.id_unidad = i.id_unidad_medida
        WHERE i.activo = TRUE
        ORDER BY i.nombre
        """
    ).to_dict("records")

    elementos: list[dict] = []
    for producto in productos:
        elementos.append(
            {
                "tipo": "producto",
                "id": int(producto["id_producto"]),
                "nombre": str(producto["nombre"]),
                "unidad": "unidades",
                "etiqueta": f"[Menú] {producto['nombre']}",
            }
        )
    for insumo in insumos:
        elementos.append(
            {
                "tipo": "insumo",
                "id": int(insumo["id_insumo"]),
                "nombre": str(insumo["nombre"]),
                "unidad": str(insumo["unidad"]),
                "etiqueta": f"[Insumo] {insumo['nombre']} ({insumo['unidad']})",
            }
        )
    return elementos


def _formulario_merma(auth: dict) -> None:
    """Formulario manual para reportar producto preparado o insumo base."""
    elementos = _elementos_para_mermar()
    if not elementos:
        st.info("No hay productos con receta ni insumos activos para reportar.")
        return

    etiquetas = [elemento["etiqueta"] for elemento in elementos]
    por_etiqueta = {elemento["etiqueta"]: elemento for elemento in elementos}

    with st.expander("➕ Agregar merma", expanded=False):
        st.caption(
            "Registra la merma con cantidad exacta y motivo. Quedará pendiente "
            "de revisión por el Gerente; aquí no se elimina ni se descuenta inventario."
        )
        with st.form("form_merma_manual", clear_on_submit=True):
            etiqueta = st.selectbox(
                "Elemento a mermar",
                etiquetas,
                help=(
                    "Los productos del menú descuentan los insumos de su receta. "
                    "Los insumos base descuentan directamente del inventario."
                ),
            )
            cantidad = st.number_input(
                "Cantidad exacta",
                min_value=0.001,
                value=1.0,
                step=0.1,
                format="%.3f",
            )
            st.caption(
                "Productos del menú: usa unidades enteras. "
                "Insumos: usa la unidad indicada en el selector."
            )
            motivo = st.text_input(
                "Motivo detallado",
                max_chars=200,
                placeholder="Ej. caducidad, caída, mal estado…",
            )
            enviado = st.form_submit_button(
                "Agregar merma",
                type="primary",
                use_container_width=True,
            )

        if not enviado:
            return

        elemento = por_etiqueta[etiqueta]
        requerir_accion("mermas.reportar")
        try:
            if elemento["tipo"] == "producto":
                if abs(float(cantidad) - round(float(cantidad))) > 1e-9:
                    st.error(
                        "Para productos del menú, la cantidad debe ser un número "
                        "entero de unidades."
                    )
                    return
                ids = registrar_merma_producto(
                    id_producto=int(elemento["id"]),
                    cantidad=int(round(float(cantidad))),
                    id_usuario=auth["id_usuario"],
                    id_empleado=auth["id_empleado"],
                    motivo=motivo,
                )
                tipo_legible = "producto preparado"
            else:
                ids = registrar_merma_insumo(
                    id_insumo=int(elemento["id"]),
                    cantidad=Decimal(str(cantidad)),
                    id_usuario=auth["id_usuario"],
                    id_empleado=auth["id_empleado"],
                    motivo=motivo,
                )
                tipo_legible = "insumo base"
        except (VentaError, SQLAlchemyError) as exc:
            st.error(f"No se pudo registrar la merma: {exc}")
            return

        st.session_state["merma_reporte_ok"] = (
            f"Merma de {tipo_legible} «{elemento['nombre']}» registrada: "
            f"{len(ids)} insumo(s) pendientes de revisión por el Gerente."
        )
        st.rerun()


def render() -> None:
    requerir_rol(ADMIN, GERENTE, MESERO, CAJA, COCINA, BAR)

    auth = usuario_actual()
    roles = set(auth.get("roles") or [])
    ver_todas = bool(roles & {ADMIN, GERENTE})

    st.subheader("📉 Mermas")
    mensaje = st.session_state.pop("merma_reporte_ok", None)
    if mensaje:
        st.success(mensaje)

    _formulario_merma(auth)

    st.caption(
        "Los reportes capturados desde Comandas, Órdenes o este formulario "
        "llegan a la bandeja del Gerente en Inventario > Gestión de Mermas."
    )

    reportes = run_query(
        """
        SELECT CASE
                   WHEN mi.motivo ILIKE CONCAT(:prefijo_producto, '%')
                       THEN 'Producto preparado'
                   WHEN mi.motivo ILIKE CONCAT(:prefijo_insumo, '%')
                       THEN 'Insumo base'
                   ELSE 'Insumo base'
               END AS tipo,
               m.descripcion AS reporte,
               mi.fecha_movimiento AS fecha,
               STRING_AGG(
                   CONCAT(i.nombre, ': ', TO_CHAR(mi.cantidad, 'FM999999999990.000'), ' ', um.abreviatura),
                   ' · ' ORDER BY i.nombre
               ) AS insumos,
               CASE
                   WHEN MIN(m.costo_estimado_perdida) IS NULL THEN 'Pendiente'
                   ELSE 'Subida'
               END AS estado,
               u.nombre_usuario AS usuario
        FROM mermas m
        JOIN movimientos_inventario mi ON mi.id_movimiento = m.id_movimiento
        JOIN insumos i ON i.id_insumo = mi.id_insumo
        JOIN unidades_medida um ON um.id_unidad = i.id_unidad_medida
        JOIN usuarios u ON u.id_usuario = m.id_usuario_registro
        WHERE (:ver_todas = 1 OR m.id_usuario_registro = :usuario)
        GROUP BY mi.motivo, m.descripcion, mi.fecha_movimiento, u.nombre_usuario
        ORDER BY mi.fecha_movimiento DESC
        LIMIT 50
        """,
        {
            "ver_todas": 1 if ver_todas else 0,
            "usuario": auth["id_usuario"],
            "prefijo_producto": PREFIJO_MOTIVO,
            "prefijo_insumo": PREFIJO_INSUMO,
        },
    )

    if reportes.empty:
        st.info("Todavía no hay reportes de mermas.")
        return

    st.dataframe(reportes, use_container_width=True, hide_index=True)
