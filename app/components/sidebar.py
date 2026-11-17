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

        actual = st.session_state.get("modulo_activo")
        if actual not in disponibles:
            actual = disponibles[0]
            st.session_state["modulo_activo"] = actual

        seleccion = st.radio(
            "Navegación",
            disponibles,
            index=disponibles.index(actual),
            format_func=lambda m: ETIQUETAS.get(m, m),
            label_visibility="collapsed",
        )
        st.session_state["modulo_activo"] = seleccion

        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            logout()
            st.rerun()

    return seleccion
