"""Control compartido para reportar la merma de un producto.

Se usa tanto en Comandas (Cocina/Bar) como en Órdenes (Mesero/Caja/Gerente)
para que el flujo sea idéntico: el usuario captura el motivo, confirma y el
reporte aparece en la bandeja de Mermas del Gerente.
"""
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from permissions import requerir_accion
from services.mermas import MermaError, registrar_merma_detalle


def render_boton_mermar(
    id_detalle: int,
    *,
    origen: str,
    clave: str,
    etiqueta: str = "📉 Mermar",
    ayuda: str | None = None,
) -> None:
    """Muestra el popover de captura y confirma la merma de una partida."""
    with st.popover(etiqueta, help=ayuda, use_container_width=True):
        motivo = st.text_input(
            "Motivo de la merma",
            key=f"merma_motivo_{clave}_{id_detalle}",
            placeholder="Ej. Se quemó el pan",
            max_chars=200,
        )
        st.caption("El reporte se enviará a la bandeja de Mermas del Gerente.")

        if st.button(
            "Confirmar merma",
            key=f"merma_confirmar_{clave}_{id_detalle}",
            type="primary",
            use_container_width=True,
        ):
            requerir_accion("mermas.reportar")
            auth = usuario_actual()
            try:
                registrar_merma_detalle(
                    id_detalle=id_detalle,
                    id_usuario=auth["id_usuario"],
                    id_empleado=auth["id_empleado"],
                    motivo=motivo,
                    origen=origen,
                )
            except (MermaError, SQLAlchemyError) as exc:
                st.error(f"No se pudo reportar la merma: {exc}")
            else:
                st.success("Merma reportada al Gerente.")
                st.rerun()
