"""Módulo de Productos y Categorías (catálogo del menú)."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database import run_query, execute
from modules.recetas import render_recetas
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE
from services.catalogo import (
    CatalogoError,
    actualizar_producto,
    crear_categoria,
    renombrar_categoria,
)


def _texto(valor) -> str:
    """Normaliza NULL/NaN de pandas a cadena vacía."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto in ("None", "nan") else texto


def _catalogo() -> pd.DataFrame:
    """Productos con indicador de receta y costo estimado por porción."""
    return run_query(
        """
        SELECT p.id_producto, p.nombre, p.descripcion,
               p.id_categoria, p.id_area_preparacion,
               c.nombre AS categoria, a.nombre AS area,
               p.precio_actual, p.disponible, p.activo,
               CASE
                   WHEN EXISTS (
                       SELECT 1 FROM recetas r
                       WHERE r.id_producto = p.id_producto
                   ) THEN 'Con receta'
                   ELSE 'Sin receta (no descuenta inventario)'
               END AS receta,
               (
                   SELECT SUM(
                       r.cantidad_usada *
                       COALESCE(i.costo_unitario_referencia, 0)
                   )
                   FROM recetas r
                   JOIN insumos i ON i.id_insumo = r.id_insumo
                   WHERE r.id_producto = p.id_producto
               ) AS costo_estimado
        FROM productos p
        JOIN categorias_productos c ON c.id_categoria = p.id_categoria
        JOIN areas_preparacion a ON a.id_area = p.id_area_preparacion
        ORDER BY p.activo DESC, c.nombre, p.nombre
        """
    )


def _categorias() -> list[dict]:
    df = run_query(
        "SELECT id_categoria, nombre FROM categorias_productos ORDER BY nombre"
    )
    return df.to_dict("records")


def _areas() -> list[dict]:
    df = run_query("SELECT id_area, nombre FROM areas_preparacion ORDER BY nombre")
    return df.to_dict("records")


def _indice_por_id(registros: list[dict], clave: str, valor: int) -> int:
    """Índice del registro que coincide con el id, para precargar un selectbox."""
    for indice, registro in enumerate(registros):
        if int(registro[clave]) == int(valor):
            return indice
    return 0


def _render_catalogo() -> None:
    """Listado y edición de productos existentes."""
    productos = _catalogo()
    st.dataframe(productos, use_container_width=True, hide_index=True)

    if productos.empty:
        st.info("Todavía no hay productos registrados.")
        return

    st.markdown("#### Editar producto")
    registros = productos.to_dict("records")
    seleccionado = st.selectbox(
        "Producto a editar",
        registros,
        format_func=lambda p: (
            f"{p['nombre']} — {'Activo' if p['activo'] else 'Inactivo'}"
        ),
        key="producto_editar",
    )
    id_producto = int(seleccionado["id_producto"])
    categorias = _categorias()
    areas = _areas()
    if not categorias or not areas:
        st.warning(
            "Registra al menos una categoría de producto y un área de "
            "preparación antes de editar."
        )
        return

    with st.form(f"editar_producto_{id_producto}"):
        nombre = st.text_input("Nombre", value=str(seleccionado["nombre"]))
        descripcion = st.text_input(
            "Descripción",
            value=_texto(seleccionado["descripcion"]),
        )
        categoria = st.selectbox(
            "Categoría",
            categorias,
            index=_indice_por_id(
                categorias, "id_categoria", int(seleccionado["id_categoria"])
            ),
            format_func=lambda c: c["nombre"],
            key=f"categoria_editar_{id_producto}",
        )
        area = st.selectbox(
            "Área de preparación",
            areas,
            index=_indice_por_id(
                areas, "id_area", int(seleccionado["id_area_preparacion"])
            ),
            format_func=lambda a: a["nombre"],
            key=f"area_editar_{id_producto}",
        )
        precio = st.number_input(
            "Precio actual",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            value=float(seleccionado["precio_actual"]),
            key=f"precio_editar_{id_producto}",
        )
        disponible = st.checkbox(
            "Disponible para venta",
            value=bool(seleccionado["disponible"]),
            key=f"disponible_editar_{id_producto}",
        )
        guardar = st.form_submit_button("Guardar cambios")

    if guardar:
        requerir_accion("productos.gestionar")
        try:
            actualizar_producto(
                id_producto=id_producto,
                nombre=nombre,
                descripcion=descripcion,
                id_categoria=int(categoria["id_categoria"]),
                id_area_preparacion=int(area["id_area"]),
                precio=precio,
                disponible=disponible,
            )
        except (CatalogoError, SQLAlchemyError) as exc:
            st.error(f"No se pudo actualizar el producto: {exc}")
        else:
            st.success("Producto actualizado.")
            st.rerun()

    if bool(seleccionado["activo"]):
        if st.button(
            "Dar de baja (activo = FALSE)",
            key=f"baja_producto_{id_producto}",
        ):
            requerir_accion("productos.gestionar")
            try:
                execute(
                    "UPDATE productos SET activo = FALSE "
                    "WHERE id_producto = :id",
                    {"id": id_producto},
                )
            except SQLAlchemyError as exc:
                st.error(f"No se pudo dar de baja el producto: {exc}")
            else:
                st.success("Producto dado de baja del catálogo.")
                st.rerun()
    else:
        if st.button(
            "Reactivar producto",
            key=f"reactivar_producto_{id_producto}",
        ):
            requerir_accion("productos.gestionar")
            try:
                execute(
                    "UPDATE productos SET activo = TRUE "
                    "WHERE id_producto = :id",
                    {"id": id_producto},
                )
            except SQLAlchemyError as exc:
                st.error(f"No se pudo reactivar el producto: {exc}")
            else:
                st.success("Producto reactivado.")
                st.rerun()


