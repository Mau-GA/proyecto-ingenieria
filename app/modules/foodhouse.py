"""
Ruta: proyectorestaurante/app/modules/foodhouse.py

FoodHouse (antes Punto de venta): captura pedidos que quedan pendientes de
cobro.

Pantalla:
  - Izquierda: catálogo de productos por categoría + búsqueda.
  - Derecha: ticket actual (tipo de orden, plataforma, partidas y total).

Solo se ofrecen los tipos de orden "Delivery" (Plataforma Externa) y
"Para llevar" (Para Llevar).

Al pulsar "Mandar comanda" se llama a services.ventas.registrar_comanda(),
que en UNA sola transacción crea la orden en estado 'Pendiente' (sin pago),
sus partidas desglosadas 1 a 1 y descuenta el inventario según receta. Si
falta stock de algún insumo, no se guarda nada y se muestra exactamente qué
insumo falta. El cobro se realiza después desde Órdenes > Delivery.

Para cuentas abiertas por mesa (consumir y pagar al final) usa el módulo
Órdenes > Mesas.
"""
from __future__ import annotations

from decimal import Decimal

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from database import run_query
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE, CAJA
from services.ventas import (
    EmpleadoRequeridoError,
    StockInsuficienteError,
    VentaError,
    dinero,
    registrar_comanda,
)

K_CARRITO = "foodhouse_carrito"          # {id_producto: {"nombre","precio","id_area","cantidad"}}
K_ULTIMA = "foodhouse_ultima_comanda"    # dict con el resultado de la última comanda

# Tipos de orden permitidos en FoodHouse: nombre en BD -> etiqueta visible.
TIPOS_FOODHOUSE = {
    "Plataforma Externa": "Delivery",
    "Para Llevar": "Para llevar",
}


def fmt(valor) -> str:
    return f"${dinero(valor):,.2f}"


# =====================================================================
# Catálogos (cacheados para no consultar en cada clic)
# =====================================================================
@st.cache_data(ttl=60, show_spinner=False)
def cargar_productos():
    return run_query(
        """
        SELECT p.id_producto, p.nombre, p.precio_actual, p.id_area_preparacion,
               c.nombre AS categoria
        FROM productos p
        JOIN categorias_productos c ON c.id_categoria = p.id_categoria
        WHERE p.activo = TRUE AND p.disponible = TRUE
        ORDER BY c.nombre, p.nombre
        """
    )


@st.cache_data(ttl=300, show_spinner=False)
def cargar_catalogos() -> dict:
    return {
        "tipos_orden": run_query(
            "SELECT id_tipo_orden, nombre, requiere_mesa, requiere_plataforma "
            "FROM tipos_orden ORDER BY id_tipo_orden"
        ),
        "plataformas": run_query(
            "SELECT id_plataforma, nombre FROM plataformas_externas "
            "WHERE activo = TRUE ORDER BY nombre"
        ),
    }


def cargar_mesas_disponibles():
    # Sin caché: el estado de las mesas cambia constantemente.
    return run_query(
        "SELECT id_mesa, numero_mesa, capacidad FROM mesas "
        "WHERE estado = 'Disponible' ORDER BY numero_mesa"
    )


def ventas_de_hoy():
    return run_query(
        """
        SELECT COUNT(DISTINCT p.id_orden) AS ordenes,
               COALESCE(SUM(p.monto), 0) AS total
        FROM pagos p
        WHERE DATE(p.fecha_pago) = CURRENT_DATE
        """
    ).iloc[0]


# =====================================================================
# Carrito
# =====================================================================
def agregar_al_carrito(prod) -> None:
    carrito = st.session_state[K_CARRITO]
    pid = int(prod["id_producto"])
    if pid in carrito:
        carrito[pid]["cantidad"] += 1
    else:
        carrito[pid] = {
            "nombre": prod["nombre"],
            "precio": dinero(prod["precio_actual"]),
            "id_area": int(prod["id_area_preparacion"]),
            "cantidad": 1,
        }


def cambiar_cantidad(pid: int, delta: int) -> None:
    carrito = st.session_state[K_CARRITO]
    if pid in carrito:
        carrito[pid]["cantidad"] += delta
        if carrito[pid]["cantidad"] <= 0:
            del carrito[pid]


