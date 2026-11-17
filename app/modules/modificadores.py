"""Pestaña de administración de modificadores de productos.

Vive dentro del módulo Productos, no en el menú lateral. Permite crear,
editar y activar/desactivar (baja lógica) las opciones de personalización
de cada producto. La baja nunca borra filas para no romper el histórico.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from database import run_query
from permissions import requerir_accion
from services.modificadores import (
    TIPOS,
    ModificadorError,
    actualizar_modificador,
    cambiar_activo_modificador,
    crear_modificador,
    modificadores_de_producto,
)


def _texto(valor) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto in ("None", "nan") else texto


def _indice_por_id(registros: list[dict], clave: str, valor) -> int:
    for indice, registro in enumerate(registros):
        if int(registro[clave]) == int(valor):
            return indice
    return 0


def _productos() -> list[dict]:
    return run_query(
        """
        SELECT id_producto, nombre
        FROM productos
        WHERE activo = TRUE
        ORDER BY nombre
        """
    ).to_dict("records")


def _insumos() -> list[dict]:
    return run_query(
        """
        SELECT i.id_insumo, i.nombre, i.activo,
               u.abreviatura AS unidad
        FROM insumos i
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        ORDER BY i.nombre
        """
    ).to_dict("records")


def _cantidad(valor, decimales: int = 3) -> str:
    if valor is None:
        return "—"
    return f"{float(valor):.{decimales}f}"


def _efecto(modificador: dict) -> str:
    """Texto claro del efecto en inventario y precio."""
    tipo = modificador["tipo"]
    nombre_insumo = modificador["insumo"]
    unidad = modificador["unidad"]
    if tipo == "Quitar":
        return f"Quitar: omitir {nombre_insumo} (según receta)"
    if tipo == "Extra":
        return (
            f"Extra: +{_cantidad(modificador['cantidad'])} {unidad} "
            f"{nombre_insumo}"
        )
    return (
        f"Sustituir: omitir {nombre_insumo} → "
        f"+{_cantidad(modificador['cantidad'])} "
        f"{modificador.get('unidad_nueva') or ''} "
        f"{modificador.get('insumo_nuevo') or ''}"
    ).replace("  ", " ")


def _tabla(modificadores: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Nombre": fila["nombre"],
                "Tipo": fila["tipo"],
                "Efecto en inventario": _efecto(fila),
                "Precio extra": f"${float(fila['precio_extra']):.2f}",
                "Activo": "Sí" if fila["activo"] else "No",
            }
            for fila in modificadores
        ]
    )


def _render_alta(producto: dict, insumos: list[dict]) -> None:
    st.markdown("#### Nuevo modificador")
    tipo = st.selectbox(
        "Tipo",
        TIPOS,
        key=f"mod_tipo_{producto['id_producto']}",
    )
    with st.form(f"nuevo_modificador_{producto['id_producto']}_{tipo}"):
        nombre = st.text_input("Nombre visible para el mesero")
        insumo = st.selectbox(
            "Insumo original / a agregar",
            insumos,
            format_func=lambda i: (
                f"{i['nombre']} ({i['unidad']})"
                + ("" if i["activo"] else " — inactivo")
            ),
            key=f"mod_insumo_{producto['id_producto']}_{tipo}",
        )
        insumo_nuevo = None
        if tipo == "Sustituir":
            insumo_nuevo = st.selectbox(
                "Insumo nuevo",
                insumos,
                format_func=lambda i: (
                    f"{i['nombre']} ({i['unidad']})"
                    + ("" if i["activo"] else " — inactivo")
                ),
                key=f"mod_nuevo_{producto['id_producto']}",
            )
        cantidad = None
        if tipo in ("Extra", "Sustituir"):
            cantidad = st.number_input(
                "Cantidad que se suma",
                min_value=0.001,
                step=0.001,
                format="%.3f",
                key=f"mod_cantidad_{producto['id_producto']}_{tipo}",
            )
        precio_extra = st.number_input(
            "Precio extra",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            key=f"mod_precio_{producto['id_producto']}_{tipo}",
        )
        crear = st.form_submit_button("Crear modificador")

    if not crear:
        return
    requerir_accion("modificadores.gestionar")
    try:
        crear_modificador(
            id_producto=int(producto["id_producto"]),
            tipo=tipo,
            nombre=nombre,
            id_insumo=int(insumo["id_insumo"]),
            id_insumo_nuevo=(
                int(insumo_nuevo["id_insumo"]) if insumo_nuevo else None
            ),
            cantidad=cantidad,
            precio_extra=precio_extra,
        )
    except (ModificadorError, SQLAlchemyError) as exc:
        st.error(f"No se pudo crear el modificador: {exc}")
    else:
        st.success("Modificador creado.")
        st.rerun()


def _render_edicion(producto: dict, insumos: list[dict]) -> None:
    modificadores = modificadores_de_producto(
        int(producto["id_producto"]), solo_activos=False
    )
    if not modificadores:
        st.info("Este producto todavía no tiene modificadores.")
        return

    seleccionado = st.selectbox(
        "Modificador a editar",
        modificadores,
        format_func=lambda m: (
            f"{m['nombre']} — {m['tipo']} — "
            f"{'Activo' if m['activo'] else 'Inactivo'}"
        ),
        key=f"mod_editar_{producto['id_producto']}",
    )
    tipo = seleccionado["tipo"]
    st.caption(f"Tipo: **{tipo}**. El tipo no se puede cambiar; crea otro si lo necesitas.")

    with st.form(f"editar_modificador_{seleccionado['id_modificador']}"):
        nombre = st.text_input(
            "Nombre visible", value=_texto(seleccionado["nombre"])
        )
        insumo = st.selectbox(
            "Insumo original / a agregar",
            insumos,
            index=_indice_por_id(
                insumos, "id_insumo", seleccionado["id_insumo"]
            ),
            format_func=lambda i: (
                f"{i['nombre']} ({i['unidad']})"
                + ("" if i["activo"] else " — inactivo")
            ),
            key=f"mod_edit_insumo_{seleccionado['id_modificador']}",
        )
        insumo_nuevo = None
        if tipo == "Sustituir":
            insumo_nuevo = st.selectbox(
                "Insumo nuevo",
                insumos,
                index=_indice_por_id(
                    insumos, "id_insumo", seleccionado["id_insumo_nuevo"]
                ),
                format_func=lambda i: (
                    f"{i['nombre']} ({i['unidad']})"
                    + ("" if i["activo"] else " — inactivo")
                ),
                key=f"mod_edit_nuevo_{seleccionado['id_modificador']}",
            )
        cantidad = None
        if tipo in ("Extra", "Sustituir"):
            cantidad = st.number_input(
                "Cantidad que se suma",
                min_value=0.001,
                step=0.001,
                format="%.3f",
                value=float(seleccionado["cantidad"] or 0.001),
                key=f"mod_edit_cantidad_{seleccionado['id_modificador']}",
            )
        precio_extra = st.number_input(
            "Precio extra",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            value=float(seleccionado["precio_extra"]),
            key=f"mod_edit_precio_{seleccionado['id_modificador']}",
        )
        guardar = st.form_submit_button("Guardar cambios")

    if guardar:
        requerir_accion("modificadores.gestionar")
        try:
            actualizar_modificador(
                int(seleccionado["id_modificador"]),
                nombre=nombre,
                id_insumo=int(insumo["id_insumo"]),
                id_insumo_nuevo=(
                    int(insumo_nuevo["id_insumo"]) if insumo_nuevo else None
                ),
                cantidad=cantidad,
                precio_extra=precio_extra,
            )
        except (ModificadorError, SQLAlchemyError) as exc:
            st.error(f"No se pudo actualizar el modificador: {exc}")
        else:
            st.success("Modificador actualizado.")
            st.rerun()

    etiqueta = "Desactivar" if seleccionado["activo"] else "Activar"
    if st.button(
        etiqueta,
        key=f"mod_activo_{seleccionado['id_modificador']}",
    ):
        requerir_accion("modificadores.gestionar")
        try:
            cambiar_activo_modificador(
                int(seleccionado["id_modificador"]),
                not bool(seleccionado["activo"]),
            )
        except (ModificadorError, SQLAlchemyError) as exc:
            st.error(f"No se pudo cambiar el estado: {exc}")
        else:
            st.success(f"Modificador {'activado' if not seleccionado['activo'] else 'desactivado'}.")
            st.rerun()


def render_modificadores() -> None:
    """Administración de modificadores por producto."""
    requerir_accion("modificadores.gestionar")
    productos = _productos()
    insumos = _insumos()
    if not productos:
        st.info("No hay productos activos para configurar modificadores.")
        return
    if not insumos:
        st.info("No hay insumos para configurar modificadores.")
        return

    producto = st.selectbox(
        "Producto",
        productos,
        format_func=lambda p: p["nombre"],
        key="modificador_producto",
    )
    modificadores = modificadores_de_producto(
        int(producto["id_producto"]), solo_activos=False
    )
    if modificadores:
        st.dataframe(
            _tabla(modificadores),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Este producto todavía no tiene modificadores.")

    _render_alta(producto, insumos)
    _render_edicion(producto, insumos)