def _render_nuevo_producto() -> None:
    """Alta de productos del menú."""
    categorias = _categorias()
    areas = _areas()
    if not categorias or not areas:
        st.warning(
            "Registra al menos una categoría de producto y un área de "
            "preparación antes de continuar."
        )
        return

    with st.form("nuevo_producto"):
        nombre = st.text_input("Nombre del producto")
        descripcion = st.text_input("Descripción (opcional)")
        categoria = st.selectbox(
            "Categoría", categorias, format_func=lambda c: c["nombre"]
        )
        area = st.selectbox(
            "Área de preparación", areas, format_func=lambda a: a["nombre"]
        )
        precio = st.number_input(
            "Precio", min_value=0.0, step=1.0, format="%.2f"
        )
        disponible = st.checkbox("Disponible desde ahora", value=True)
        enviado = st.form_submit_button("Guardar producto")

    if enviado:
        requerir_accion("productos.gestionar")
        if not nombre.strip():
            st.error("El nombre es obligatorio.")
            return
        try:
            execute(
                """
                INSERT INTO productos
                    (nombre, descripcion, id_categoria,
                     id_area_preparacion, precio_actual, disponible)
                VALUES (:n, :d, :c, :a, :p, :disponible)
                """,
                {
                    "n": nombre.strip(),
                    "d": descripcion.strip() or None,
                    "c": int(categoria["id_categoria"]),
                    "a": int(area["id_area"]),
                    "p": precio,
                    "disponible": disponible,
                },
            )
        except IntegrityError:
            st.error("Ya existe un producto con ese nombre.")
        except SQLAlchemyError as exc:
            st.error(f"No se pudo crear el producto: {exc}")
        else:
            st.success(f"Producto '{nombre.strip()}' creado.")
            st.rerun()


def _render_categorias() -> None:
    """Alta y renombrado de categorías de producto."""
    categorias = run_query(
        """
        SELECT c.id_categoria, c.nombre, c.descripcion,
               COUNT(p.id_producto) AS productos
        FROM categorias_productos c
        LEFT JOIN productos p ON p.id_categoria = c.id_categoria
        GROUP BY c.id_categoria, c.nombre, c.descripcion
        ORDER BY c.nombre
        """
    )
    st.dataframe(categorias, use_container_width=True, hide_index=True)

    st.markdown("#### Nueva categoría")
    with st.form("nueva_categoria_producto"):
        nombre = st.text_input("Nombre de la categoría")
        descripcion = st.text_input("Descripción (opcional)")
        crear = st.form_submit_button("Crear categoría")
    if crear:
        requerir_accion("productos.gestionar")
        try:
            crear_categoria(nombre, descripcion)
        except (CatalogoError, SQLAlchemyError) as exc:
            st.error(f"No se pudo crear la categoría: {exc}")
        else:
            st.success("Categoría creada.")
            st.rerun()

    if categorias.empty:
        return

    st.markdown("#### Renombrar categoría")
    registros = categorias.to_dict("records")
    seleccionada = st.selectbox(
        "Categoría a renombrar",
        registros,
        format_func=lambda c: c["nombre"],
        key="categoria_renombrar",
    )
    with st.form(f"renombrar_categoria_{seleccionada['id_categoria']}"):
        nuevo_nombre = st.text_input(
            "Nuevo nombre", value=str(seleccionada["nombre"])
        )
        renombrar = st.form_submit_button("Renombrar")
    if renombrar:
        requerir_accion("productos.gestionar")
        try:
            renombrar_categoria(
                int(seleccionada["id_categoria"]), nuevo_nombre
            )
        except (CatalogoError, SQLAlchemyError) as exc:
            st.error(f"No se pudo renombrar la categoría: {exc}")
        else:
            st.success("Categoría renombrada.")
            st.rerun()


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("🍔 Productos")

    tab_catalogo, tab_nuevo, tab_recetas, tab_categorias = st.tabs(
        ["Catálogo", "Nuevo producto", "Recetas", "Categorías"]
    )

    with tab_catalogo:
        _render_catalogo()

    with tab_nuevo:
        _render_nuevo_producto()

    with tab_recetas:
        render_recetas()

    with tab_categorias:
        _render_categorias()
