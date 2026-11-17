"""Avisos compartidos de lotes caducados y próximos a caducar."""
from __future__ import annotations

from decimal import Decimal

import streamlit as st

from permissions import ADMIN, GERENTE, tiene_rol
from services.lotes import ESTADO_CADUCADO, ESTADO_POR_VENCER, lotes_en_alerta


def _detalle(fila: dict) -> str:
    restante = Decimal(str(fila["cantidad_restante"]))
    dias = fila["dias_para_caducar"]
    if fila["estado"] == ESTADO_CADUCADO:
        cuando = f"caducó hace {abs(int(dias))} día(s)"
    else:
        cuando = f"caduca en {int(dias)} día(s)"
    fecha = fila["fecha_caducidad"]
    return (
        f"- {fila['insumo']}: {restante:.3f} {fila['unidad']} "
        f"(lote #{fila['id_lote']}, {cuando}, fecha {fecha})."
    )


def render_alertas_caducidad() -> None:
    """Muestra alertas de inventario solo a Administrador y Gerente."""
    if not tiene_rol(ADMIN, GERENTE):
        return
    alertas = lotes_en_alerta()
    caducados = [f for f in alertas if f["estado"] == ESTADO_CADUCADO]
    por_vencer = [f for f in alertas if f["estado"] == ESTADO_POR_VENCER]

    if caducados:
        st.error(
            f"🚨 {len(caducados)} lote(s) caducado(s) bloquean stock. "
            "Registra la merma desde Inventario > Lotes y caducidad."
        )
        for fila in caducados[:8]:
            st.caption(_detalle(fila))
    if por_vencer:
        st.warning(f"⚠️ {len(por_vencer)} lote(s) están próximos a caducar.")
        for fila in por_vencer[:8]:
            st.caption(_detalle(fila))