def total_carrito() -> Decimal:
    return sum(
        (i["precio"] * i["cantidad"] for i in st.session_state[K_CARRITO].values()),
        Decimal("0.00"),
    )


def partidas_del_carrito() -> list[dict]:
    """Convierte el carrito al formato que espera services.ventas."""
    return [
        {"id_producto": pid, "cantidad": i["cantidad"],
         "precio_unitario": i["precio"], "id_area_preparacion": i["id_area"]}
        for pid, i in st.session_state[K_CARRITO].items()
    ]


# =====================================================================
# Interfaz
# =====================================================================
def _render_catalogo() -> None:
    st.markdown("#### Productos")
    productos = cargar_productos()
    if productos.empty:
        st.info("No hay productos activos y disponibles. Dalos de alta en el módulo Productos.")
        return

    busqueda = st.text_input("Buscar", placeholder="Nombre del producto…",
                             label_visibility="collapsed", key="foodhouse_busqueda")
    if busqueda:
        productos = productos[productos["nombre"].str.contains(busqueda, case=False, na=False)]

    categorias = list(dict.fromkeys(productos["categoria"]))  # orden estable, sin duplicados
    if not categorias:
        st.caption("Sin coincidencias.")
        return

    for tab, categoria in zip(st.tabs(categorias), categorias):
        with tab:
            filas = productos[productos["categoria"] == categoria].to_dict("records")
            cols = st.columns(3)
            for i, prod in enumerate(filas):
                with cols[i % 3]:
                    if st.button(
                        f"{prod['nombre']}\n\n{fmt(prod['precio_actual'])}",
                        key=f"foodhouse_prod_{prod['id_producto']}",
                        use_container_width=True,
                    ):
                        agregar_al_carrito(prod)
                        st.rerun()


def _render_ultima_venta() -> None:
    venta = st.session_state.get(K_ULTIMA)
    if not venta:
        return
    numero_orden = venta.get("numero_dia", venta["id_orden"])
    st.success(
        f"Comanda orden #{numero_orden} registrada · Total {fmt(venta['total'])} · "
        "Pendiente de cobro en Órdenes > Delivery"
    )
    if venta["inventario"]:
        with st.expander("Insumos descontados del inventario"):
            st.dataframe(
                [
                    {"Insumo": r["insumo"], "Descontado": f"{r['cantidad']:.3f} {r['unidad']}",
                     "Stock restante": f"{r['stock_restante']:.3f} {r['unidad']}"}
                    for r in venta["inventario"]
                ],
                hide_index=True, use_container_width=True,
            )


