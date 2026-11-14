"""Pestaña de gestión de recetas dentro del módulo Productos.

No es un módulo independiente del menú: se importa desde ``productos.py``.
Cada alta, cambio o baja de insumo se ejecuta en una transacción del servicio
``services.recetas``; aquí solo se valida la entrada y se muestran mensajes.
"""
from __future__ import annotations

from decimal import Decimal

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from database import run_query
from permissions import requerir_accion
from services.recetas import (
    RecetaError,
    agregar_insumo_receta,
    cambiar_cantidad_receta,
    quitar_insumo_receta,
)


def _receta_del_producto(id_producto: int):
    """Receta vigente con unidad y costo de referencia de cada insumo."""
    return run_query(
        """
        SELECT r.id_insumo, i.nombre AS insumo, r.cantidad_usada AS cantidad,
               u.abreviatura AS unidad,
               i.costo_unitario_referencia AS costo_unitario
        FROM recetas r
        JOIN insumos i ON i.id_insumo = r.id_insumo
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE r.id_producto = :p
        ORDER BY i.nombre
        """,
        {"p": id_producto},
    )


def render_recetas() -> None:
    """Muestra y permite editar la receta del producto seleccionado."""
    requerir_accion("recetas.gestionar")

    productos = run_query(
        """
        SELECT p.id_producto, p.nombre
        FROM productos p
        WHERE p.activo = TRUE
        ORDER BY p.nombre
        """
    )
    if productos.empty:
        st.info("No hay productos activos para configurar recetas.")
        return

    producto = st.selectbox(
        "Producto",
        productos.to_dict("records"),
        format_func=lambda p: p["nombre"],
        key="receta_producto",
    )
    id_producto = int(producto["id_producto"])
    receta = _receta_del_producto(id_producto)

    if receta.empty:
        st.warning(
            "Este producto no tiene receta. Se vende sin descontar inventario "
            "y no se puede calcular su disponibilidad real."
        )
    else:
        vista = receta.copy()
        vista["cantidad"] = vista["cantidad"].map(
            lambda valor: f"{float(valor):.3f}"
        )
        st.dataframe(
            vista[["insumo", "cantidad", "unidad"]],
            use_container_width=True,
            hide_index=True,
        )

        # El esquema sí tiene costo de referencia; si algún costo es nulo se
        # omite del cálculo para no inventar un valor.
        costos = [
            Decimal(str(fila["cantidad"])) * Decimal(str(fila["costo_unitario"]))
            for _, fila in receta.iterrows()
            if fila["costo_unitario"] is not None
        ]
        if costos:
            st.metric(
                "Costo estimado por porción",
                f"${sum(costos):.2f}",
            )

    insumos = run_query(
        """
        SELECT i.id_insumo, i.nombre, u.abreviatura AS unidad
        FROM insumos i
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE i.activo = TRUE
        ORDER BY i.nombre
        """
    )
    if insumos.empty:
        st.info("No hay insumos activos para agregar a la receta.")
        return

    st.markdown("#### Agregar insumo")
    with st.form(f"agregar_insumo_receta_{id_producto}"):
        insumo_nuevo = st.selectbox(
            "Insumo",
            insumos.to_dict("records"),
            format_func=lambda i: f"{i['nombre']} ({i['unidad']})",
            key=f"insumo_nuevo_{id_producto}",
        )
        cantidad_nueva = st.number_input(
            "Cantidad usada por porción",
            min_value=0.001,
            step=0.001,
            format="%.3f",
            key=f"cantidad_nueva_{id_producto}",
        )
        agregar = st.form_submit_button("Agregar a la receta")

    if agregar:
        requerir_accion("recetas.gestionar")
        try:
            agregar_insumo_receta(
                id_producto=id_producto,
                id_insumo=int(insumo_nuevo["id_insumo"]),
                cantidad=cantidad_nueva,
            )
        except (RecetaError, SQLAlchemyError) as exc:
            st.error(f"No se pudo agregar el insumo: {exc}")
        else:
            st.success("Insumo agregado a la receta.")
            st.rerun()

    if receta.empty:
        return

    st.markdown("#### Modificar cantidad")
    fila_cambio = st.selectbox(
        "Insumo de la receta",
        receta.to_dict("records"),
        format_func=lambda r: f"{r['insumo']} ({r['unidad']})",
        key=f"insumo_cambio_{id_producto}",
    )
    with st.form(f"cambiar_cantidad_receta_{id_producto}"):
        cantidad_cambio = st.number_input(
            "Nueva cantidad por porción",
            min_value=0.001,
            step=0.001,
            format="%.3f",
            value=float(fila_cambio["cantidad"]),
            key=f"cantidad_cambio_{id_producto}_{fila_cambio['id_insumo']}",
        )
        cambiar = st.form_submit_button("Actualizar cantidad")

    if cambiar:
        requerir_accion("recetas.gestionar")
        try:
            cambiar_cantidad_receta(
                id_producto=id_producto,
                id_insumo=int(fila_cambio["id_insumo"]),
                cantidad=cantidad_cambio,
            )
        except (RecetaError, SQLAlchemyError) as exc:
            st.error(f"No se pudo cambiar la cantidad: {exc}")
        else:
            st.success("Cantidad actualizada en la receta.")
            st.rerun()

    st.markdown("#### Quitar insumo")
    fila_quitar = st.selectbox(
        "Insumo a quitar",
        receta.to_dict("records"),
        format_func=lambda r: f"{r['insumo']} ({r['unidad']})",
        key=f"insumo_quitar_{id_producto}",
    )
    if st.button(
        "Quitar de la receta",
        key=f"quitar_insumo_{id_producto}_{fila_quitar['id_insumo']}",
    ):
        requerir_accion("recetas.gestionar")
        try:
            quitar_insumo_receta(
                id_producto=id_producto,
                id_insumo=int(fila_quitar["id_insumo"]),
            )
        except (RecetaError, SQLAlchemyError) as exc:
            st.error(f"No se pudo quitar el insumo: {exc}")
        else:
            st.success("Insumo quitado de la receta.")
            st.rerun()
