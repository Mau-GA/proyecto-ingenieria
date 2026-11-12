"""Módulo de Mesas: vista tipo mapa/tarjetas del salón."""
import streamlit as st

from database import run_query
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE, MESERO

COLOR_ESTADO = {
    "Disponible": "#2ecc71",
    "Ocupada": "#ff5a5f",
}


def render():
    requerir_rol(ADMIN, GERENTE, MESERO)
    st.subheader("🍽️ Mesas")

    mesas = run_query("SELECT id_mesa, numero_mesa, capacidad, estado FROM mesas ORDER BY numero_mesa")
    if mesas.empty:
        st.info("No hay mesas registradas todavía.")
        return

    cols = st.columns(4)
    for i, mesa in mesas.iterrows():
        with cols[i % 4]:
            color = COLOR_ESTADO.get(mesa["estado"], "#7f8c8d")
            st.markdown(
                f"""
                <div class="pos-card" style="border-left:4px solid {color}; margin-bottom:10px;">
                    <div class="pos-card-title">Mesa {mesa['numero_mesa']}</div>
                    <div class="pos-card-value">{mesa['estado']}</div>
                    <div class="pos-card-title">Capacidad: {mesa['capacidad']}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("Abrir orden", key=f"mesa_{mesa['id_mesa']}"):
                requerir_accion("mesas.abrir_orden")
                st.session_state["mesa_seleccionada"] = int(mesa["id_mesa"])
                st.session_state["modulo_activo"] = "ordenes"
                st.rerun()
