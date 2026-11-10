"""Módulo de Inventario: existencias por categoría, ajustes y gestión de mermas."""
from decimal import Decimal

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from auth import usuario_actual
from database import run_query, execute, transaccion
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE
from services.mermas import PATRON_DETALLE, PREFIJO_MOTIVO, TABLA_MERMA_DETALLE
from services.ventas import VentaError, recalcular_total_orden


def _es_nulo(valor) -> bool:
    """True para NULL/NaN, sin depender del tipo devuelto por pandas."""
    return valor is None or valor != valor


def _subir_merma_en(conn, id_merma: int) -> None:
    """Sube una merma: descuenta inventario y fija su costo.

    Todas las mermas (de producto/partida o capturadas directamente sobre un
    insumo) quedaron pendientes sin tocar stock al reportarse. Al subirlas, el
    Gerente confirma la pérdida y aquí se descuenta formalmente el inventario.
    """
    fila = conn.execute(
        text(
            """
            SELECT m.id_merma, m.costo_estimado_perdida,
                   mi.id_movimiento, mi.id_insumo, mi.cantidad,
                   mi.motivo, i.stock_actual, i.costo_unitario_referencia
            FROM mermas m
            JOIN movimientos_inventario mi ON mi.id_movimiento = m.id_movimiento
            JOIN insumos i ON i.id_insumo = mi.id_insumo
            WHERE m.id_merma = :id
            FOR UPDATE
            """
        ),
        {"id": id_merma},
    ).mappings().first()

    if fila is None:
        raise VentaError("El reporte de merma ya no existe.")
    if not _es_nulo(fila["costo_estimado_perdida"]):
        # Idempotencia: si ya se subió, no volver a descontar stock.
        return

    if Decimal(str(fila["stock_actual"])) < Decimal(str(fila["cantidad"])):
        raise VentaError("No hay stock suficiente para descontar la merma reportada.")

    id_tipo_merma = conn.execute(
        text(
            "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario "
            "WHERE nombre = 'Merma'"
        )
    ).scalar_one()

    conn.execute(
        text(
            "UPDATE movimientos_inventario SET id_tipo_movimiento = :tipo "
            "WHERE id_movimiento = :movimiento"
        ),
        {"tipo": int(id_tipo_merma), "movimiento": int(fila["id_movimiento"])},
    )
    conn.execute(
        text("UPDATE insumos SET stock_actual = stock_actual - :c WHERE id_insumo = :i"),
        {"c": fila["cantidad"], "i": int(fila["id_insumo"])},
    )

    costo = Decimal(str(fila["cantidad"])) * Decimal(
        str(fila["costo_unitario_referencia"] or 0)
    )
    conn.execute(
        text("UPDATE mermas SET costo_estimado_perdida = :costo WHERE id_merma = :id"),
        {"costo": costo, "id": id_merma},
    )


def _subir_mermas(ids_mermas: list[int]) -> None:
    """Sube en bloque todas las filas de un mismo reporte."""
    with transaccion() as conn:
        for id_merma in ids_mermas:
            _subir_merma_en(conn, int(id_merma))


