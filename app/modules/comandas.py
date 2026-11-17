"""
Módulo de Comandas (KDS) para Cocina y Bar.

Muestra las comandas pendientes, en preparación y listas del área
correspondiente al rol. Cocina ve el área Cocina; Bar ve el área Bar;
Gerente/Administrador ven ambas áreas en modo de lectura. El Gerente
además puede limpiar la sección Listo pasando sus partidas a Entregado.

El listado se refresca solo cada 2 segundos con ``st.fragment(run_every=2)``:
una orden nueva aparece sin recargar la página. Cada refresco solo lee la
base de datos; las escrituras únicamente ocurren al pulsar un botón.

Los comentarios/notas de preparación capturados por el mesero se
recuperan de ``detalle_notas``, una fila por partida.

Antes de marcar un producto como listo, Cocina/Bar puede reportarlo como
merma. Esa marca vive en ``detalle_mermas`` y el reporte se envía a la
bandeja del Gerente (Inventario > Gestión de Mermas).
"""
from __future__ import annotations

import html
from datetime import datetime

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from auth import usuario_actual
from components.merma import render_boton_mermar
from database import run_query
from permissions import (
    requerir_rol,
    requerir_accion,
    puede_accion,
    ADMIN,
    BAR,
    COCINA,
    GERENTE,
)
from services.cocina import (
    CocinaError,
    cambiar_estado_partida,
    color_por_espera,
    guardar_configuracion_kds,
    obtener_configuracion_kds,
)
from services.ventas import VentaError, limpiar_comandas_listas

ESTADOS = ("Pendiente", "En preparación", "Listo")
ESTADOS_CON_SEMAFORO = ("Pendiente", "En preparación")


def _cambiar_estado_detalle(
    id_detalle: int,
    estado_esperado: str,
    estado_nuevo: str,
) -> None:
    """Delegado al servicio de cocina: valida secuencia y concurrencia."""
    requerir_accion("comandas.preparar")
    auth = usuario_actual() or {}
    try:
        cambiar_estado_partida(
            id_detalle,
            estado_esperado,
            estado_nuevo,
            auth.get("id_usuario"),
        )
    except CocinaError as exc:
        st.error(str(exc))
    except SQLAlchemyError as exc:
        st.error(f"No se pudo cambiar el estado de la partida: {exc}")
    else:
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
    """Consulta las comandas activas, su cliente y los tiempos del KDS.

    La hora de recepción sale de la primera fila ``Pendiente`` del historial.
    Las partidas históricas del seed no tienen historial, así que se usa
    ``ordenes.fecha_orden`` como respaldo. El tiempo transcurrido se calcula
    con ``CURRENT_TIMESTAMP`` de PostgreSQL, nunca con el reloj del navegador.
    """
    return run_query(
        """
        SELECT d.id_detalle, d.id_orden, d.id_producto, d.cantidad,
               p.nombre AS producto, a.nombre AS area, ed.nombre AS estado,
               m.numero_mesa, n.comentario,
               mm.motivo AS merma_motivo,
               cl.nombre AS cliente,
               COALESCE(h.fecha_recepcion, o.fecha_orden) AS fecha_recepcion,
               h.fecha_preparacion, h.fecha_listo,
               u.nombre_usuario AS usuario_preparacion,
               EXTRACT(EPOCH FROM (
                   CURRENT_TIMESTAMP - COALESCE(h.fecha_recepcion, o.fecha_orden)
               )) / 60.0 AS minutos_espera,
               CASE
                   WHEN h.fecha_preparacion IS NOT NULL
                    AND h.fecha_listo IS NOT NULL
                   THEN EXTRACT(EPOCH FROM (
                       h.fecha_listo - h.fecha_preparacion
                   )) / 60.0
               END AS minutos_preparacion,
               (
                   SELECT string_agg(
                       CASE dm.tipo
                           WHEN 'Quitar' THEN 'SIN: ' || dm.nombre
                           WHEN 'Extra' THEN 'EXTRA: ' || dm.nombre
                           ELSE 'CAMBIO: ' || dm.nombre
                       END,
                       ' · ' ORDER BY dm.id_detalle_modificador
                   )
                   FROM detalle_modificadores dm
                   WHERE dm.id_detalle = d.id_detalle
               ) AS modificadores,
               o.numero_orden_diaria AS numero_dia
        FROM detalles_orden d
        JOIN productos p ON p.id_producto = d.id_producto
        JOIN areas_preparacion a ON a.id_area = d.id_area_preparacion
        JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
        JOIN ordenes o ON o.id_orden = d.id_orden
        JOIN estados_orden eo ON eo.id_estado = o.id_estado
        LEFT JOIN mesas m ON m.id_mesa = o.id_mesa
        LEFT JOIN clientes cl ON cl.id_cliente = o.id_cliente
        LEFT JOIN LATERAL (
            SELECT
                MIN(hh.fecha) FILTER (
                    WHERE eh.nombre = 'Pendiente'
                ) AS fecha_recepcion,
                MIN(hh.fecha) FILTER (
                    WHERE eh.nombre = 'En preparación'
                ) AS fecha_preparacion,
                MIN(hh.fecha) FILTER (
                    WHERE eh.nombre = 'Listo'
                ) AS fecha_listo,
                (
                    ARRAY_AGG(
                        hh.id_usuario
                        ORDER BY hh.fecha, hh.id_historial
                    ) FILTER (WHERE eh.nombre = 'En preparación')
                )[1] AS id_usuario_preparacion
            FROM historial_estado_detalle hh
            JOIN estados_detalle_orden eh ON eh.id_estado = hh.id_estado
            WHERE hh.id_detalle = d.id_detalle
        ) h ON TRUE
        LEFT JOIN usuarios u ON u.id_usuario = h.id_usuario_preparacion
        LEFT JOIN detalle_notas n ON n.id_detalle = d.id_detalle
        LEFT JOIN detalle_mermas mm ON mm.id_detalle = d.id_detalle
        WHERE eo.nombre <> 'Cancelada'
          AND ed.nombre IN ('Pendiente', 'En preparación', 'Listo')
        ORDER BY o.fecha_orden, d.id_detalle
        """
    )


