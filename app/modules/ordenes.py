"""
Módulo de Órdenes (cuentas abiertas de mesas y delivery).

La vista cambia según el rol para respetar la matriz RBAC:
  - Gerente/Administrador: crea órdenes de mesa y cobra cuentas.
  - Mesero: crea/agrega partidas a cuentas de mesa y captura notas.
  - Caja: lista y cobra cuentas abiertas de mesas y delivery.
  - Cocina/Bar: no entran aquí; usan el módulo Comandas.

La pantalla se divide en dos pestañas:
  - Mesas: cuentas abiertas con `id_mesa`.
  - Delivery: pedidos sin mesa (FoodHouse / plataformas / para llevar).

Toda la escritura en BD vive en services/ventas.py y se hace en una sola
transacción por acción (guardar orden / cobrar / eliminar partida): si algo
falla, no queda nada a medias.

Regla de cobro: solo se puede cobrar cuando todas las partidas no mermadas
están en estado 'Listo' o 'Entregado'. La validación se repite en el backend
para que no se pueda saltar desde la interfaz.

Las notas de preparación por producto se guardan en la bitácora
`auditoria` (JSON) porque el esquema no tiene una columna específica y no
se debe modificar la base de datos.

El mesero/cajero también puede reportar la merma de una partida desde el
detalle de una cuenta abierta. La partida queda tachada, deja de sumar al
total y el reporte aparece en la bandeja del Gerente.
"""
import html

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from components.merma import render_boton_mermar
from database import run_query
from permissions import (
    requerir_rol,
    requerir_accion,
    ADMIN,
    CAJA,
    GERENTE,
    MESERO,
)
from services.ventas import (
    StockInsuficienteError,
    VentaError,
    cobrar_orden_mesa,
    eliminar_partida,
    guardar_orden_mesa,
    modificar_cantidad_partida,
    porciones_disponibles,
)


def _orden_abierta_de_mesa(id_mesa: int):
    df = run_query(
        """
        SELECT o.id_orden, o.id_mesa, o.total,
               o.numero_orden_diaria AS numero_dia
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        WHERE o.id_mesa = :id_mesa AND e.es_final = FALSE
        ORDER BY o.id_orden DESC LIMIT 1
        """,
        {"id_mesa": id_mesa},
    )
    return None if df.empty else df.iloc[0]


def _detalles_orden(id_orden: int):
    """Partidas de una orden con la última nota de preparación capturada."""
    return run_query(
        """
        SELECT d.id_detalle, d.cantidad, p.nombre AS producto,
               d.precio_unitario, d.subtotal, ed.nombre AS estado_detalle,
               (
                   SELECT a.datos_nuevos ->> 'comentario'
                   FROM auditoria a
                   WHERE a.tabla_afectada = 'detalles_orden_comentario'
                     AND a.id_registro_afectado = d.id_detalle
                   ORDER BY a.id_auditoria DESC
                   LIMIT 1
               ) AS comentario,
               (
                   SELECT a.datos_nuevos ->> 'motivo'
                   FROM auditoria a
                   WHERE a.tabla_afectada = 'detalles_orden_merma'
                     AND a.id_registro_afectado = d.id_detalle
                   ORDER BY a.id_auditoria DESC
                   LIMIT 1
               ) AS merma_motivo
        FROM detalles_orden d
        JOIN productos p ON p.id_producto = d.id_producto
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        WHERE d.id_orden = :orden
        ORDER BY d.id_detalle
        """,
        {"orden": id_orden},
    )