def _rechazar_mermas(ids_mermas: list[int]) -> None:
    """Rechaza/elimina un reporte completo y restaura el total si era de producto."""
    detalles: dict[int, int | None] = {}
    ordenes: set[int] = set()

    with transaccion() as conn:
        for id_merma in ids_mermas:
            fila = conn.execute(
                text(
                    """
                    SELECT mi.id_movimiento, mi.motivo, mi.id_orden
                    FROM mermas m
                    JOIN movimientos_inventario mi ON mi.id_movimiento = m.id_movimiento
                    WHERE m.id_merma = :id
                    FOR UPDATE
                    """
                ),
                {"id": int(id_merma)},
            ).mappings().first()
            if fila is None:
                continue

            if str(fila["motivo"] or "").startswith(PREFIJO_MOTIVO):
                coincidencia = PATRON_DETALLE.search(str(fila["motivo"]))
                if coincidencia:
                    detalles[int(coincidencia.group(1))] = (
                        int(fila["id_orden"]) if fila["id_orden"] is not None else None
                    )

            conn.execute(
                text("DELETE FROM mermas WHERE id_merma = :id"), {"id": int(id_merma)}
            )
            conn.execute(
                text(
                    "DELETE FROM movimientos_inventario WHERE id_movimiento = :movimiento"
                ),
                {"movimiento": int(fila["id_movimiento"])},
            )

        # Al rechazar, la partida vuelve a ser cobrable y deja de estar tachada.
        for id_detalle, id_orden in detalles.items():
            conn.execute(
                text(
                    """
                    DELETE FROM auditoria
                    WHERE tabla_afectada = :tabla AND id_registro_afectado = :detalle
                    """
                ),
                {"tabla": TABLA_MERMA_DETALLE, "detalle": id_detalle},
            )
            if id_orden is not None:
                ordenes.add(id_orden)

        for id_orden in ordenes:
            estado_orden = conn.execute(
                text(
                    """
                    SELECT eo.nombre
                    FROM ordenes o
                    JOIN estados_orden eo ON eo.id_estado = o.id_estado
                    WHERE o.id_orden = :o
                    """
                ),
                {"o": id_orden},
            ).scalar()
            # Solo las cuentas abiertas vuelven a sumar el producto rechazado.
            if estado_orden not in (None, "Pagada", "Cancelada"):
                recalcular_total_orden(conn, id_orden)


def _separar_descripcion(descripcion: str) -> tuple[str, str]:
    """Separa 'Producto: X | Motivo: Y' para mostrarlo en la bandeja."""
    texto = str(descripcion or "").strip()
    if "| Motivo:" in texto:
        producto, motivo = texto.split("| Motivo:", 1)
        producto = producto.replace("Producto:", "").replace("Insumo:", "").strip()
        return producto or "Producto", motivo.strip()
    return "Producto", texto


def _motivo_de_descripcion(descripcion: str) -> str:
    """Extrae solo el motivo de descripciones '... | Motivo: Y'."""
    texto = str(descripcion or "").strip()
    if "| Motivo:" in texto:
        return texto.split("| Motivo:", 1)[1].strip()
    return texto


def _render_reporte_producto(filas: list) -> None:
    """Tarjeta agrupada de todos los insumos de un mismo producto mermado."""
    primera = filas[0]
    producto, motivo = _separar_descripcion(primera["motivo"])
    ids_pendientes = [
        int(fila["id_merma"])
        for fila in filas
        if _es_nulo(fila["costo_estimado_perdida"])
    ]
    clave = min(int(fila["id_merma"]) for fila in filas)

    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.markdown(f"**Producto:** {producto}")
        c1.caption("Tipo: Producto preparado · descuenta los insumos de su receta")
        c1.caption(f"Motivo: {motivo or 'Sin motivo capturado'}")
        c2.markdown(f"**Fecha:** {primera['fecha']}")
        c2.caption(f"**Empleado que reporta:** {primera['empleado_reporta']}")
        c2.caption(f"**Usuario del sistema:** {primera['usuario_registro']}")
        c3.markdown("**Insumos afectados**")
        for fila in filas:
            c3.caption(
                f"• {fila['insumo']}: {float(fila['cantidad']):.3f} {fila['unidad']}"
            )

        if ids_pendientes:
            b1, b2 = st.columns(2)
            if b1.button(
                "⬆️ Subir merma",
                key=f"subir_grupo_{clave}",
                use_container_width=True,
            ):
                requerir_accion("mermas.gestionar")
                try:
                    _subir_mermas(ids_pendientes)
                except (VentaError, SQLAlchemyError) as exc:
                    st.error(f"No se pudo subir la merma: {exc}")
                else:
                    st.rerun()
            if b2.button(
                "🗑️ Eliminar",
                key=f"eliminar_grupo_{clave}",
                use_container_width=True,
            ):
                requerir_accion("mermas.gestionar")
                try:
                    _rechazar_mermas(ids_pendientes)
                except (VentaError, SQLAlchemyError) as exc:
                    st.error(f"No se pudo eliminar la merma: {exc}")
                else:
                    st.rerun()
        else:
            st.caption("✔ Merma subida: stock descontado y costo registrado.")