def _render_configuracion_kds() -> None:
    """Expander de umbrales, exclusivo para Gerente/Administrador."""
    if not puede_accion("kds.configurar"):
        return

    with st.expander("⚙️ Configuración KDS: colores por tiempo de espera"):
        st.caption(
            "Verde antes del umbral amarillo, amarillo entre ambos umbrales "
            "y rojo desde el umbral rojo. Aplica a Pendiente y En preparación."
        )
        try:
            config = obtener_configuracion_kds()
        except CocinaError as exc:
            st.error(str(exc))
            return

        c1, c2 = st.columns(2)
        amarillo = c1.number_input(
            "Umbral amarillo (min)",
            min_value=1,
            step=1,
            value=int(config["umbral_amarillo_min"]),
            key="kds_umbral_amarillo",
        )
        rojo = c2.number_input(
            "Umbral rojo (min)",
            min_value=1,
            step=1,
            value=int(config["umbral_rojo_min"]),
            key="kds_umbral_rojo",
        )

        if st.button("Guardar umbrales", key="kds_guardar_umbrales"):
            requerir_accion("kds.configurar")
            try:
                guardar_configuracion_kds(int(amarillo), int(rojo))
            except CocinaError as exc:
                st.error(str(exc))
            except SQLAlchemyError as exc:
                st.error(f"No se pudo guardar la configuración: {exc}")
            else:
                st.success("Umbrales del KDS actualizados.")
                st.rerun()


