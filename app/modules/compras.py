"""Módulo de Compras: entrada de producto por secciones y actualización de stock."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from database import run_query, transaccion
from permissions import requerir_accion, requerir_rol, ADMIN, GERENTE

K_CARRITO = "carrito_compra"
K_PROVEEDOR = "compras_proveedor"
K_DESCUENTO = "compras_descuento"
K_FLASH = "compras_flash"
K_VERSION_INGRESOS = "compras_ingresos_version"
K_VERSION_SECCIONES = "compras_secciones_version"
K_LINEA = "compras_linea_seq"
K_RESET_DESCUENTO = "compras_reset_descuento"

COLUMNAS_SECCION = ["Insumo", "Cantidad", "Precio Unitario"]
COLUMNAS_INGRESOS = ["Insumo", "Unidad", "Cantidad", "Precio Unitario", "Quitar"]


def _es_nulo(valor) -> bool:
    """True para NULL/NaN, sin depender del tipo devuelto por pandas."""
    return valor is None or valor != valor


def _siguiente_linea() -> int:
    siguiente = int(st.session_state.get(K_LINEA, 0)) + 1
    st.session_state[K_LINEA] = siguiente
    return siguiente


def _carrito() -> list[dict]:
    carrito = st.session_state.setdefault(K_CARRITO, [])
    for item in carrito:
        if "linea" not in item:
            item["linea"] = _siguiente_linea()
        item.setdefault("unidad", "")
    return carrito


def _version_seccion(id_categoria: int, incrementar: bool = False) -> int:
    versiones = st.session_state.setdefault(K_VERSION_SECCIONES, {})
    clave = int(id_categoria)
    if incrementar:
        versiones[clave] = int(versiones.get(clave, 0)) + 1
    return int(versiones.get(clave, 0))


def _bump_version_ingresos() -> None:
    st.session_state[K_VERSION_INGRESOS] = (
        int(st.session_state.get(K_VERSION_INGRESOS, 0)) + 1
    )


def _aplicar_reset_pendiente() -> None:
    if st.session_state.pop(K_RESET_DESCUENTO, False):
        st.session_state.pop(K_DESCUENTO, None)


def _mostrar_flash() -> None:
    flash = st.session_state.pop(K_FLASH, None)
    if not flash:
        return
    tipo, mensaje = flash
    if tipo == "success":
        st.success(mensaje)
    elif tipo == "warning":
        st.warning(mensaje)
    else:
        st.info(mensaje)


def _etiqueta_insumo(nombre: str, unidad: str) -> str:
    return f"{nombre} ({unidad})"


def _leer_seccion(
    editor: pd.DataFrame, insumos_por_etiqueta: dict[str, dict]
) -> tuple[list[dict], str | None]:
    """Convierte las filas capturadas en partidas válidas para el carrito."""
    partidas: list[dict] = []
    for _, fila in editor.iterrows():
        etiqueta = fila.get("Insumo")
        cantidad = fila.get("Cantidad")
        costo = fila.get("Precio Unitario")

        if _es_nulo(cantidad) or float(cantidad) <= 0:
            continue
        if _es_nulo(etiqueta) or not str(etiqueta).strip():
            return [], "Selecciona un insumo en cada fila con cantidad."
        if etiqueta not in insumos_por_etiqueta:
            return [], "Selecciona un insumo válido en cada fila capturada."
        if _es_nulo(costo) or float(costo) < 0:
            return [], f"Captura un precio unitario válido para {etiqueta}."

        insumo = insumos_por_etiqueta[etiqueta]
        partidas.append(
            {
                "id_insumo": int(insumo["id_insumo"]),
                "nombre": str(insumo["nombre"]),
                "unidad": str(insumo["unidad"]),
                "cantidad": float(cantidad),
                "costo_unitario": float(costo),
            }
        )
    return partidas, None


def _fusionar_partidas(partidas: list[dict]) -> None:
    """Agrega al carrito, sumando cantidades cuando coincide insumo y costo."""
    carrito = _carrito()
    for partida in partidas:
        existente = next(
            (
                item
                for item in carrito
                if int(item["id_insumo"]) == int(partida["id_insumo"])
                and abs(
                    float(item["costo_unitario"])
                    - float(partida["costo_unitario"])
                )
                < 0.0001
            ),
            None,
        )
        if existente is not None:
            existente["cantidad"] = float(existente["cantidad"]) + float(
                partida["cantidad"]
            )
            continue

        carrito.append(
            {
                "linea": _siguiente_linea(),
                "id_insumo": int(partida["id_insumo"]),
                "nombre": str(partida["nombre"]),
                "unidad": str(partida["unidad"]),
                "cantidad": float(partida["cantidad"]),
                "costo_unitario": float(partida["costo_unitario"]),
            }
        )


def _render_seccion(insumos: pd.DataFrame) -> None:
    id_categoria = int(insumos.iloc[0]["id_categoria"])
    etiquetas: list[str] = []
    insumos_por_etiqueta: dict[str, dict] = {}
    for _, insumo in insumos.iterrows():
        etiqueta = _etiqueta_insumo(
            str(insumo["nombre"]), str(insumo["unidad"])
        )
        etiquetas.append(etiqueta)
        insumos_por_etiqueta[etiqueta] = insumo.to_dict()

    st.caption(
        "Captura la cantidad y el precio unitario. Usa el botón + de la tabla "
        "para agregar más insumos de esta categoría."
    )
    editor = st.data_editor(
        pd.DataFrame(columns=COLUMNAS_SECCION),
        key=f"compras_seccion_{id_categoria}_{_version_seccion(id_categoria)}",
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_order=COLUMNAS_SECCION,
        column_config={
            "Insumo": st.column_config.SelectboxColumn(
                "Insumo",
                options=etiquetas,
                required=True,
                width="large",
            ),
            "Cantidad": st.column_config.NumberColumn(
                "Cantidad",
                min_value=0.001,
                step=0.5,
                format="%.3f",
                required=True,
            ),
            "Precio Unitario": st.column_config.NumberColumn(
                "Precio Unitario",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                required=True,
            ),
        },
    )

    if not st.button(
        "Agregar a Ingresos",
        key=f"agregar_ingresos_{id_categoria}",
        use_container_width=True,
    ):
        return

    partidas, error = _leer_seccion(editor, insumos_por_etiqueta)
    if error:
        st.error(error)
        return
    if not partidas:
        st.warning("Captura al menos una cantidad mayor a cero.")
        return

    _fusionar_partidas(partidas)
    _version_seccion(id_categoria, incrementar=True)
    _bump_version_ingresos()
    st.session_state[K_FLASH] = (
        "success",
        f"{len(partidas)} partida(s) agregada(s) a Ingresos.",
    )
    st.rerun()


def _registrar_compra(
    proveedor: dict, carrito: list[dict], descuento: float, auth: dict
) -> int:
    """Registra compra, detalles, movimientos, stock y auditoría en una transacción."""
    subtotal = round(
        sum(
            float(item["cantidad"]) * float(item["costo_unitario"])
            for item in carrito
        ),
        2,
    )
    descuento = round(float(descuento), 2)
    if descuento < 0 or descuento > subtotal:
        raise ValueError("El descuento no puede ser mayor al subtotal.")
    total = round(subtotal - descuento, 2)

    partidas_auditoria: list[dict] = []
    for item in carrito:
        cantidad = float(item["cantidad"])
        costo = float(item["costo_unitario"])
        if cantidad <= 0:
            raise ValueError("Todas las cantidades deben ser mayores a cero.")
        if costo < 0:
            raise ValueError("Los precios unitarios no pueden ser negativos.")
        partidas_auditoria.append(
            {
                "id_insumo": int(item["id_insumo"]),
                "nombre": str(item["nombre"]),
                "unidad": str(item.get("unidad", "")),
                "cantidad": cantidad,
                "costo_unitario": round(costo, 2),
                "subtotal": round(cantidad * costo, 2),
            }
        )

    datos_auditoria = {
        "id_proveedor": int(proveedor["id_proveedor"]),
        "proveedor": str(proveedor["nombre"]),
        "id_empleado": int(auth["id_empleado"]),
        "subtotal": subtotal,
        "descuento": descuento,
        "total": total,
        "partidas": partidas_auditoria,
    }

    with transaccion() as conn:
        id_tipo_entrada = conn.execute(
            text(
                "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario "
                "WHERE nombre = 'Entrada por compra'"
            )
        ).scalar_one()

        id_compra = conn.execute(
            text(
                """
                INSERT INTO compras (id_proveedor, id_empleado, total)
                VALUES (:proveedor, :empleado, :total)
                RETURNING id_compra
                """
            ),
            {
                "proveedor": int(proveedor["id_proveedor"]),
                "empleado": int(auth["id_empleado"]),
                "total": total,
            },
        ).scalar_one()

        for item in carrito:
            conn.execute(
                text(
                    """
                    INSERT INTO detalle_compra
                        (id_compra, id_insumo, cantidad, costo_unitario)
                    VALUES (:compra, :insumo, :cantidad, :costo)
                    """
                ),
                {
                    "compra": int(id_compra),
                    "insumo": int(item["id_insumo"]),
                    "cantidad": float(item["cantidad"]),
                    "costo": float(item["costo_unitario"]),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO movimientos_inventario
                        (id_insumo, id_tipo_movimiento, cantidad, motivo,
                         id_empleado, id_compra)
                    VALUES (:insumo, :tipo, :cantidad,
                            'Entrada por compra a proveedor', :empleado, :compra)
                    """
                ),
                {
                    "insumo": int(item["id_insumo"]),
                    "tipo": int(id_tipo_entrada),
                    "cantidad": float(item["cantidad"]),
                    "empleado": int(auth["id_empleado"]),
                    "compra": int(id_compra),
                },
            )
            conn.execute(
                text(
                    "UPDATE insumos SET stock_actual = stock_actual + :cantidad "
                    "WHERE id_insumo = :insumo"
                ),
                {
                    "cantidad": float(item["cantidad"]),
                    "insumo": int(item["id_insumo"]),
                },
            )

        conn.execute(
            text(
                """
                INSERT INTO auditoria
                    (id_usuario, tabla_afectada, id_registro_afectado, accion,
                     datos_nuevos)
                VALUES (:usuario, 'compras', :registro, 'INSERT',
                        CAST(:datos AS JSONB))
                """
            ),
            {
                "usuario": int(auth["id_usuario"]),
                "registro": int(id_compra),
                "datos": json.dumps(datos_auditoria, ensure_ascii=False),
            },
        )

    return int(id_compra)


