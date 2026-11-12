"""
Módulo de Comandas (antes KDS) para Cocina y Bar.

Muestra las comandas pendientes, en preparación y listas del área
correspondiente al rol. Cocina ve el área Cocina; Bar ve el área Bar;
Gerente/Administrador ven ambas áreas en modo de lectura. El Gerente
además puede limpiar la sección Listo pasando sus partidas a Entregado.

Los comentarios/notas de preparación capturados por el mesero se
recuperan de la bitácora `auditoria`, donde se guardan asociados al
id del detalle de la orden.

Antes de marcar un producto como listo, Cocina/Bar puede reportarlo como
merma. Esa marca también vive en `auditoria` y el reporte se envía a la
bandeja del Gerente (Inventario > Gestión de Mermas).
"""
import html

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from components.merma import render_boton_mermar
from database import run_query, execute
from permissions import (
    requerir_rol,
    requerir_accion,
    ADMIN,
    BAR,
    COCINA,
    GERENTE,
)
from services.ventas import VentaError, limpiar_comandas_listas

ESTADOS = ("Pendiente", "En preparación", "Listo")


def _cambiar_estado_detalle(id_detalle: int, nombre_estado: str) -> None:
    requerir_accion("comandas.preparar")
    estado = run_query(
        "SELECT id_estado FROM estados_detalle_orden WHERE nombre = :n", {"n": nombre_estado}
    )
    if estado.empty:
        st.error(f"No existe el estado de preparación '{nombre_estado}'.")
        return
    execute(
        "UPDATE detalles_orden SET id_estado = :e WHERE id_detalle = :d",
        {"e": int(estado.iloc[0]["id_estado"]), "d": id_detalle},
    )
    st.rerun()


def _areas_del_usuario(roles: set[str]) -> list[str]:
    if roles & {ADMIN, GERENTE}:
        return ["Cocina", "Bar"]
    areas = []
    if COCINA in roles:
        areas.append("Cocina")
    if BAR in roles:
        areas.append("Bar")
    return areas


def _comandas_por_estado():
    """Consulta las comandas activas incluyendo la nota de preparación."""
    return run_query(
        """
        SELECT d.id_detalle, d.id_orden, d.id_producto, d.cantidad,
               p.nombre AS producto, a.nombre AS area, ed.nombre AS estado,
               m.numero_mesa, n.comentario, mm.merma_motivo,
               o.numero_orden_diaria AS numero_dia
        FROM detalles_orden d
        JOIN productos p ON p.id_producto = d.id_producto
        JOIN areas_preparacion a ON a.id_area = d.id_area_preparacion
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        JOIN ordenes o ON o.id_orden = d.id_orden
        JOIN estados_orden eo ON eo.id_estado = o.id_estado
        LEFT JOIN mesas m ON m.id_mesa = o.id_mesa
        LEFT JOIN (
            SELECT au.id_registro_afectado AS id_detalle,
                   au.datos_nuevos ->> 'comentario' AS comentario
            FROM auditoria au
            JOIN (
                SELECT id_registro_afectado, MAX(id_auditoria) AS ultimo
                FROM auditoria
                WHERE tabla_afectada = 'detalles_orden_comentario'
                GROUP BY id_registro_afectado
            ) ult ON ult.ultimo = au.id_auditoria
        ) n ON n.id_detalle = d.id_detalle
        LEFT JOIN (
            SELECT au.id_registro_afectado AS id_detalle,
                   au.datos_nuevos ->> 'motivo' AS merma_motivo
            FROM auditoria au
            JOIN (
                SELECT id_registro_afectado, MAX(id_auditoria) AS ultimo
                FROM auditoria
                WHERE tabla_afectada = 'detalles_orden_merma'
                GROUP BY id_registro_afectado
            ) ult ON ult.ultimo = au.id_auditoria
        ) mm ON mm.id_detalle = d.id_detalle
        WHERE eo.nombre <> 'Cancelada'
          AND ed.nombre IN ('Pendiente', 'En preparación', 'Listo')
        ORDER BY o.fecha_orden, d.id_detalle
        """
    )