@st.fragment(run_every=2)
def _render_listado(
    areas: list[str],
    solo_lectura: bool,
    es_gerente: bool,
    id_usuario: int | None,
) -> None:
    """Listado autocontenido: se vuelve a ejecutar cada 2 segundos."""
    st.caption(f"Actualizado: {datetime.now():%H:%M:%S}")

    try:
        config = obtener_configuracion_kds()
    except CocinaError as exc:
        st.error(str(exc))
        config = {"umbral_amarillo_min": 10, "umbral_rojo_min": 20}
    umbral_amarillo = int(config["umbral_amarillo_min"])
    umbral_rojo = int(config["umbral_rojo_min"])

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
                        total = limpiar_comandas_listas(id_usuario=id_usuario)
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
                    _render_tarjeta(
                        fila,
                        estado,
                        solo_lectura,
                        umbral_amarillo,
                        umbral_rojo,
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

    _render_configuracion_kds()
    _render_listado(
        areas,
        solo_lectura,
        es_gerente,
        auth.get("id_usuario"),
    )


def _texto(valor) -> str:
    """Normaliza NULL/NaN de pandas a cadena vacía."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto in ("None", "nan") else texto


def _numero(valor) -> float | None:
    """Normaliza NULL/NaN de pandas a número o None."""
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return None if numero != numero else numero


def _hora(valor) -> str:
    """Formatea un timestamp de la BD como HH:MM (cadena vacía si es NULL)."""
    if valor is None:
        return ""
    try:
        if valor != valor:  # NaN
            return ""
    except TypeError:
        pass
    try:
        return valor.strftime("%H:%M")
    except AttributeError:
        texto = str(valor)
        return texto[11:16] if len(texto) >= 16 else texto


def _render_tarjeta(
    fila,
    estado: str,
    solo_lectura: bool,
    umbral_amarillo: int,
    umbral_rojo: int,
) -> None:
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

        cliente = _texto(fila["cliente"])
        if cliente:
            st.caption(f"👤 Cliente: {cliente}")

        modificadores = _texto(fila["modificadores"])
        if modificadores:
            st.markdown(f"🧾 **Modificadores:** {modificadores}")

        comentario = _texto(fila["comentario"])
        if comentario:
            st.markdown(f"📝 **Nota de preparación:** {comentario}")

        # Hora de recepción y datos de quien tomó la partida.
        datos_tiempo = []
        hora_recepcion = _hora(fila["fecha_recepcion"])
        if hora_recepcion:
            datos_tiempo.append(f"Recibida {hora_recepcion}")

        usuario_preparacion = _texto(fila["usuario_preparacion"])
        if usuario_preparacion:
            hora_toma = _hora(fila["fecha_preparacion"])
            datos_tiempo.append(
                f"Tomada por {usuario_preparacion}"
                + (f" a las {hora_toma}" if hora_toma else "")
            )

        minutos_preparacion = _numero(fila["minutos_preparacion"])
        if minutos_preparacion is not None:
            datos_tiempo.append(
                f"Preparación: {int(round(minutos_preparacion))} min"
            )
        if datos_tiempo:
            st.caption(" · ".join(datos_tiempo))

        if mermado:
            st.markdown(
                f"<span class='tag-mermado'>MERMADO</span> "
                f"Motivo: {html.escape(merma_motivo)}",
                unsafe_allow_html=True,
            )
            st.caption("Este producto ya fue reportado. No se debe preparar ni cobrar.")
            return

        # Semáforo de espera: el texto de los minutos acompaña siempre al color.
        if estado in ESTADOS_CON_SEMAFORO:
            minutos_espera = _numero(fila["minutos_espera"])
            if minutos_espera is not None:
                minutos_mostrados = max(0, int(round(minutos_espera)))
                color = color_por_espera(
                    minutos_mostrados, umbral_amarillo, umbral_rojo
                )
                st.markdown(
                    f"<div class='kds-espera kds-{color}'>"
                    f"⏱️ {minutos_mostrados} min en espera"
                    "</div>",
                    unsafe_allow_html=True,
                )

        if solo_lectura:
            return

        id_detalle = int(fila["id_detalle"])
        if estado == "Pendiente":
            c_accion, c_merma = st.columns([1, 1])
            if c_accion.button(
                "Iniciar preparación",
                key=f"comanda_ini_{id_detalle}",
                use_container_width=True,
            ):
                _cambiar_estado_detalle(
                    id_detalle, "Pendiente", "En preparación"
                )
            with c_merma:
                render_boton_mermar(id_detalle, origen="comanda", clave="comanda")
        elif estado == "En preparación":
            c_accion, c_merma = st.columns([1, 1])
            if c_accion.button(
                "Marcar listo",
                key=f"comanda_listo_{id_detalle}",
                use_container_width=True,
            ):
                _cambiar_estado_detalle(id_detalle, "En preparación", "Listo")
            with c_merma:
                render_boton_mermar(id_detalle, origen="comanda", clave="comanda")
        elif estado == "Listo":
            if st.button(
                "Entregar",
                key=f"comanda_entrega_{id_detalle}",
                use_container_width=True,
            ):
                _cambiar_estado_detalle(id_detalle, "Listo", "Entregado")