def _texto(valor) -> str:
    """Normaliza NULL/NaN de pandas a cadena vacía."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto in ("None", "nan") else texto


def _pendientes_de_cobro(id_orden: int) -> int:
    """Cantidad de partidas no mermadas que aún no están listas/entregadas."""
    fila = run_query(
        """
        SELECT COUNT(*) AS pendientes
        FROM detalles_orden d
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        WHERE d.id_orden = :orden
          AND ed.nombre NOT IN ('Listo', 'Entregado')
          AND NOT EXISTS (
              SELECT 1
              FROM auditoria a
              WHERE a.tabla_afectada = 'detalles_orden_merma'
                AND a.id_registro_afectado = d.id_detalle
          )
        """,
        {"orden": id_orden},
    )
    return 0 if fila.empty else int(fila.iloc[0]["pendientes"])


def _render_detalles(
    id_orden: int,
    permitir_eliminar: bool,
    permitir_mermar: bool = True,
) -> None:
    detalles = _detalles_orden(id_orden)
    if detalles.empty:
        st.caption("La cuenta no tiene productos.")
        return

    auth = usuario_actual()
    for _, detalle in detalles.iterrows():
        id_detalle = int(detalle["id_detalle"])
        merma_motivo = _texto(detalle["merma_motivo"])
        mermado = bool(merma_motivo)
        titulo = f"{int(detalle['cantidad'])}× {detalle['producto']}"
        subtotal = f"${float(detalle['subtotal']):.2f}"

        anchos = [3, 2, 2]
        if permitir_mermar and not mermado:
            anchos.append(1.6)
        if permitir_eliminar:
            anchos.append(1)
        columnas = st.columns(anchos)

        columnas[0].markdown(f"~~**{titulo}**~~" if mermado else f"**{titulo}**")
        comentario = _texto(detalle["comentario"])
        if comentario:
            columnas[1].caption(f"📝 {comentario}")
        columnas[2].markdown(f"~~{subtotal}~~" if mermado else subtotal)

        if mermado:
            st.markdown(
                f"<span class='tag-mermado'>MERMADO</span> "
                f"Motivo: {html.escape(merma_motivo)} · Este producto no se cobra.",
                unsafe_allow_html=True,
            )
            continue

        indice = 3
        if permitir_mermar:
            with columnas[indice]:
                render_boton_mermar(
                    id_detalle,
                    origen="orden",
                    clave="orden",
                    etiqueta="📉 Mermar",
                    ayuda="Reportar este producto como merma",
                )
            indice += 1

        if permitir_eliminar:
            if str(detalle["estado_detalle"]) == "Pendiente":
                if columnas[indice].button(
                    "🗑️",
                    key=f"eliminar_detalle_{id_detalle}",
                    help="Eliminar producto de la cuenta",
                ):
                    requerir_accion("ordenes.eliminar_partida")
                    try:
                        eliminar_partida(
                            id_detalle=id_detalle,
                            id_usuario=auth["id_usuario"],
                            id_empleado=auth["id_empleado"],
                        )
                    except (StockInsuficienteError, VentaError, SQLAlchemyError) as exc:
                        st.error(f"No se pudo eliminar el producto: {exc}")
                    else:
                        st.rerun()
            else:
                columnas[indice].caption("En preparación")

        if (
            permitir_eliminar
            and str(detalle["estado_detalle"]) == "Pendiente"
        ):
            c_cantidad, c_actualizar = st.columns([2, 1])
            nueva_cantidad = c_cantidad.number_input(
                "Cantidad",
                min_value=1,
                value=int(detalle["cantidad"]),
                step=1,
                key=f"cantidad_detalle_{id_detalle}",
            )
            if c_actualizar.button(
                "Actualizar",
                key=f"actualizar_detalle_{id_detalle}",
                help="Cambiar la cantidad de esta partida",
            ):
                requerir_accion("ordenes.eliminar_partida")
                try:
                    modificar_cantidad_partida(
                        id_detalle=id_detalle,
                        nueva_cantidad=int(nueva_cantidad),
                        id_usuario=auth["id_usuario"],
                        id_empleado=auth["id_empleado"],
                    )
                except (
                    StockInsuficienteError,
                    VentaError,
                    SQLAlchemyError,
                ) as exc:
                    st.error(f"No se pudo actualizar la cantidad: {exc}")
                else:
                    st.rerun()


def _agregar_producto_orden(
    producto_sel: dict, cantidad: int, comentario: str
) -> None:
    """Callback: valida existencias, agrega al carrito y limpia la nota."""
    carrito = st.session_state.setdefault("carrito", [])
    porciones = producto_sel.get("porciones")
    if porciones is not None:
        en_carrito = sum(
            int(item["cantidad"])
            for item in carrito
            if int(item["id_producto"]) == int(producto_sel["id_producto"])
        )
        maximo = max(int(porciones) - en_carrito, 0)
        if maximo <= 0:
            st.session_state["carrito_error"] = (
                f"{producto_sel['nombre']} está agotado: no quedan porciones "
                "disponibles con el stock actual."
            )
            return
        if int(cantidad) > maximo:
            st.session_state["carrito_error"] = (
                f"Solo puedes agregar {maximo} unidad(es) más de "
                f"{producto_sel['nombre']} (ya hay {en_carrito} en el carrito)."
            )
            return

    st.session_state.pop("carrito_error", None)
    carrito.append(
        {
            "id_producto": int(producto_sel["id_producto"]),
            "nombre": producto_sel["nombre"],
            "cantidad": int(cantidad),
            "precio": float(producto_sel["precio_actual"]),
            "id_area_preparacion": int(producto_sel["id_area_preparacion"]),
            "comentario": comentario.strip(),
        }
    )
    st.session_state["orden_comentario"] = ""


def _render_constructor_orden(id_mesa: int):
    requerir_accion("ordenes.crear")
    mesa = run_query(
        "SELECT numero_mesa FROM mesas WHERE id_mesa = :m", {"m": id_mesa}
    )
    numero = int(mesa.iloc[0]["numero_mesa"]) if not mesa.empty else id_mesa
    st.markdown(f"### Mesa seleccionada: {numero}")
    productos = run_query(
        """
        SELECT p.id_producto, p.nombre, p.precio_actual,
               p.id_area_preparacion, c.nombre AS categoria
        FROM productos p
        JOIN categorias_productos c ON c.id_categoria = p.id_categoria
        WHERE p.activo = TRUE AND p.disponible = TRUE
        ORDER BY c.nombre, p.nombre
        """
    )
    if productos.empty:
        st.warning("No hay productos disponibles. Da de alta productos en el módulo Productos.")
        return

    carrito = st.session_state.setdefault("carrito", [])
    porciones = porciones_disponibles(None)
    registros = productos.to_dict("records")
    for registro in registros:
        registro["porciones"] = porciones.get(int(registro["id_producto"]))

    categorias = sorted({registro["categoria"] for registro in registros})
    tab_cat = st.selectbox("Categoría", categorias)
    subset = [
        registro for registro in registros
        if registro["categoria"] == tab_cat
    ]

    def _etiqueta_producto(producto: dict) -> str:
        etiqueta = f"{producto['nombre']} (${producto['precio_actual']})"
        disponibles = producto["porciones"]
        if disponibles is None:
            return etiqueta + " — Sin receta"
        if int(disponibles) <= 0:
            return etiqueta + " — Agotado"
        return etiqueta + f" — {int(disponibles)} porción(es)"

    col1, col2 = st.columns([3, 1])
    with col1:
        producto_sel = st.selectbox(
            "Producto", subset, format_func=_etiqueta_producto,
        )
    with col2:
        cantidad = st.number_input("Cant.", min_value=1, value=1, step=1)

    en_carrito = sum(
        int(item["cantidad"])
        for item in carrito
        if int(item["id_producto"]) == int(producto_sel["id_producto"])
    )
    restantes = (
        None
        if producto_sel["porciones"] is None
        else max(int(producto_sel["porciones"]) - en_carrito, 0)
    )
    if restantes is not None:
        if restantes <= 0:
            st.warning(
                f"{producto_sel['nombre']} está agotado con el stock actual."
            )
        else:
            st.caption(f"Máximo disponible para agregar: {restantes}.")

    comentario = st.text_input(
        "Comentarios/Notas de preparación (opcional)",
        max_chars=200,
        placeholder="Ej. sin cebolla, término medio, sin hielo…",
        key="orden_comentario",
    )

    st.button(
        "➕ Agregar producto",
        on_click=_agregar_producto_orden,
        args=(producto_sel, int(cantidad), comentario),
        disabled=restantes == 0,
    )
    error_carrito = st.session_state.pop("carrito_error", None)
    if error_carrito:
        st.warning(error_carrito)

    if carrito:
        st.markdown("#### Carrito")
        total = 0.0
        for i, item in enumerate(carrito):
            subtotal = item["cantidad"] * item["precio"]
            total += subtotal
            c1, c2 = st.columns([5, 1])
            c1.write(f"{item['cantidad']} × {item['nombre']} — ${subtotal:.2f}")
            if item.get("comentario"):
                c1.caption(f"📝 {item['comentario']}")
            if c2.button("Quitar", key=f"quitar_{i}"):
                carrito.pop(i)
                st.rerun()
        st.caption("Cada unidad se guardará como una partida individual para poder mermarla por separado.")
        st.markdown(f"**Total del carrito: ${total:.2f}**")

        if st.button("✅ Enviar a cocina/bar y guardar orden", type="primary"):
            auth = usuario_actual()
            partidas = [
                {"id_producto": it["id_producto"], "cantidad": it["cantidad"],
                 "precio_unitario": it["precio"], "id_area_preparacion": it["id_area_preparacion"],
                 "comentario": it.get("comentario")}
                for it in carrito
            ]
            try:
                resultado = guardar_orden_mesa(
                    id_mesa=id_mesa, partidas=partidas,
                    id_usuario=auth["id_usuario"], id_empleado=auth["id_empleado"],
                )
            except StockInsuficienteError as exc:
                st.error("No se guardó la orden: no alcanza el inventario.")
                for f in exc.faltantes:
                    st.markdown(f"- **{f.insumo}**: se requieren {f.requerido:.3f} {f.unidad}, "
                                f"hay {f.disponible:.3f} {f.unidad}")
                return
            except (VentaError, SQLAlchemyError) as exc:
                st.error(f"No se guardó la orden: {exc}")
                return
            st.session_state["carrito"] = []
            st.success(f"Orden #{resultado['numero_dia']} actualizado correctamente.")
            st.rerun()


def _render_cobro(orden, id_mesa: int | None) -> None:
    """Calculadora de efectivo y cobro integrados en la tarjeta de la orden."""
    requerir_accion("ordenes.cobrar")
    id_orden = int(orden["id_orden"])
    numero_orden = int(orden["numero_dia"])
    total = float(orden["total"])
    if total <= 0:
        st.info(
            "La cuenta no tiene importe cobrable: todos sus productos fueron "
            "mermados o eliminados."
        )
        return

    pendientes = _pendientes_de_cobro(id_orden)
    if pendientes > 0:
        st.warning(
            f"No se puede cobrar todavía: {pendientes} producto(s) siguen "
            "pendientes o en preparación. Cocina y bar deben marcarlos como "
            "Listo o Entregado."
        )
        return

    metodos = run_query(
        "SELECT id_metodo_pago, nombre, requiere_referencia "
        "FROM metodos_pago WHERE activo = TRUE ORDER BY id_metodo_pago"
    )
    if metodos.empty:
        st.error("No hay métodos de pago activos.")
        return

    metodo = st.selectbox(
        "Método de pago",
        metodos.to_dict("records"),
        format_func=lambda m: m["nombre"],
        key=f"cobro_metodo_{id_orden}",
    )
    referencia = None
    if metodo["requiere_referencia"]:
        referencia = st.text_input(
            "Referencia de pago (voucher / folio)",
            max_chars=100,
            key=f"cobro_referencia_{id_orden}",
        )

    es_efectivo = metodo["nombre"] == "Efectivo"
    cambio = None
    if es_efectivo:
        recibido = st.number_input(
            "Efectivo recibido",
            min_value=0.0,
            step=10.0,
            format="%.2f",
            key=f"cobro_recibido_{id_orden}",
        )
        cambio = recibido - total
        if recibido > 0:
            st.caption(f"Cambio: ${max(cambio, 0):.2f}")

    if st.button("💰 Cobrar y cerrar", type="primary", key=f"cobro_boton_{id_orden}",
                 use_container_width=True):
        if metodo["requiere_referencia"] and not referencia:
            st.error("Este método de pago requiere referencia.")
            return
        if es_efectivo and cambio is not None and cambio < 0:
            st.error("El efectivo recibido es menor al total.")
            return

        auth = usuario_actual()
        try:
            cobrar_orden_mesa(
                id_orden=id_orden,
                id_mesa=id_mesa,
                id_metodo_pago=int(metodo["id_metodo_pago"]),
                id_usuario=auth["id_usuario"],
                referencia_pago=referencia,
            )
        except (VentaError, SQLAlchemyError) as exc:
            st.error(f"No se pudo cobrar: {exc}")
            return
        if id_mesa is not None:
            st.session_state.pop("mesa_seleccionada", None)
            estado_mesa = run_query(
                "SELECT estado FROM mesas WHERE id_mesa = :m",
                {"m": id_mesa},
            )
            if not estado_mesa.empty and estado_mesa.iloc[0]["estado"] == "Disponible":
                st.success(f"Orden #{numero_orden} cobrado. Mesa liberada.")
            else:
                st.success(
                    f"Orden #{numero_orden} cobrado. La mesa sigue ocupada "
                    "por otra cuenta abierta."
                )
        else:
            st.success(f"Orden #{numero_orden} cobrado.")
        st.rerun()


def _render_cuentas_abiertas(
    *,
    solo_mesas: bool,
    permitir_eliminar: bool,
    puede_cobrar: bool,
) -> None:
    """Lista cuentas abiertas de mesas o de delivery en tarjetas visuales."""
    st.markdown("#### Cuentas abiertas por cobrar" if solo_mesas else "#### Pedidos de delivery abiertos")
    abiertas = run_query(
        """
        SELECT o.id_orden, o.id_mesa, o.total, o.fecha_orden,
               o.numero_orden_diaria AS numero_dia,
               m.numero_mesa, t.nombre AS tipo, e.nombre AS estado,
               pe.nombre AS plataforma, o.referencia_externa
        FROM ordenes o
        JOIN estados_orden e ON e.id_estado = o.id_estado
        JOIN tipos_orden t ON t.id_tipo_orden = o.id_tipo_orden
        LEFT JOIN mesas m ON m.id_mesa = o.id_mesa
        LEFT JOIN plataformas_externas pe ON pe.id_plataforma = o.id_plataforma_externa
        WHERE e.es_final = FALSE
          AND (
              (:solo_mesas = 1 AND o.id_mesa IS NOT NULL)
              OR (:solo_mesas = 0 AND o.id_mesa IS NULL)
          )
        ORDER BY o.fecha_orden, o.id_orden
        """,
        {"solo_mesas": 1 if solo_mesas else 0},
    )
    if abiertas.empty:
        st.info(
            "No hay cuentas abiertas de mesas."
            if solo_mesas
            else "No hay pedidos de delivery abiertos."
        )
        return

    for _, orden in abiertas.iterrows():
        id_orden = int(orden["id_orden"])
        numero_orden = int(orden["numero_dia"])
        total = float(orden["total"])
        id_mesa = None
        if _texto(orden["id_mesa"]):
            id_mesa = int(orden["id_mesa"])

        if solo_mesas:
            numero_mesa = _texto(orden["numero_mesa"])
            ubicacion = f"Mesa {int(numero_mesa)}" if numero_mesa else "Mesa"
        else:
            plataforma = _texto(orden["plataforma"])
            referencia = _texto(orden["referencia_externa"])
            ubicacion = plataforma or "Delivery"
            if referencia:
                ubicacion += f" · Pedido {referencia}"

        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 3, 1])
            c1.markdown(f"**Orden #{numero_orden}**")
            c2.markdown(str(orden["tipo"]))
            c3.markdown(ubicacion)
            c4.markdown(f"**${total:.2f}**")

            etiqueta = "Abrir cuenta / cobrar" if puede_cobrar else "Ver comanda"
            with st.expander(etiqueta):
                st.markdown(
                    f"#### Orden #{numero_orden} — Total: ${total:.2f}"
                )
                _render_detalles(id_orden, permitir_eliminar=permitir_eliminar)
                if puede_cobrar:
                    _render_cobro(orden, id_mesa)
                else:
                    st.caption(
                        "Puedes reportar mermas. El cobro lo realiza Caja cuando "
                        "cocina y bar terminen la preparación."
                    )


def _render_tab_mesas(*, puede_crear: bool, puede_cobrar: bool) -> None:
    if puede_crear:
        id_mesa = st.session_state.get("mesa_seleccionada")
        if id_mesa is None:
            st.info(
                "Selecciona una mesa desde el módulo **Mesas** para abrir o "
                "continuar una orden."
            )
        else:
            orden = _orden_abierta_de_mesa(id_mesa)
            _render_constructor_orden(id_mesa)
            if orden is not None:
                numero_orden = int(orden["numero_dia"])
                st.markdown(
                    f"#### Cuenta abierta — Orden #{numero_orden} — "
                    f"Total: ${float(orden['total']):.2f}"
                )
                _render_detalles(
                    int(orden["id_orden"]),
                    permitir_eliminar=puede_cobrar,
                )
                if puede_cobrar:
                    _render_cobro(orden, id_mesa)
                else:
                    st.caption(
                        "El cobro lo realiza Caja cuando cocina y bar terminen "
                        "la preparación."
                    )
            return

    if puede_cobrar:
        _render_cuentas_abiertas(
            solo_mesas=True,
            permitir_eliminar=True,
            puede_cobrar=True,
        )


def _render_tab_delivery(*, puede_cobrar: bool) -> None:
    _render_cuentas_abiertas(
        solo_mesas=False,
        permitir_eliminar=puede_cobrar,
        puede_cobrar=puede_cobrar,
    )


def render():
    requerir_rol(ADMIN, GERENTE, MESERO, CAJA)

    auth = usuario_actual()
    roles = set(auth.get("roles") or [])
    es_admin = bool(roles & {ADMIN, GERENTE})
    puede_crear = es_admin or MESERO in roles
    puede_cobrar = es_admin or CAJA in roles

    st.subheader("🧾 Órdenes")
    tab_mesas, tab_delivery = st.tabs(["🍽️ Mesas", "🛵 Delivery"])
    with tab_mesas:
        _render_tab_mesas(puede_crear=puede_crear, puede_cobrar=puede_cobrar)
    with tab_delivery:
        _render_tab_delivery(puede_cobrar=puede_cobrar)
