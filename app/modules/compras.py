"""Módulo de Compras: entrada de producto por secciones y actualización de stock."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import streamlit as st
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from database import run_query, transaccion
from permissions import requerir_accion, requerir_rol, ADMIN, GERENTE
from services.bitacora import registrar_evento
from services.lotes import obtener_dias_aviso
from services.proveedores import (
    ProveedorError,
    actualizar_proveedor,
    cambiar_estado_proveedor,
    costos_acordados,
    crear_proveedor,
    desvincular_insumo,
    listar_historial_precios,
    listar_insumos_proveedor,
    listar_proveedores,
    vincular_insumo,
)

K_CARRITO = "carrito_compra"
K_PROVEEDOR = "compras_proveedor"
K_DESCUENTO = "compras_descuento"
K_FLASH = "compras_flash"
K_VERSION_INGRESOS = "compras_ingresos_version"
K_VERSION_SECCIONES = "compras_secciones_version"
K_LINEA = "compras_linea_seq"
K_RESET_DESCUENTO = "compras_reset_descuento"

COLUMNAS_INGRESOS = [
    "Insumo",
    "Unidad",
    "Cantidad",
    "Precio Unitario",
    "Caducidad",
    "Quitar",
]
K_FLASH_PROVEEDORES = "compras_proveedores_flash"


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


def _normalizar_fecha(valor) -> date | None:
    """Convierte date/datetime/Timestamp/ISO en date; None si viene vacío."""
    if valor is None or valor != valor:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError as exc:
        raise ValueError(f"Fecha de caducidad inválida: {valor}") from exc


def _render_seccion(
    insumos: pd.DataFrame,
    costos: dict[int, Decimal],
    id_proveedor: int,
) -> None:
    """Captura una línea de compra con costo sugerido y caducidad condicional."""
    id_categoria = int(insumos.iloc[0]["id_categoria"])
    registros = insumos.to_dict("records")
    etiqueta = lambda i: _etiqueta_insumo(str(i["nombre"]), str(i["unidad"]))
    version = f"{id_categoria}_{id_proveedor}"

    st.caption(
        "Selecciona el insumo. Si tiene costo acordado se propone como precio; "
        "puedes cambiarlo y se mostrará un aviso."
    )
    insumo = st.selectbox(
        "Insumo",
        registros,
        format_func=etiqueta,
        key=f"compras_insumo_{version}",
    )
    id_insumo = int(insumo["id_insumo"])
    acordado = costos.get(id_insumo)
    if acordado is not None:
        st.caption(f"Costo acordado con el proveedor: ${acordado:,.2f}")

    hoy = _normalizar_fecha(
        run_query("SELECT CURRENT_DATE AS hoy").iloc[0]["hoy"]
    )
    col_cantidad, col_precio = st.columns(2)
    cantidad = col_cantidad.number_input(
        "Cantidad",
        min_value=0.001,
        step=0.5,
        format="%.3f",
        key=f"compras_cantidad_{version}_{id_insumo}",
    )
    costo_referencia = insumo.get("costo_unitario_referencia")
    if acordado is not None:
        precio_inicial = float(acordado)
    elif _es_nulo(costo_referencia):
        precio_inicial = 0.0
    else:
        precio_inicial = float(costo_referencia)
    precio = col_precio.number_input(
        "Precio Unitario",
        min_value=0.0,
        step=1.0,
        format="%.2f",
        value=precio_inicial,
        key=f"compras_precio_{version}_{id_insumo}",
    )

    requiere = bool(insumo.get("requiere_caducidad"))
    fecha = st.date_input(
        "Fecha de caducidad",
        value=None,
        min_value=(hoy or date.today()) + timedelta(days=1),
        key=f"compras_caducidad_{version}_{id_insumo}",
        help=(
            "Obligatoria para insumos que requieren caducidad."
            if requiere
            else "Opcional para este insumo."
        ),
    )
    if requiere:
        st.caption("⚠️ Este insumo requiere fecha de caducidad posterior a hoy.")

    if acordado is not None and abs(float(precio) - float(acordado)) > 0.005:
        st.warning(
            "El costo capturado es distinto del costo acordado "
            f"(${acordado:,.2f}). Se guardará el costo capturado."
        )

    if not st.button(
        "Agregar a Ingresos",
        key=f"agregar_ingresos_{id_categoria}",
        use_container_width=True,
    ):
        return

    if float(cantidad) <= 0:
        st.error("Captura una cantidad mayor a cero.")
        return
    fecha_normal = _normalizar_fecha(fecha)
    if requiere and fecha_normal is None:
        st.error("Captura la fecha de caducidad para este insumo.")
        return
    if requiere and hoy is not None and fecha_normal <= hoy:
        st.error("La fecha de caducidad debe ser posterior a hoy.")
        return

    _fusionar_partidas(
        [
            {
                "id_insumo": id_insumo,
                "nombre": str(insumo["nombre"]),
                "unidad": str(insumo["unidad"]),
                "cantidad": float(cantidad),
                "costo_unitario": float(precio),
                "fecha_caducidad": fecha_normal,
                "requiere_caducidad": requiere,
            }
        ]
    )
    _version_seccion(id_categoria, incrementar=True)
    _bump_version_ingresos()
    st.session_state[K_FLASH] = (
        "success",
        f"1 partida agregada a Ingresos.",
    )
    st.rerun()


def _fusionar_partidas(partidas: list[dict]) -> None:
    """Agrega al carrito, sumando líneas con insumo, costo y caducidad iguales."""
    carrito = _carrito()
    for partida in partidas:
        fecha = _normalizar_fecha(partida.get("fecha_caducidad"))
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
                and _normalizar_fecha(item.get("fecha_caducidad")) == fecha
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
                "fecha_caducidad": fecha,
                "requiere_caducidad": bool(
                    partida.get("requiere_caducidad", False)
                ),
            }
        )


def _registrar_compra(
    proveedor: dict,
    carrito: list[dict],
    descuento: float,
    auth: dict,
    fechas_caducidad: dict | None = None,
) -> int:
    """Registra compra, lotes, costo promedio, stock y bitácora en una transacción.

    La fecha de caducidad se toma de ``fechas_caducidad`` (por número de línea
    o id de insumo) o del propio item del carrito. Para insumos con
    ``requiere_caducidad`` es obligatoria y debe ser posterior a hoy. Si algo
    falla, la transacción completa hace ROLLBACK.
    """
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
    fechas = dict(fechas_caducidad or {})

    partidas_compra: list[dict] = []
    for item in carrito:
        cantidad = Decimal(str(item["cantidad"])).quantize(Decimal("0.001"))
        costo = Decimal(str(item["costo_unitario"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if cantidad <= 0:
            raise ValueError("Todas las cantidades deben ser mayores a cero.")
        if costo < 0:
            raise ValueError("Los precios unitarios no pueden ser negativos.")

        linea = item.get("linea")
        fecha = None
        if linea is not None and linea in fechas:
            fecha = fechas[linea]
        elif int(item["id_insumo"]) in fechas:
            fecha = fechas[int(item["id_insumo"])]
        else:
            fecha = item.get("fecha_caducidad")
        fecha = _normalizar_fecha(fecha)

        partidas_compra.append(
            {
                "linea": linea,
                "id_insumo": int(item["id_insumo"]),
                "nombre": str(item["nombre"]),
                "unidad": str(item.get("unidad", "")),
                "cantidad": cantidad,
                "costo_unitario": costo,
                "subtotal": (cantidad * costo).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                "fecha_caducidad": fecha,
            }
        )

    with transaccion() as conn:
        hoy = conn.execute(text("SELECT CURRENT_DATE")).scalar_one()
        ids_insumos = sorted({p["id_insumo"] for p in partidas_compra})
        filas = conn.execute(
            text(
                """
                SELECT id_insumo, nombre, stock_actual,
                       costo_unitario_referencia, requiere_caducidad
                FROM insumos
                WHERE id_insumo IN :ids AND activo = TRUE
                ORDER BY id_insumo
                FOR UPDATE
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids_insumos},
        ).mappings().all()
        por_id = {int(f["id_insumo"]): f for f in filas}
        if len(por_id) != len(ids_insumos):
            raise ValueError("Uno de los insumos de la compra ya no existe.")

        for partida in partidas_compra:
            datos_insumo = por_id[partida["id_insumo"]]
            fecha = partida["fecha_caducidad"]
            if bool(datos_insumo["requiere_caducidad"]):
                if fecha is None:
                    raise ValueError(
                        f"Captura la fecha de caducidad de "
                        f"{partida['nombre']}; es obligatoria."
                    )
                if fecha <= hoy:
                    raise ValueError(
                        f"La fecha de caducidad de {partida['nombre']} debe "
                        "ser posterior a hoy."
                    )

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

        stock_estado = {
            id_insumo: Decimal(str(fila["stock_actual"]))
            for id_insumo, fila in por_id.items()
        }
        costo_estado = {
            id_insumo: (
                None
                if fila["costo_unitario_referencia"] is None
                else Decimal(str(fila["costo_unitario_referencia"]))
            )
            for id_insumo, fila in por_id.items()
        }
        costos_evento: list[dict] = []

        for partida in partidas_compra:
            id_insumo = partida["id_insumo"]
            cantidad = partida["cantidad"]
            costo_compra = partida["costo_unitario"]
            stock_anterior = stock_estado[id_insumo]
            costo_anterior = costo_estado[id_insumo]

            if costo_anterior is None or stock_anterior == 0:
                costo_nuevo = costo_compra
            else:
                costo_nuevo = (
                    (stock_anterior * costo_anterior)
                    + (cantidad * costo_compra)
                ) / (stock_anterior + cantidad)
            costo_nuevo = costo_nuevo.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            id_detalle_compra = conn.execute(
                text(
                    """
                    INSERT INTO detalle_compra
                        (id_compra, id_insumo, cantidad, costo_unitario)
                    VALUES (:compra, :insumo, :cantidad, :costo)
                    RETURNING id_detalle_compra
                    """
                ),
                {
                    "compra": int(id_compra),
                    "insumo": id_insumo,
                    "cantidad": cantidad,
                    "costo": costo_compra,
                },
            ).scalar_one()

            id_lote = conn.execute(
                text(
                    """
                    INSERT INTO lotes_insumo
                        (id_insumo, id_detalle_compra, cantidad_recibida,
                         fecha_caducidad)
                    VALUES (:insumo, :detalle, :cantidad, :caducidad)
                    RETURNING id_lote
                    """
                ),
                {
                    "insumo": id_insumo,
                    "detalle": int(id_detalle_compra),
                    "cantidad": cantidad,
                    "caducidad": partida["fecha_caducidad"],
                },
            ).scalar_one()

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
                    "insumo": id_insumo,
                    "tipo": int(id_tipo_entrada),
                    "cantidad": cantidad,
                    "empleado": int(auth["id_empleado"]),
                    "compra": int(id_compra),
                },
            )
            conn.execute(
                text(
                    "UPDATE insumos SET stock_actual = stock_actual + :cantidad, "
                    "costo_unitario_referencia = :costo "
                    "WHERE id_insumo = :insumo"
                ),
                {
                    "cantidad": cantidad,
                    "costo": costo_nuevo,
                    "insumo": id_insumo,
                },
            )

            stock_nuevo = stock_anterior + cantidad
            stock_estado[id_insumo] = stock_nuevo
            costo_estado[id_insumo] = costo_nuevo
            partida["id_lote"] = int(id_lote)
            partida["costo_anterior"] = costo_anterior
            partida["costo_nuevo"] = costo_nuevo
            partida["stock_anterior"] = stock_anterior
            partida["stock_nuevo"] = stock_nuevo
            costos_evento.append(
                {
                    "id_insumo": id_insumo,
                    "insumo": partida["nombre"],
                    "costo_anterior": costo_anterior,
                    "costo_nuevo": costo_nuevo,
                    "stock_anterior": stock_anterior,
                    "stock_nuevo": stock_nuevo,
                }
            )

        registrar_evento(
            conn,
            tipo_evento="COMPRA",
            entidad="compras",
            id_entidad=int(id_compra),
            id_usuario=int(auth["id_usuario"]),
            datos_nuevos={
                "id_proveedor": int(proveedor["id_proveedor"]),
                "proveedor": str(proveedor["nombre"]),
                "id_empleado": int(auth["id_empleado"]),
                "subtotal": subtotal,
                "descuento": descuento,
                "total": total,
                "partidas": partidas_compra,
                "costos": costos_evento,
                "id_compra": int(id_compra),
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
                "Caducidad": item.get("fecha_caducidad"),
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
            "Caducidad": st.column_config.DateColumn(
                "Caducidad",
                format="YYYY-MM-DD",
                help="Obligatoria para insumos que requieren caducidad.",
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
        fecha = _normalizar_fecha(fila.get("Caducidad"))
        if item.get("requiere_caducidad") and fecha is None:
            st.error(
                f"Captura la fecha de caducidad de {item['nombre']}; "
                "es obligatoria."
            )
            return
        item["fecha_caducidad"] = fecha
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


def _mostrar_flash_proveedores() -> None:
    flash = st.session_state.pop(K_FLASH_PROVEEDORES, None)
    if not flash:
        return
    tipo, mensaje = flash
    if tipo == "success":
        st.success(mensaje)
    elif tipo == "warning":
        st.warning(mensaje)
    else:
        st.info(mensaje)


def _render_proveedores(auth: dict, insumos: pd.DataFrame) -> None:
    """Directorio y administración de proveedores, vínculos y costos."""
    requerir_accion("proveedores.gestionar")
    st.markdown("### Proveedores")
    _mostrar_flash_proveedores()

    with st.expander("➕ Nuevo proveedor", expanded=False):
        with st.form("nuevo_proveedor", clear_on_submit=True):
            nombre = st.text_input("Nombre *", max_chars=100)
            col_tel, col_correo = st.columns(2)
            telefono = col_tel.text_input("Teléfono", max_chars=20)
            correo = col_correo.text_input("Correo", max_chars=100)
            direccion = st.text_input("Dirección", max_chars=255)
            condiciones = st.text_input(
                "Condiciones", max_chars=200,
                help="Ej. crédito 30 días, entrega martes y jueves.",
            )
            alta = st.form_submit_button(
                "Guardar proveedor", type="primary", use_container_width=True
            )
        if alta:
            requerir_accion("proveedores.gestionar")
            try:
                crear_proveedor(
                    nombre=nombre,
                    telefono=telefono,
                    direccion=direccion,
                    correo=correo,
                    condiciones=condiciones,
                    id_usuario=int(auth["id_usuario"]),
                )
            except ProveedorError as exc:
                st.error(str(exc))
            except SQLAlchemyError as exc:
                st.error(f"No se pudo guardar el proveedor: {exc}")
            else:
                st.session_state[K_FLASH_PROVEEDORES] = (
                    "success",
                    "Proveedor registrado.",
                )
                st.rerun()

    proveedores = listar_proveedores(incluir_inactivos=True)
    if proveedores.empty:
        st.info("Aún no hay proveedores registrados.")
        return

    st.dataframe(
        proveedores[
            ["nombre", "telefono", "correo", "condiciones", "activo"]
        ].rename(
            columns={
                "nombre": "Proveedor",
                "telefono": "Teléfono",
                "correo": "Correo",
                "condiciones": "Condiciones",
                "activo": "Activo",
            }
        ),
        hide_index=True,
        use_container_width=True,
    )

    seleccion = st.selectbox(
        "Proveedor a administrar",
        proveedores.to_dict("records"),
        format_func=lambda p: (
            f"{p['nombre']} ({'activo' if p['activo'] else 'inactivo'})"
        ),
        key="compras_proveedor_admin",
    )
    id_proveedor = int(seleccion["id_proveedor"])

    with st.expander("✏️ Editar proveedor", expanded=False):
        with st.form(f"editar_proveedor_{id_proveedor}"):
            nombre_edit = st.text_input(
                "Nombre *", value=str(seleccion["nombre"]), max_chars=100
            )
            col_tel, col_correo = st.columns(2)
            telefono_edit = col_tel.text_input(
                "Teléfono",
                value="" if _es_nulo(seleccion["telefono"]) else str(seleccion["telefono"]),
                max_chars=20,
            )
            correo_edit = col_correo.text_input(
                "Correo",
                value="" if _es_nulo(seleccion["correo"]) else str(seleccion["correo"]),
                max_chars=100,
            )
            direccion_edit = st.text_input(
                "Dirección",
                value=(
                    ""
                    if _es_nulo(seleccion["direccion"])
                    else str(seleccion["direccion"])
                ),
                max_chars=255,
            )
            condiciones_edit = st.text_input(
                "Condiciones",
                value=(
                    ""
                    if _es_nulo(seleccion["condiciones"])
                    else str(seleccion["condiciones"])
                ),
                max_chars=200,
            )
            activo_edit = st.checkbox(
                "Proveedor activo", value=bool(seleccion["activo"])
            )
            guardar = st.form_submit_button(
                "Guardar cambios", type="primary", use_container_width=True
            )
        if guardar:
            requerir_accion("proveedores.gestionar")
            try:
                actualizar_proveedor(
                    id_proveedor=id_proveedor,
                    nombre=nombre_edit,
                    telefono=telefono_edit,
                    direccion=direccion_edit,
                    correo=correo_edit,
                    condiciones=condiciones_edit,
                    activo=activo_edit,
                    id_usuario=int(auth["id_usuario"]),
                )
            except ProveedorError as exc:
                st.error(str(exc))
            except SQLAlchemyError as exc:
                st.error(f"No se pudo actualizar el proveedor: {exc}")
            else:
                st.session_state[K_FLASH_PROVEEDORES] = (
                    "success",
                    "Proveedor actualizado.",
                )
                st.rerun()

    if st.button(
        "Desactivar proveedor" if seleccion["activo"] else "Reactivar proveedor",
        key=f"estado_proveedor_{id_proveedor}",
        use_container_width=True,
    ):
        requerir_accion("proveedores.gestionar")
        try:
            cambiar_estado_proveedor(
                id_proveedor=id_proveedor,
                activo=not bool(seleccion["activo"]),
                id_usuario=int(auth["id_usuario"]),
            )
        except ProveedorError as exc:
            st.error(str(exc))
        except SQLAlchemyError as exc:
            st.error(f"No se pudo cambiar el estado: {exc}")
        else:
            st.session_state[K_FLASH_PROVEEDORES] = (
                "success",
                "Estado del proveedor actualizado.",
            )
            st.rerun()

    st.markdown("#### Insumos y costos acordados")
    vinculados = listar_insumos_proveedor(id_proveedor)
    if vinculados.empty:
        st.info("Este proveedor no tiene insumos vinculados.")
    else:
        st.dataframe(
            vinculados[
                ["insumo", "unidad", "costo_acordado", "activo"]
            ].rename(
                columns={
                    "insumo": "Insumo",
                    "unidad": "Unidad",
                    "costo_acordado": "Costo acordado",
                    "activo": "Activo",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )

    if insumos.empty:
        st.warning("No hay insumos activos para vincular.")
    else:
        opciones_insumo = insumos.to_dict("records")
        with st.form(f"vincular_insumo_{id_proveedor}"):
            insumo_vincular = st.selectbox(
                "Insumo",
                opciones_insumo,
                format_func=lambda i: _etiqueta_insumo(
                    str(i["nombre"]), str(i["unidad"])
                ),
            )
            costo_acordado = st.number_input(
                "Costo acordado", min_value=0.0, step=1.0, format="%.2f"
            )
            vincular = st.form_submit_button(
                "Vincular / actualizar costo",
                type="primary",
                use_container_width=True,
            )
        if vincular:
            requerir_accion("proveedores.gestionar")
            try:
                vincular_insumo(
                    id_proveedor=id_proveedor,
                    id_insumo=int(insumo_vincular["id_insumo"]),
                    costo_acordado=costo_acordado,
                    id_usuario=int(auth["id_usuario"]),
                )
            except ProveedorError as exc:
                st.error(str(exc))
            except SQLAlchemyError as exc:
                st.error(f"No se pudo vincular el insumo: {exc}")
            else:
                st.session_state[K_FLASH_PROVEEDORES] = (
                    "success",
                    "Insumo y costo acordado guardados.",
                )
                st.rerun()

    activos = vinculados[vinculados["activo"] == True] if not vinculados.empty else vinculados
    if not activos.empty:
        with st.form(f"desvincular_insumo_{id_proveedor}"):
            insumo_quitar = st.selectbox(
                "Insumo a desvincular",
                activos.to_dict("records"),
                format_func=lambda i: str(i["insumo"]),
            )
            desvincular = st.form_submit_button(
                "Desvincular insumo", use_container_width=True
            )
        if desvincular:
            requerir_accion("proveedores.gestionar")
            try:
                desvincular_insumo(
                    id_proveedor=id_proveedor,
                    id_insumo=int(insumo_quitar["id_insumo"]),
                    id_usuario=int(auth["id_usuario"]),
                )
            except ProveedorError as exc:
                st.error(str(exc))
            except SQLAlchemyError as exc:
                st.error(f"No se pudo desvincular el insumo: {exc}")
            else:
                st.session_state[K_FLASH_PROVEEDORES] = (
                    "success",
                    "Insumo desvinculado.",
                )
                st.rerun()

    with st.expander("🕘 Historial de precios", expanded=False):
        historial = listar_historial_precios(id_proveedor)
        if historial.empty:
            st.info("Aún no hay cambios de precio registrados.")
        else:
            st.dataframe(
                historial[
                    ["insumo", "costo_anterior", "costo_nuevo", "nombre_usuario", "fecha"]
                ].rename(
                    columns={
                        "insumo": "Insumo",
                        "costo_anterior": "Costo anterior",
                        "costo_nuevo": "Costo nuevo",
                        "nombre_usuario": "Usuario",
                        "fecha": "Fecha",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )


def render() -> None:
    requerir_rol(ADMIN, GERENTE)
    _aplicar_reset_pendiente()

    st.subheader("📥 Entrada de Producto")
    _mostrar_flash()

    auth = usuario_actual()
    proveedores = listar_proveedores(incluir_inactivos=False)
    insumos = run_query(
        """
        SELECT i.id_insumo, i.nombre, i.id_categoria,
               c.nombre AS categoria, u.abreviatura AS unidad,
               i.requiere_caducidad, i.costo_unitario_referencia
        FROM insumos i
        JOIN categorias_insumos c ON c.id_categoria = i.id_categoria
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE i.activo = TRUE
        ORDER BY c.nombre, i.nombre
        """
    )

    tab_compras, tab_proveedores = st.tabs(
        ["Capturar compra", "Proveedores"]
    )

    with tab_compras:
        if auth["id_empleado"] is None:
            st.error(
                "Tu cuenta de usuario no está vinculada a una ficha de empleado, "
                "así que no puede registrar compras. Pide a un administrador que "
                "la vincule en el módulo Usuarios."
            )
        elif proveedores.empty:
            st.warning(
                "Registra al menos un proveedor activo antes de capturar una "
                "compra. Puedes hacerlo en la pestaña Proveedores."
            )
        elif insumos.empty:
            st.warning(
                "Registra al menos un insumo activo antes de capturar una compra."
            )
        else:
            proveedor = st.selectbox(
                "Proveedor",
                proveedores.to_dict("records"),
                format_func=lambda p: p["nombre"],
                key=K_PROVEEDOR,
            )
            costos = costos_acordados(int(proveedor["id_proveedor"]))

            categorias = list(dict.fromkeys(insumos["categoria"]))
            ids_categorias = [
                int(
                    insumos.loc[
                        insumos["categoria"] == categoria, "id_categoria"
                    ].iloc[0]
                )
                for categoria in categorias
            ]
            for tab, categoria in zip(st.tabs(categorias), categorias):
                with tab:
                    _render_seccion(
                        insumos[insumos["categoria"] == categoria],
                        costos,
                        int(proveedor["id_proveedor"]),
                    )

            _render_ingresos(proveedor, auth, ids_categorias)

    with tab_proveedores:
        _render_proveedores(auth, insumos)