def _render_ingresos(
    proveedor: dict, auth: dict, ids_categorias: list[int]
) -> None:
    carrito = _carrito()
    st.markdown("### Ingresos")
    if not carrito:
        st.info("Aún no has agregado productos a Ingresos.")
        return

    version = int(st.session_state.get(K_VERSION_INGRESOS, 0))
    datos = pd.DataFrame(
        [
            {
                "linea": int(item["linea"]),
                "Insumo": str(item["nombre"]),
                "Unidad": str(item.get("unidad", "")),
                "Cantidad": float(item["cantidad"]),
                "Precio Unitario": float(item["costo_unitario"]),
                "Quitar": False,
            }
            for item in carrito
        ]
    )
    editor = st.data_editor(
        datos,
        key=f"compras_ingresos_{version}",
        num_rows="fixed",
        hide_index=True,
        use_container_width=True,
        column_order=COLUMNAS_INGRESOS,
        column_config={
            "linea": None,
            "Insumo": st.column_config.TextColumn("Insumo", disabled=True),
            "Unidad": st.column_config.TextColumn("Unidad", disabled=True),
            "Cantidad": st.column_config.NumberColumn(
                "Cantidad",
                min_value=0.001,
                step=0.5,
                format="%.3f",
            ),
            "Precio Unitario": st.column_config.NumberColumn(
                "Precio Unitario",
                min_value=0.0,
                format="%.2f",
                disabled=True,
            ),
            "Quitar": st.column_config.CheckboxColumn("Quitar", default=False),
        },
        disabled=["Insumo", "Unidad", "Precio Unitario"],
    )

    por_linea = {int(item["linea"]): item for item in carrito}
    lineas = [int(item["linea"]) for item in carrito]
    lineas_a_quitar: set[int] = set()
    for posicion, (_, fila) in enumerate(editor.iterrows()):
        linea = fila.get("linea")
        if _es_nulo(linea) and posicion < len(lineas):
            linea = lineas[posicion]
        if _es_nulo(linea):
            continue
        item = por_linea.get(int(linea))
        if item is None:
            continue
        cantidad = fila.get("Cantidad")
        if _es_nulo(cantidad) or float(cantidad) <= 0:
            st.error(
                f"La cantidad de {item['nombre']} debe ser mayor a cero."
            )
            return
        item["cantidad"] = float(cantidad)
        if bool(fila.get("Quitar")):
            lineas_a_quitar.add(int(linea))

    if lineas_a_quitar:
        st.session_state[K_CARRITO] = [
            item
            for item in carrito
            if int(item["linea"]) not in lineas_a_quitar
        ]
        if not st.session_state[K_CARRITO]:
            st.session_state[K_RESET_DESCUENTO] = True
        _bump_version_ingresos()
        st.session_state[K_FLASH] = (
            "info",
            "Partida(s) quitada(s) de Ingresos.",
        )
        st.rerun()

    subtotal = round(
        sum(
            float(item["cantidad"]) * float(item["costo_unitario"])
            for item in carrito
        ),
        2,
    )
    col_subtotal, col_descuento, col_total = st.columns(3)
    col_subtotal.metric("Subtotal", f"${subtotal:,.2f}")
    with col_descuento:
        descuento = st.number_input(
            "Descuento (en MXN)",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            key=K_DESCUENTO,
        )
    descuento = round(float(descuento), 2)
    total = round(max(subtotal - descuento, 0.0), 2)
    col_total.metric("Total", f"${total:,.2f}")

    if descuento > subtotal:
        st.error("El descuento no puede ser mayor al subtotal.")
        return

    if not st.button(
        "Confirmar Ingresos", type="primary", use_container_width=True
    ):
        return

    requerir_accion("compras.registrar")
    try:
        id_compra = _registrar_compra(proveedor, carrito, descuento, auth)
    except (SQLAlchemyError, ValueError) as exc:
        st.error(f"No se pudo registrar la compra: {exc}")
        return

    st.session_state[K_CARRITO] = []
    st.session_state[K_LINEA] = 0
    _bump_version_ingresos()
    for id_categoria in ids_categorias:
        _version_seccion(id_categoria, incrementar=True)
    st.session_state[K_RESET_DESCUENTO] = True
    st.session_state[K_FLASH] = (
        "success",
        f"Compra #{id_compra} registrada y stock actualizado.",
    )
    st.rerun()