def _render_reporte_individual(reporte) -> None:
    """Tarjeta de una merma capturada directamente sobre un insumo."""
    id_merma = int(reporte["id_merma"])
    pendiente = _es_nulo(reporte["costo_estimado_perdida"])

    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.markdown(f"**Insumo:** {reporte['insumo']}")
        c1.caption("Tipo: Insumo base · descuenta directo del inventario")
        c1.caption(f"Cantidad: {float(reporte['cantidad']):.3f} {reporte['unidad']}")
        c2.markdown(f"**Fecha:** {reporte['fecha']}")
        c2.caption(f"**Empleado que reporta:** {reporte['empleado_reporta']}")
        c2.caption(f"**Usuario del sistema:** {reporte['usuario_registro']}")
        c3.markdown(f"**Motivo:** {_motivo_de_descripcion(reporte['motivo'])}")

        if pendiente:
            b1, b2 = st.columns(2)
            if b1.button(
                "⬆️ Subir merma",
                key=f"subir_merma_{id_merma}",
                use_container_width=True,
            ):
                requerir_accion("mermas.gestionar")
                try:
                    _subir_mermas([id_merma])
                except (VentaError, SQLAlchemyError) as exc:
                    st.error(f"No se pudo subir la merma: {exc}")
                else:
                    st.rerun()
            if b2.button(
                "🗑️ Eliminar",
                key=f"eliminar_merma_{id_merma}",
                use_container_width=True,
            ):
                requerir_accion("mermas.gestionar")
                try:
                    _rechazar_mermas([id_merma])
                except (VentaError, SQLAlchemyError) as exc:
                    st.error(f"No se pudo eliminar la merma: {exc}")
                else:
                    st.rerun()
        else:
            st.caption("✔ Merma subida: el stock ya fue descontado.")


