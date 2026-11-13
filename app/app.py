"""
Ruta: proyectorestaurante/app/app.py

Punto de entrada de la aplicación Streamlit.

Flujo: verificación de conexión -> pantalla de login -> sidebar con
navegación filtrada por rol -> despacha a la página del módulo activo.
El primer módulo del menú es FoodHouse (modules/foodhouse.py).

La validación de rol se hace de nuevo dentro de cada módulo (ver
permissions.requerir_rol), así que aunque alguien fuerce la navegación,
el backend vuelve a bloquear el acceso.
"""
import base64
import os

import streamlit as st

from auth import esta_autenticado, login
from components.sidebar import render_sidebar
from database import verificar_conexion
from modules import (
    comandas,
    compras,
    foodhouse,
    inventario,
    mermas_reporte,
    mesas,
    ordenes,
    productos,
    reportes,
    usuarios,
)
from permissions import puede_modulo

NOMBRE_RESTAURANTE = os.getenv("RESTAURANTE_NOMBRE", "Los Bro's Restaurant")

st.set_page_config(page_title=NOMBRE_RESTAURANTE, page_icon="🍴", layout="wide")

# Ruta absoluta: funciona aunque Streamlit se ejecute desde otra carpeta.
_CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")
if os.path.exists(_CSS):
    with open(_CSS, encoding="utf-8") as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

def _ruta_banner() -> str | None:
    """Busca BANNER.png en la carpeta de assets o en la raíz del proyecto."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidatos = (
        os.path.join(base, "assets", "BANNER.png"),
        os.path.join(base, "BANNER.png"),
        os.path.join(base, os.pardir, "BANNER.png"),
    )
    for ruta in candidatos:
        if os.path.exists(ruta):
            return ruta
    return None


def _banner_base64() -> str | None:
    """Lee BANNER.png y lo devuelve codificado en base64 para usarlo en CSS."""
    ruta = _ruta_banner()
    if not ruta:
        return None
    try:
        with open(ruta, "rb") as archivo:
            return base64.b64encode(archivo.read()).decode("ascii")
    except OSError:
        return None


def _aplicar_fondo_login() -> None:
    """Pone BANNER.png como fondo completo del Login con un velo oscuro."""
    banner = _banner_base64()
    if not banner:
        return
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background-image:
                linear-gradient(rgba(0, 0, 0, 0.80), rgba(0, 0, 0, 0.80)),
                url("data:image/png;base64,{banner}");
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


DESPACHADOR = {
    "foodhouse": foodhouse.render,
    "mesas": mesas.render,
    "comandas": comandas.render,
    "ordenes": ordenes.render,
    "mermas": mermas_reporte.render,
    "productos": productos.render,
    "inventario": inventario.render,
    "compras": compras.render,
    "usuarios": usuarios.render,
    "reportes": reportes.render,
}


def pantalla_login():
    _aplicar_fondo_login()
    st.markdown("<div class='login-spacer'></div>", unsafe_allow_html=True)

    # Columnas laterales para centrar el formulario sin ocupar todo el ancho.
    _, centro, _ = st.columns([1, 1.5, 1])

    with centro:
        st.markdown(
            f"<h1 class='login-welcome'>{NOMBRE_RESTAURANTE}</h1>",
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            with st.form("login"):
                usuario = st.text_input(
                    "Usuario",
                    placeholder="Tu usuario",
                    autocomplete="username",
                )
                password = st.text_input(
                    "Contraseña",
                    type="password",
                    placeholder="••••••••",
                    autocomplete="current-password",
                )
                enviado = st.form_submit_button(
                    "Ingresar", type="primary", use_container_width=True
                )

            if enviado:
                if login(usuario, password):
                    st.rerun()
                else:
                    st.error(
                        "Usuario o contraseña incorrectos, o el usuario está inactivo."
                    )

        st.caption("Acceso exclusivo para personal autorizado.")


def main():
    ok, msg = verificar_conexion()
    if not ok:
        st.error("No hay conexión con la base de datos.")
        st.code(msg)
        st.info("Revisa `docker compose ps` y `docker compose logs db`. "
                "En el primer arranque PostgreSQL tarda unos segundos en inicializarse.")
        st.stop()

    if not esta_autenticado():
        pantalla_login()
        return

    modulo_activo = render_sidebar()
    pagina = DESPACHADOR.get(modulo_activo)
    if pagina is None or not puede_modulo(modulo_activo):
        st.error("🚫 Acceso Denegado: tu rol no tiene permiso para acceder a esta sección.")
        st.stop()
    pagina()


if __name__ == "__main__":
    main()
