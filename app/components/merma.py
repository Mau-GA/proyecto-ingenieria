"""Control compartido para reportar la merma de un producto.

Se usa tanto en Comandas (Cocina/Bar) como en Órdenes (Mesero/Caja/Gerente)
para que el flujo sea idéntico: el usuario captura el motivo y elige entre
dejar la partida tachada o repetirla en estado Pendiente. El reporte aparece
en la bandeja de Mermas del Gerente.
"""
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from permissions import requerir_accion
from services.mermas import (
    MermaError,
    registrar_merma_detalle,
    repetir_detalle_mermado,
)


def render_boton_mermar(
    id_detalle: int,
    *,
    origen: str,
    clave: str,
    etiqueta: str = "📉 Mermar",
    ayuda: str | None = None,
) -> None:
    """Muestra el popover para tachar la partida o tacharla y repetirla."""
    with st.popover(etiqueta, help=ayuda, use_container_width=True):
        motivo = st.text_input(
            "Motivo de la merma",
            key=f"merma_motivo_{clave}_{id_detalle}",
            placeholder="Ej. Se quemó el pan",
            max_chars=200,
        )
        st.caption("El reporte se enviará a la bandeja de Mermas del Gerente.")

        if st.button(
            "Mermar y dejar tachado",
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

        if st.button(
            "Mermar y repetir",
            key=f"merma_repetir_{clave}_{id_detalle}",
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
                    liberar_mesa_si_total=False,
                )
            except (MermaError, SQLAlchemyError) as exc:
                st.error(f"No se pudo reportar la merma: {exc}")
            else:
                try:
                    repetir_detalle_mermado(id_detalle=id_detalle)
                except (MermaError, SQLAlchemyError) as exc:
                    st.warning(
                        "La merma se reportó, pero no se pudo repetir la "
                        f"partida: {exc}"
                    )
                else:
                    st.success(
                        "Merma reportada y producto repetido en estado Pendiente."
                    )
                    st.rerun()
