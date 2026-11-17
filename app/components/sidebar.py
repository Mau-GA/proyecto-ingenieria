"""Barra lateral: identidad del usuario, navegación filtrada por rol y logout."""
import os

import streamlit as st

from auth import usuario_actual, logout
from permissions import modulos_visibles

NOMBRE_RESTAURANTE = os.getenv("RESTAURANTE_NOMBRE", "Los Bro's Restaurant")

ETIQUETAS = {
    "foodhouse": "🍔 FoodHouse",
    "mesas": "🍽️ Mesas",
    "comandas": "🍳 Comandas",
    "ordenes": "🧾 Órdenes",
    "mermas": "📉 Mermas",
    "productos": "🍔 Productos",
    "inventario": "📦 Inventario",
    "compras": "🛒 Compras",
    "usuarios": "👤 Usuarios",
    "reportes": "📊 Reportes",
}


def render_sidebar() -> str:
    auth = usuario_actual()
    if not auth:
        st.stop()

    with st.sidebar:
        st.markdown(f"## 🍴 {NOMBRE_RESTAURANTE}")
        st.caption(f"Sesión: **{auth['nombre_usuario']}**")
        st.caption(f"Rol(es): {', '.join(auth['roles'])}")
        st.markdown("---")

        # Solo se ofrecen los módulos autorizados para los roles en sesión.
        disponibles = modulos_visibles()
        if not disponibles:
            st.error("Tu usuario no tiene ningún rol con módulos asignados.")
            if st.button("Cerrar sesión", use_container_width=True):
                logout()
                st.rerun()
            st.stop()

        # Navegación pedida desde otra pantalla (p. ej. Mesas → "Abrir orden").
        # Se aplica ANTES de crear el radio: Streamlit no permite cambiar el
        # valor de un widget con key después de haberlo dibujado.
        destino = st.session_state.pop("navegar_a", None)
        if destino in disponibles:
            st.session_state["nav_modulo"] = destino

        # Si no hay selección válida (primer ingreso o rol sin ese módulo),
        # se respeta modulo_activo si ya existía; si no, el primer módulo.
        if st.session_state.get("nav_modulo") not in disponibles:
            previo = st.session_state.get("modulo_activo")
            st.session_state["nav_modulo"] = (
                previo if previo in disponibles else disponibles[0]
            )

        # El radio usa key fija y SIN index: así su identidad no cambia entre
        # ejecuciones y ningún clic se pierde.
        seleccion = st.radio(
            "Navegación",
            disponibles,
            key="nav_modulo",
            format_func=lambda m: ETIQUETAS.get(m, m),
            label_visibility="collapsed",
        )
        st.session_state["modulo_activo"] = seleccion

        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            logout()
            st.rerun()

    return seleccion
