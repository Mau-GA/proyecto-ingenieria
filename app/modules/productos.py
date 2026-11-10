"""Módulo de Productos y Categorías (catálogo del menú)."""
import streamlit as st

from database import run_query, execute
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("🍔 Productos")

    tab_lista, tab_nuevo = st.tabs(["Catálogo", "Nuevo producto"])

    with tab_lista:
        productos = run_query(
            """
            SELECT p.id_producto, p.nombre, c.nombre AS categoria, a.nombre AS area,
                   p.precio_actual, p.disponible, p.activo
            FROM productos p
            JOIN categorias_productos c ON c.id_categoria = p.id_categoria
            JOIN areas_preparacion a ON a.id_area = p.id_area_preparacion
            ORDER BY c.nombre, p.nombre
            """
        )
        st.dataframe(productos, use_container_width=True, hide_index=True)

    with tab_nuevo:
        categorias = run_query("SELECT id_categoria, nombre FROM categorias_productos ORDER BY nombre")
        areas = run_query("SELECT id_area, nombre FROM areas_preparacion ORDER BY nombre")

        if categorias.empty or areas.empty:
            st.warning("Registra al menos una categoría de producto y un área de preparación antes de continuar.")
            return

        with st.form("nuevo_producto"):
            nombre = st.text_input("Nombre del producto")
            categoria = st.selectbox("Categoría", categorias.to_dict("records"), format_func=lambda c: c["nombre"])
            area = st.selectbox("Área de preparación", areas.to_dict("records"), format_func=lambda a: a["nombre"])
            precio = st.number_input("Precio", min_value=0.0, step=1.0, format="%.2f")
            disponible = st.checkbox("Disponible desde ahora", value=True)
            enviado = st.form_submit_button("Guardar producto")

        if enviado:
            requerir_accion("productos.gestionar")
            if not nombre.strip():
                st.error("El nombre es obligatorio.")
            else:
                execute(
                    """
                    INSERT INTO productos (nombre, id_categoria, id_area_preparacion, precio_actual, disponible)
                    VALUES (:n, :c, :a, :p, :d)
                    """,
                    {"n": nombre.strip(), "c": int(categoria["id_categoria"]), "a": int(area["id_area"]),
                     "p": precio, "d": disponible},
                )
                st.success(f"Producto '{nombre}' creado.")
                st.rerun()