def render() -> None:
    requerir_rol(ADMIN, GERENTE)
    _aplicar_reset_pendiente()

    st.subheader("📥 Entrada de Producto")
    _mostrar_flash()

    auth = usuario_actual()
    if auth["id_empleado"] is None:
        st.error(
            "Tu cuenta de usuario no está vinculada a una ficha de empleado, "
            "así que no puede registrar compras. Pide a un administrador que la "
            "vincule en el módulo Usuarios."
        )
        return

    proveedores = run_query(
        "SELECT id_proveedor, nombre FROM proveedores "
        "WHERE activo = TRUE ORDER BY nombre"
    )
    insumos = run_query(
        """
        SELECT i.id_insumo, i.nombre, i.id_categoria,
               c.nombre AS categoria, u.abreviatura AS unidad
        FROM insumos i
        JOIN categorias_insumos c ON c.id_categoria = i.id_categoria
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE i.activo = TRUE
        ORDER BY c.nombre, i.nombre
        """
    )
    if proveedores.empty:
        st.warning(
            "Registra al menos un proveedor activo antes de capturar una compra."
        )
        return
    if insumos.empty:
        st.warning(
            "Registra al menos un insumo activo antes de capturar una compra."
        )
        return

    proveedor = st.selectbox(
        "Proveedor",
        proveedores.to_dict("records"),
        format_func=lambda p: p["nombre"],
        key=K_PROVEEDOR,
    )

    categorias = list(dict.fromkeys(insumos["categoria"]))
    ids_categorias = [
        int(insumos.loc[insumos["categoria"] == categoria, "id_categoria"].iloc[0])
        for categoria in categorias
    ]
    for tab, categoria in zip(st.tabs(categorias), categorias):
        with tab:
            _render_seccion(insumos[insumos["categoria"] == categoria])

    _render_ingresos(proveedor, auth, ids_categorias)