def _render_gestion_mermas() -> None:
    requerir_accion("mermas.gestionar")
    # Convención sin cambiar el esquema: un reporte está pendiente mientras
    # mermas.costo_estimado_perdida sea NULL. Al subirlo se calcula el costo
    # y se descuenta formalmente el stock del insumo.
    reportes = run_query(
        """
        SELECT m.id_merma, i.nombre AS insumo, mi.fecha_movimiento AS fecha,
               CONCAT(e.nombre, ' ', e.apellido) AS empleado_reporta,
               u.nombre_usuario AS usuario_registro,
               m.descripcion AS motivo, mi.motivo AS movimiento_motivo,
               mi.cantidad, um.abreviatura AS unidad,
               m.costo_estimado_perdida
        FROM mermas m
        JOIN movimientos_inventario mi ON mi.id_movimiento = m.id_movimiento
        JOIN insumos i ON i.id_insumo = mi.id_insumo
        JOIN unidades_medida um ON um.id_unidad = i.id_unidad_medida
        JOIN empleados e ON e.id_empleado = mi.id_empleado
        JOIN usuarios u ON u.id_usuario = m.id_usuario_registro
        ORDER BY mi.fecha_movimiento DESC, m.id_merma DESC
        """
    )

    if reportes.empty:
        st.info("No hay reportes de mermas registrados.")
        return

    # Los reportes hechos desde Comandas/Órdenes generan una fila por insumo.
    # Se agrupan por movimiento para mostrarlos como un solo reporte.
    grupos: dict[tuple[str, object], list] = {}
    for _, reporte in reportes.iterrows():
        movimiento = str(reporte["movimiento_motivo"] or "")
        if movimiento.startswith(PREFIJO_MOTIVO):
            clave: tuple[str, object] = ("producto", movimiento)
        else:
            clave = ("individual", int(reporte["id_merma"]))
        grupos.setdefault(clave, []).append(reporte)

    pendientes = sum(
        1
        for filas in grupos.values()
        if any(_es_nulo(fila["costo_estimado_perdida"]) for fila in filas)
    )
    if pendientes:
        st.warning(
            f"Hay {pendientes} reporte(s) de merma pendiente(s) de autorización."
        )

    for (tipo, _), filas in grupos.items():
        if tipo == "producto":
            _render_reporte_producto(filas)
        else:
            _render_reporte_individual(filas[0])


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("📦 Inventario")

    tab_stock, tab_ajuste, tab_mermas = st.tabs(
        ["Existencias", "Registrar ajuste", "Gestión de Mermas"]
    )

    with tab_stock:
        insumos = run_query(
            """
            SELECT i.id_insumo, i.nombre, c.nombre AS categoria, u.abreviatura AS unidad,
                   i.stock_actual, i.stock_minimo,
                   (i.stock_actual <= i.stock_minimo) AS stock_bajo
            FROM insumos i
            JOIN categorias_insumos c ON c.id_categoria = i.id_categoria
            JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
            WHERE i.activo = TRUE
            ORDER BY c.nombre, i.nombre
            """
        )
        if insumos.empty:
            st.info("No hay insumos registrados todavía.")
        else:
            bajos = insumos[insumos["stock_bajo"] == 1]
            if not bajos.empty:
                st.warning(f"⚠️ {len(bajos)} insumo(s) por debajo del stock mínimo.")

            categorias = list(dict.fromkeys(insumos["categoria"]))
            for tab, categoria in zip(st.tabs(categorias), categorias):
                with tab:
                    subset = insumos[insumos["categoria"] == categoria]
                    st.dataframe(
                        subset.drop(columns=["stock_bajo"]),
                        use_container_width=True,
                        hide_index=True,
                    )

    with tab_ajuste:
        insumos_sel = run_query("SELECT id_insumo, nombre FROM insumos WHERE activo = TRUE ORDER BY nombre")
        if insumos_sel.empty:
            st.info("No hay insumos registrados todavía.")
            return

        with st.form("ajuste_inventario"):
            insumo = st.selectbox("Insumo", insumos_sel.to_dict("records"), format_func=lambda i: i["nombre"])
            tipo = st.radio("Tipo de ajuste", ["Ajuste positivo (entrada)", "Ajuste negativo (salida)"])
            cantidad = st.number_input("Cantidad", min_value=0.001, step=0.1, format="%.3f")
            motivo = st.text_input("Motivo (ej. conteo físico, corrección de captura)")
            enviado = st.form_submit_button("Registrar movimiento")

        if enviado:
            requerir_accion("inventario.gestionar")
            nombre_tipo = "Ajuste positivo" if tipo.startswith("Ajuste positivo") else "Ajuste negativo"
            id_tipo = run_query(
                "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario WHERE nombre = :n", {"n": nombre_tipo}
            ).iloc[0]["id_tipo_movimiento"]

            auth = usuario_actual()
            if auth["id_empleado"] is None:
                st.error(
                    "Tu cuenta de usuario no está vinculada a una ficha de empleado, "
                    "así que no puede registrar movimientos de inventario. Pide a un "
                    "administrador que la vincule en el módulo Usuarios."
                )
                st.stop()
            execute(
                """
                INSERT INTO movimientos_inventario (id_insumo, id_tipo_movimiento, cantidad, motivo, id_empleado)
                VALUES (:i, :t, :c, :m, :e)
                """,
                {"i": int(insumo["id_insumo"]), "t": int(id_tipo), "c": cantidad,
                 "m": motivo or "Ajuste manual", "e": auth["id_empleado"]},
            )
            signo = 1 if nombre_tipo == "Ajuste positivo" else -1
            execute(
                "UPDATE insumos SET stock_actual = stock_actual + :c WHERE id_insumo = :i",
                {"c": signo * cantidad, "i": int(insumo["id_insumo"])},
            )
            st.success("Movimiento registrado y stock actualizado.")
            st.rerun()

    with tab_mermas:
        _render_gestion_mermas()