def _render_ticket(catalogos: dict, auth: dict) -> None:
    st.markdown("#### Orden actual")
    _render_ultima_venta()

    # --- Tipo de orden y datos condicionales ---
    # Solo Delivery (Plataforma Externa) y Para llevar.
    tipos = {
        int(t["id_tipo_orden"]): t
        for t in catalogos["tipos_orden"].to_dict("records")
        if t["nombre"] in TIPOS_FOODHOUSE
    }
    if not tipos:
        st.error("No están configurados los tipos de orden 'Plataforma Externa' y 'Para Llevar'.")
        return

    id_tipo = st.selectbox(
        "Tipo de orden",
        list(tipos),
        format_func=lambda i: TIPOS_FOODHOUSE[tipos[i]["nombre"]],
        key="foodhouse_tipo",
    )
    tipo = tipos[id_tipo]

    id_mesa = id_plataforma = None
    referencia_externa = None
    if tipo["requiere_mesa"]:
        mesas = cargar_mesas_disponibles()
        if mesas.empty:
            st.warning("No hay mesas disponibles.")
        else:
            etiquetas = {
                int(m["id_mesa"]): f"Mesa {m['numero_mesa']} ({m['capacidad']} personas)"
                for m in mesas.to_dict("records")
            }
            id_mesa = st.selectbox("Mesa", list(etiquetas), format_func=etiquetas.get, key="foodhouse_mesa")
    if tipo["requiere_plataforma"]:
        plataformas = dict(zip(catalogos["plataformas"]["id_plataforma"].astype(int),
                               catalogos["plataformas"]["nombre"]))
        if plataformas:
            id_plataforma = st.selectbox("Plataforma de delivery", list(plataformas),
                                         format_func=plataformas.get, key="foodhouse_plataforma")
        referencia_externa = st.text_input("Número de pedido en la plataforma", max_chars=50,
                                           key="foodhouse_ref_ext")

    # --- Partidas ---
    carrito = st.session_state[K_CARRITO]
    with st.container(border=True):
        if not carrito:
            st.caption("Agrega productos desde el catálogo.")
        for pid, item in list(carrito.items()):
            c1, c2, c3, c4 = st.columns([5, 1, 1, 2])
            c1.markdown(f"**{item['cantidad']}×** {item['nombre']}")
            c2.button("➖", key=f"foodhouse_menos_{pid}", on_click=cambiar_cantidad, args=(pid, -1))
            c3.button("➕", key=f"foodhouse_mas_{pid}", on_click=cambiar_cantidad, args=(pid, 1))
            c4.markdown(f"<div style='text-align:right'>{fmt(item['precio'] * item['cantidad'])}</div>",
                        unsafe_allow_html=True)

    total = total_carrito()
    st.markdown(f"### Total: {fmt(total)}")

    col_a, col_b = st.columns(2)
    if col_a.button("🗑️ Vaciar", use_container_width=True, disabled=not carrito, key="foodhouse_vaciar"):
        st.session_state[K_CARRITO] = {}
        st.rerun()

    if col_b.button("📤 Mandar comanda", type="primary", use_container_width=True,
                    disabled=not carrito, key="foodhouse_mandar"):
        requerir_accion("foodhouse.vender")
        # --- Validaciones de captura antes de tocar la BD ---
        errores = []
        if tipo["requiere_mesa"] and id_mesa is None:
            errores.append("Este tipo de orden requiere mesa.")
        if tipo["requiere_plataforma"] and (id_plataforma is None or not referencia_externa):
            errores.append("Indica plataforma y número de pedido.")
        if errores:
            for e in errores:
                st.error(e)
            return

        try:
            resultado = registrar_comanda(
                partidas=partidas_del_carrito(),
                id_tipo_orden=id_tipo,
                id_usuario=auth["id_usuario"],
                id_empleado=auth["id_empleado"],
                id_mesa=id_mesa,
                id_plataforma=id_plataforma,
                referencia_externa=referencia_externa,
            )
        except StockInsuficienteError as exc:
            st.error("No se registró la comanda: no alcanza el inventario.")
            for f in exc.faltantes:
                st.markdown(f"- **{f.insumo}**: se requieren {f.requerido:.3f} {f.unidad}, "
                            f"hay {f.disponible:.3f} {f.unidad}")
            st.caption("Registra una compra o un ajuste en Inventario, o marca el producto "
                       "como no disponible en Productos.")
            return
        except (EmpleadoRequeridoError, VentaError) as exc:
            st.error(str(exc))
            return
        except SQLAlchemyError as exc:
            st.error(f"No se pudo registrar la comanda (no se guardó nada): {exc}")
            return

        st.session_state[K_ULTIMA] = resultado
        st.session_state[K_CARRITO] = {}
        st.rerun()


def render() -> None:
    requerir_rol(ADMIN, GERENTE, CAJA)
    auth = usuario_actual()
    st.session_state.setdefault(K_CARRITO, {})
    st.session_state.setdefault(K_ULTIMA, None)

    encabezado, metrica = st.columns([3, 1])
    encabezado.subheader("🍔 FoodHouse")
    hoy = ventas_de_hoy()
    metrica.metric("Cobrado hoy", fmt(hoy["total"]), f"{int(hoy['ordenes'])} órdenes")

    if auth["id_empleado"] is None:
        st.warning("Tu usuario no está vinculado a un empleado: solo podrás vender productos "
                   "sin receta. Pide a un administrador que lo vincule en Usuarios.")

    catalogos = cargar_catalogos()
    izquierda, derecha = st.columns([3, 2], gap="large")
    with izquierda:
        _render_catalogo()
    with derecha:
        _render_ticket(catalogos, auth)