def render() -> None:
    requerir_rol(ADMIN, GERENTE, COCINA, BAR)

    auth = usuario_actual()
    roles = set(auth.get("roles") or [])
    areas = _areas_del_usuario(roles)
    solo_lectura = bool(roles & {ADMIN, GERENTE})
    es_gerente = GERENTE in roles

    st.subheader("🍳 Comandas")
    if not areas:
        st.info("Tu rol no tiene un área de preparación asignada.")
        return

    comandas = _comandas_por_estado()
    if not comandas.empty:
        comandas = comandas[comandas["area"].isin(areas)]
    if comandas.empty:
        st.success("No hay comandas activas.")
        return

    if solo_lectura:
        st.caption(
            "Vista de solo lectura. El Gerente puede limpiar la sección Listo."
        )

    for tab, estado in zip(st.tabs(list(ESTADOS)), ESTADOS):
        with tab:
            if estado == "Listo" and es_gerente:
                if st.button(
                    "Limpiar sección",
                    key="comandas_limpiar_listo",
                    type="primary",
                    use_container_width=True,
                ):
                    requerir_accion("comandas.limpiar")
                    try:
                        total = limpiar_comandas_listas()
                    except (VentaError, SQLAlchemyError) as exc:
                        st.error(f"No se pudo limpiar la sección: {exc}")
                    else:
                        if total == 0:
                            st.info("No había partidas en estado Listo.")
                        else:
                            st.success(
                                f"{total} partida(s) marcada(s) como Entregado."
                            )
                            st.rerun()

            del_estado = comandas[comandas["estado"] == estado]
            if del_estado.empty:
                st.caption(f"Sin comandas en estado «{estado}».")
                continue

            for area in areas:
                subset = del_estado[del_estado["area"] == area]
                if subset.empty:
                    continue
                st.markdown(f"**{area}**")
                for _, fila in subset.iterrows():
                    _render_tarjeta(fila, estado, solo_lectura)


def _texto(valor) -> str:
    """Normaliza NULL/NaN de pandas a cadena vacía."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto in ("None", "nan") else texto


def _render_tarjeta(fila, estado: str, solo_lectura: bool) -> None:
    with st.container(border=True):
        merma_motivo = _texto(fila["merma_motivo"])
        mermado = bool(merma_motivo)
        titulo = f"{int(fila['cantidad'])}× {fila['producto']}"

        c1, c2 = st.columns([4, 2])
        c1.markdown(f"~~**{titulo}**~~" if mermado else f"**{titulo}**")
        mesa = (
            f"Mesa {int(fila['numero_mesa'])}"
            if fila["numero_mesa"] == fila["numero_mesa"]
            else "Sin mesa"
        )
        c2.caption(f"Orden #{int(fila['numero_dia'])} · {mesa} · {fila['area']}")

        comentario = _texto(fila["comentario"])
        if comentario:
            st.markdown(f"📝 **Nota de preparación:** {comentario}")

        if mermado:
            st.markdown(
                f"<span class='tag-mermado'>MERMADO</span> "
                f"Motivo: {html.escape(merma_motivo)}",
                unsafe_allow_html=True,
            )
            st.caption("Este producto ya fue reportado. No se debe preparar ni cobrar.")
            return

        if solo_lectura:
            return

        id_detalle = int(fila["id_detalle"])
        if estado == "Pendiente":
            c_accion, c_merma = st.columns([1, 1])
            if c_accion.button("Iniciar preparación", key=f"comanda_ini_{id_detalle}",
                               use_container_width=True):
                _cambiar_estado_detalle(id_detalle, "En preparación")
            with c_merma:
                render_boton_mermar(id_detalle, origen="comanda", clave="comanda")
        elif estado == "En preparación":
            c_accion, c_merma = st.columns([1, 1])
            if c_accion.button("Marcar listo", key=f"comanda_listo_{id_detalle}",
                               use_container_width=True):
                _cambiar_estado_detalle(id_detalle, "Listo")
            with c_merma:
                render_boton_mermar(id_detalle, origen="comanda", clave="comanda")
        else:
            if st.button("Marcar entregado", key=f"comanda_entrega_{id_detalle}",
                         use_container_width=True):
                _cambiar_estado_detalle(id_detalle, "Entregado")
