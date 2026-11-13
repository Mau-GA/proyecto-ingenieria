"""Módulo de Inventario: stock crítico, insumos, existencias, ajustes y mermas."""
import json
from decimal import Decimal

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from auth import usuario_actual
from database import run_query, execute, transaccion
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE
from services.mermas import (
    PATRON_DETALLE,
    PREFIJO_MOTIVO,
    TABLA_MERMA_DETALLE,
    TABLA_MERMA_PENDIENTE,
    TABLA_REPOSICION,
)
from services.ventas import VentaError, recalcular_total_orden


def _es_nulo(valor) -> bool:
    """True para NULL/NaN, sin depender del tipo devuelto por pandas."""
    return valor is None or valor != valor


def _registrar_ajuste(
    *,
    id_insumo: int,
    tipo: str,
    cantidad,
    motivo: str,
    id_empleado: int,
) -> None:
    """Aplica un ajuste de inventario atómico, validando stock al restar."""
    nombre_tipo = (
        "Ajuste positivo" if str(tipo).startswith("Ajuste positivo")
        else "Ajuste negativo"
    )
    cantidad_dec = Decimal(str(cantidad))
    if cantidad_dec <= 0:
        raise VentaError("La cantidad del ajuste debe ser mayor que cero.")
    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT i.stock_actual, u.abreviatura AS unidad
                FROM insumos i
                JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
                WHERE i.id_insumo = :insumo
                FOR UPDATE OF i
                """
            ),
            {"insumo": int(id_insumo)},
        ).mappings().first()
        if fila is None:
            raise VentaError("El insumo seleccionado ya no existe.")

        disponible = Decimal(str(fila["stock_actual"]))
        if nombre_tipo == "Ajuste negativo" and disponible < cantidad_dec:
            raise VentaError(
                "No se puede aplicar el ajuste negativo: stock "
                f"disponible {disponible:.3f} {fila['unidad']}."
            )

        id_tipo = conn.execute(
            text(
                "SELECT id_tipo_movimiento FROM "
                "tipos_movimiento_inventario WHERE nombre = :n"
            ),
            {"n": nombre_tipo},
        ).scalar_one()

        conn.execute(
            text(
                """
                INSERT INTO movimientos_inventario
                    (id_insumo, id_tipo_movimiento, cantidad,
                     motivo, id_empleado)
                VALUES (:i, :t, :c, :m, :e)
                """
            ),
            {
                "i": int(id_insumo),
                "t": int(id_tipo),
                "c": cantidad_dec,
                "m": motivo or "Ajuste manual",
                "e": int(id_empleado),
            },
        )
        signo = 1 if nombre_tipo == "Ajuste positivo" else -1
        conn.execute(
            text(
                "UPDATE insumos SET stock_actual = stock_actual + :c "
                "WHERE id_insumo = :i"
            ),
            {"c": signo * cantidad_dec, "i": int(id_insumo)},
        )


def _datos_json(valor) -> dict:
    """Normaliza el JSONB de auditoría a dict."""
    if isinstance(valor, dict):
        return valor
    if not valor:
        return {}
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return {}


def _subir_merma_en(conn, id_merma: int) -> None:
    """Confirma una merma pendiente.

    - Partida ya vendida: solo registra el costo; la venta ya descontó stock.
    - Captura manual de producto/insumo: descuenta stock en este momento.
    """
    fila = conn.execute(
        text(
            """
            SELECT m.id_merma, m.costo_estimado_perdida, m.id_movimiento,
                   a.datos_nuevos AS pendiente
            FROM mermas m
            LEFT JOIN LATERAL (
                SELECT datos_nuevos
                FROM auditoria
                WHERE tabla_afectada = :tabla
                  AND id_registro_afectado = m.id_merma
                ORDER BY id_auditoria DESC
                LIMIT 1
            ) a ON TRUE
            WHERE m.id_merma = :id
            FOR UPDATE OF m
            """
        ),
        {"id": id_merma, "tabla": TABLA_MERMA_PENDIENTE},
    ).mappings().first()

    if fila is None:
        raise VentaError("El reporte de merma ya no existe.")
    if not _es_nulo(fila["costo_estimado_perdida"]):
        # Idempotencia: si ya se subió, no volver a descontar stock.
        return

    datos = _datos_json(fila["pendiente"])
    if not datos:
        # Compatibilidad con reportes antiguos que sí creaban el movimiento.
        movimiento = conn.execute(
            text(
                """
                SELECT mi.id_movimiento, mi.id_insumo, mi.cantidad,
                       i.stock_actual, i.costo_unitario_referencia
                FROM mermas m
                JOIN movimientos_inventario mi
                  ON mi.id_movimiento = m.id_movimiento
                JOIN insumos i ON i.id_insumo = mi.id_insumo
                WHERE m.id_merma = :id
                FOR UPDATE OF i
                """
            ),
            {"id": id_merma},
        ).mappings().first()
        if movimiento is None:
            raise VentaError("El reporte de merma no tiene datos pendientes.")
        if Decimal(str(movimiento["stock_actual"])) < Decimal(
            str(movimiento["cantidad"])
        ):
            raise VentaError(
                "No hay stock suficiente para descontar la merma reportada."
            )
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
            {
                "tipo": int(id_tipo_merma),
                "movimiento": int(movimiento["id_movimiento"]),
            },
        )
        conn.execute(
            text(
                "UPDATE insumos SET stock_actual = stock_actual - :c "
                "WHERE id_insumo = :i"
            ),
            {
                "c": movimiento["cantidad"],
                "i": int(movimiento["id_insumo"]),
            },
        )
        costo = Decimal(str(movimiento["cantidad"])) * Decimal(
            str(movimiento["costo_unitario_referencia"] or 0)
        )
        conn.execute(
            text(
                "UPDATE mermas SET costo_estimado_perdida = :costo "
                "WHERE id_merma = :id"
            ),
            {"costo": costo, "id": id_merma},
        )
        return

    origen = str(
        datos.get("origen")
        or ("partida" if datos.get("id_detalle") else "insumo")
    )
    id_insumo = int(datos["id_insumo"])
    cantidad = Decimal(str(datos["cantidad"]))
    id_empleado = int(datos["id_empleado"])
    producto = str(datos.get("producto") or "Producto")
    motivo = str(datos.get("motivo") or "Sin motivo capturado")
    id_orden = datos.get("id_orden")

    insumo = conn.execute(
        text(
            """
            SELECT stock_actual, costo_unitario_referencia
            FROM insumos
            WHERE id_insumo = :insumo
            FOR UPDATE
            """
        ),
        {"insumo": id_insumo},
    ).mappings().first()
    if insumo is None:
        raise VentaError("El insumo de la merma ya no existe.")

    id_movimiento = None
    if origen != "partida":
        if Decimal(str(insumo["stock_actual"])) < cantidad:
            raise VentaError(
                "No hay stock suficiente para descontar la merma reportada."
            )
        id_tipo_merma = conn.execute(
            text(
                "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario "
                "WHERE nombre = 'Merma'"
            )
        ).scalar_one()
        id_movimiento = conn.execute(
            text(
                """
                INSERT INTO movimientos_inventario
                    (id_insumo, id_tipo_movimiento, cantidad, motivo,
                     id_empleado, id_orden)
                VALUES (:insumo, :tipo, :cantidad, :motivo, :empleado, :orden)
                RETURNING id_movimiento
                """
            ),
            {
                "insumo": id_insumo,
                "tipo": int(id_tipo_merma),
                "cantidad": cantidad,
                "motivo": f"Merma confirmada | {producto} | {motivo}"[:150],
                "empleado": id_empleado,
                "orden": id_orden,
            },
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE insumos SET stock_actual = stock_actual - :c "
                "WHERE id_insumo = :i"
            ),
            {"c": cantidad, "i": id_insumo},
        )

    costo = cantidad * Decimal(
        str(insumo["costo_unitario_referencia"] or 0)
    )
    conn.execute(
        text(
            "UPDATE mermas SET id_movimiento = :movimiento, "
            "costo_estimado_perdida = :costo WHERE id_merma = :id"
        ),
        {
            "movimiento": int(id_movimiento) if id_movimiento is not None else None,
            "costo": costo,
            "id": id_merma,
        },
    )


def _subir_mermas(ids_mermas: list[int]) -> None:
    """Sube en bloque todas las filas de un mismo reporte."""
    with transaccion() as conn:
        for id_merma in ids_mermas:
            _subir_merma_en(conn, int(id_merma))


def _rechazar_mermas(ids_mermas: list[int]) -> None:
    """Rechaza/elimina un reporte pendiente y restaura el total si era partida."""
    detalles: dict[int, int | None] = {}
    ordenes: set[int] = set()

    with transaccion() as conn:
        for id_merma in ids_mermas:
            fila = conn.execute(
                text(
                    """
                    SELECT m.id_merma, m.costo_estimado_perdida,
                           m.id_movimiento,
                           a.datos_nuevos AS pendiente
                    FROM mermas m
                    LEFT JOIN LATERAL (
                        SELECT datos_nuevos
                        FROM auditoria
                        WHERE tabla_afectada = :tabla
                          AND id_registro_afectado = m.id_merma
                        ORDER BY id_auditoria DESC
                        LIMIT 1
                    ) a ON TRUE
                    WHERE m.id_merma = :id
                    FOR UPDATE OF m
                    """
                ),
                {"id": int(id_merma), "tabla": TABLA_MERMA_PENDIENTE},
            ).mappings().first()
            if fila is None:
                continue
            if not _es_nulo(fila["costo_estimado_perdida"]):
                raise VentaError(
                    "Esta merma ya fue subida; no se puede rechazar."
                )

            datos = _datos_json(fila["pendiente"])
            if datos:
                id_detalle = datos.get("id_detalle")
                if id_detalle is not None:
                    id_detalle = int(id_detalle)
                    reposicion = conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM auditoria
                            WHERE tabla_afectada = :tabla
                              AND id_registro_afectado = :detalle
                            LIMIT 1
                            """
                        ),
                        {
                            "tabla": TABLA_REPOSICION,
                            "detalle": id_detalle,
                        },
                    ).scalar()
                    if reposicion:
                        raise VentaError(
                            "Esta merma ya tiene reposición; no se puede "
                            "rechazar."
                        )
                    id_orden = datos.get("id_orden")
                    detalles[id_detalle] = (
                        int(id_orden) if id_orden is not None else None
                    )
                conn.execute(
                    text(
                        "DELETE FROM auditoria WHERE tabla_afectada = :tabla "
                        "AND id_registro_afectado = :merma"
                    ),
                    {
                        "tabla": TABLA_MERMA_PENDIENTE,
                        "merma": int(id_merma),
                    },
                )
                conn.execute(
                    text("DELETE FROM mermas WHERE id_merma = :id"),
                    {"id": int(id_merma)},
                )
                continue

            # Compatibilidad con reportes antiguos que tenían movimiento.
            movimiento = None
            if fila["id_movimiento"] is not None:
                movimiento = conn.execute(
                    text(
                        """
                        SELECT id_movimiento, motivo, id_orden
                        FROM movimientos_inventario
                        WHERE id_movimiento = :movimiento
                        FOR UPDATE
                        """
                    ),
                    {"movimiento": int(fila["id_movimiento"])},
                ).mappings().first()
            if movimiento is not None and str(
                movimiento["motivo"] or ""
            ).startswith(PREFIJO_MOTIVO):
                coincidencia = PATRON_DETALLE.search(str(movimiento["motivo"]))
                if coincidencia:
                    detalles[int(coincidencia.group(1))] = (
                        int(movimiento["id_orden"])
                        if movimiento["id_orden"] is not None
                        else None
                    )

            conn.execute(
                text("DELETE FROM mermas WHERE id_merma = :id"),
                {"id": int(id_merma)},
            )
            if movimiento is not None:
                conn.execute(
                    text(
                        "DELETE FROM movimientos_inventario "
                        "WHERE id_movimiento = :movimiento"
                    ),
                    {"movimiento": int(movimiento["id_movimiento"])},
                )

        # Al rechazar, la partida vuelve a ser cobrable y deja de estar tachada.
        for id_detalle, id_orden in detalles.items():
            conn.execute(
                text(
                    """
                    DELETE FROM auditoria
                    WHERE tabla_afectada = :tabla
                      AND id_registro_afectado = :detalle
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
        tipo_origen = str(primera["tipo_origen"])
        c1.markdown(f"**Producto:** {producto}")
        if tipo_origen == "partida":
            c1.caption(
                "Tipo: Producto preparado · partida ya vendida: al subir solo "
                "se registra el costo (no descuenta stock adicional)"
            )
        else:
            c1.caption(
                "Tipo: Producto preparado · captura manual: al subir descuenta "
                "los insumos de su receta"
            )
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
            if tipo_origen == "partida":
                st.caption(
                    "✔ Merma subida: costo registrado; el stock ya se había "
                    "descontado al vender la partida."
                )
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


def _unidad_obligatoria(nombre: str) -> bool:
    """True para insumos que deben manejarse siempre por pieza."""
    texto = str(nombre or "").casefold()
    return any(clave in texto for clave in ("alita", "coca", "refresco", "soda"))


def _render_stock_critico() -> None:
    """Vista concentrada de insumos activos en o bajo su punto de reorden."""
    criticos = run_query(
        """
        SELECT i.nombre AS insumo, c.nombre AS categoria,
               u.abreviatura AS unidad, i.stock_actual, i.stock_minimo,
               (i.stock_minimo - i.stock_actual) AS faltante
        FROM insumos i
        JOIN categorias_insumos c ON c.id_categoria = i.id_categoria
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE i.activo = TRUE
          AND i.stock_actual <= i.stock_minimo
        ORDER BY (i.stock_actual - i.stock_minimo), i.nombre
        """
    )
    st.caption(
        "Insumos activos cuyo stock actual es menor o igual a su stock mínimo. "
        "Ordenados por faltante para priorizar las compras."
    )
    if criticos.empty:
        st.success("Ningún insumo está en stock crítico.")
        return

    st.error(f"{len(criticos)} insumo(s) requieren reposición.")
    st.dataframe(
        criticos.rename(
            columns={
                "insumo": "Insumo",
                "categoria": "Categoría",
                "unidad": "Unidad",
                "stock_actual": "Stock actual",
                "stock_minimo": "Stock mínimo",
                "faltante": "Faltante",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )


def _render_catalogo_insumos() -> None:
    """Alta y edición de insumos, forzando piezas en alitas y refrescos."""
    requerir_accion("inventario.gestionar")
    insumos = run_query(
        """
        SELECT i.id_insumo, i.nombre, i.descripcion, i.id_categoria,
               i.id_unidad_medida, c.nombre AS categoria,
               u.nombre AS unidad, u.abreviatura, i.stock_actual,
               i.stock_minimo, i.costo_unitario_referencia, i.activo
        FROM insumos i
        JOIN categorias_insumos c ON c.id_categoria = i.id_categoria
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        ORDER BY i.nombre
        """
    )
    categorias = run_query(
        "SELECT id_categoria, nombre FROM categorias_insumos ORDER BY nombre"
    ).to_dict("records")
    unidades = run_query(
        "SELECT id_unidad, nombre, abreviatura FROM unidades_medida ORDER BY id_unidad"
    ).to_dict("records")
    if not categorias or not unidades:
        st.warning(
            "Registra al menos una categoría de insumo y una unidad de medida."
        )
        return

    pieza = next(
        (
            unidad
            for unidad in unidades
            if str(unidad["abreviatura"]).casefold() == "pza"
        ),
        None,
    )
    if pieza is None:
        st.error("Falta la unidad de medida 'Pieza' (pza) en el catálogo.")
        return

    if not insumos.empty:
        st.dataframe(
            insumos.drop(
                columns=["id_insumo", "id_categoria", "id_unidad_medida"]
            ),
            use_container_width=True,
            hide_index=True,
        )

    opciones = [{"id_insumo": 0, "nombre": "➕ Nuevo insumo"}] + (
        insumos[["id_insumo", "nombre"]].to_dict("records")
        if not insumos.empty
        else []
    )
    seleccion = st.selectbox(
        "Insumo a editar",
        opciones,
        format_func=lambda insumo: insumo["nombre"],
    )
    id_seleccionado = int(seleccion["id_insumo"])
    actual = (
        insumos[insumos["id_insumo"] == id_seleccionado].iloc[0].to_dict()
        if id_seleccionado
        else None
    )

    indice_categoria = next(
        (
            indice
            for indice, categoria in enumerate(categorias)
            if actual
            and int(categoria["id_categoria"]) == int(actual["id_categoria"])
        ),
        0,
    )
    indice_unidad = next(
        (
            indice
            for indice, unidad in enumerate(unidades)
            if actual
            and int(unidad["id_unidad"]) == int(actual["id_unidad_medida"])
        ),
        0,
    )

    st.caption(
        "Los insumos de alitas, coca, refrescos o soda se guardan siempre "
        "con unidad Pieza (pza), aunque se elija otra."
    )
    with st.form("form_insumo", clear_on_submit=True):
        nombre = st.text_input(
            "Nombre",
            value=str(actual["nombre"]) if actual else "",
        )
        descripcion = st.text_input(
            "Descripción",
            value=str(actual["descripcion"] or "") if actual else "",
        )
        categoria = st.selectbox(
            "Categoría",
            categorias,
            index=indice_categoria,
            format_func=lambda item: item["nombre"],
        )
        unidad = st.selectbox(
            "Unidad de medida",
            unidades,
            index=(
                next(
                    (
                        indice
                        for indice, item in enumerate(unidades)
                        if int(item["id_unidad"]) == int(pieza["id_unidad"])
                    ),
                    0,
                )
                if _unidad_obligatoria(
                    str(actual["nombre"]) if actual else ""
                )
                else indice_unidad
            ),
            format_func=lambda item: (
                f"{item['nombre']} ({item['abreviatura']})"
            ),
        )
        stock_minimo = st.number_input(
            "Stock mínimo",
            min_value=0.0,
            value=float(actual["stock_minimo"]) if actual else 0.0,
            step=0.1,
            format="%.3f",
        )
        costo = st.number_input(
            "Costo unitario de referencia",
            min_value=0.0,
            value=(
                float(actual["costo_unitario_referencia"] or 0)
                if actual
                else 0.0
            ),
            step=1.0,
            format="%.2f",
        )
        if actual:
            st.caption(
                f"Stock actual: {float(actual['stock_actual']):.3f} "
                f"{actual['abreviatura']}. Usa 'Registrar ajuste' para "
                "modificarlo."
            )
        else:
            st.caption(
                "El stock inicial se carga después desde 'Registrar ajuste'."
            )
        activo = st.checkbox(
            "Activo",
            value=bool(actual["activo"]) if actual else True,
        )
        enviado = st.form_submit_button("Guardar insumo")

    if not enviado:
        return

    requerir_accion("inventario.gestionar")
    if not nombre.strip():
        st.error("El nombre del insumo es obligatorio.")
        return

    id_unidad = int(unidad["id_unidad"])
    if _unidad_obligatoria(nombre):
        id_unidad = int(pieza["id_unidad"])

    if actual and id_unidad != int(actual["id_unidad_medida"]):
        dependencias = run_query(
            """
            SELECT
                (SELECT COUNT(*) FROM recetas WHERE id_insumo = :id) AS recetas,
                (SELECT COUNT(*) FROM movimientos_inventario
                 WHERE id_insumo = :id) AS movimientos
            """,
            {"id": id_seleccionado},
        ).iloc[0]
        if int(dependencias["recetas"]) or int(dependencias["movimientos"]):
            st.error(
                "No se puede cambiar la unidad de un insumo que ya tiene "
                "recetas o movimientos. Usa el script de conversión o ajusta "
                "primero sus recetas e historial."
            )
            return

    parametros = {
        "n": nombre.strip(),
        "d": descripcion.strip() or None,
        "c": int(categoria["id_categoria"]),
        "u": id_unidad,
        "sm": stock_minimo,
        "costo": costo,
        "a": activo,
    }
    try:
        if actual:
            execute(
                """
                UPDATE insumos
                SET nombre = :n, descripcion = :d, id_categoria = :c,
                    id_unidad_medida = :u, stock_minimo = :sm,
                    costo_unitario_referencia = :costo, activo = :a
                WHERE id_insumo = :id
                """,
                {**parametros, "id": id_seleccionado},
            )
            st.success(f"Insumo '{nombre.strip()}' actualizado.")
        else:
            execute(
                """
                INSERT INTO insumos
                    (nombre, descripcion, id_categoria, id_unidad_medida,
                     stock_actual, stock_minimo, costo_unitario_referencia,
                     activo)
                VALUES (:n, :d, :c, :u, 0, :sm, :costo, :a)
                """,
                parametros,
            )
            st.success(f"Insumo '{nombre.strip()}' creado.")
    except SQLAlchemyError as exc:
        st.error(f"No se pudo guardar el insumo: {exc}")
        return
    st.rerun()


def _render_gestion_mermas() -> None:
    requerir_accion("mermas.gestionar")
    # Convención sin cambiar el esquema: un reporte está pendiente mientras
    # mermas.costo_estimado_perdida sea NULL. Al subirlo se calcula el costo
    # y se descuenta formalmente el stock del insumo.
    reportes = run_query(
        """
        SELECT m.id_merma, i.nombre AS insumo,
               COALESCE(mi.fecha_movimiento, m.fecha_deteccion) AS fecha,
               COALESCE(CONCAT(e.nombre, ' ', e.apellido), '—') AS empleado_reporta,
               u.nombre_usuario AS usuario_registro,
               m.descripcion AS motivo, mi.motivo AS movimiento_motivo,
               COALESCE(
                   mi.cantidad,
                   (a.datos_nuevos ->> 'cantidad')::numeric
               ) AS cantidad,
               um.abreviatura AS unidad,
               a.datos_nuevos ->> 'reporte' AS reporte,
               COALESCE(a.datos_nuevos ->> 'origen', 'insumo') AS tipo_origen,
               m.costo_estimado_perdida
        FROM mermas m
        LEFT JOIN LATERAL (
            SELECT datos_nuevos
            FROM auditoria
            WHERE tabla_afectada = :tabla
              AND id_registro_afectado = m.id_merma
            ORDER BY id_auditoria DESC
            LIMIT 1
        ) a ON TRUE
        LEFT JOIN movimientos_inventario mi
          ON mi.id_movimiento = m.id_movimiento
        JOIN insumos i ON i.id_insumo = COALESCE(
            mi.id_insumo,
            (a.datos_nuevos ->> 'id_insumo')::int
        )
        JOIN unidades_medida um ON um.id_unidad = i.id_unidad_medida
        LEFT JOIN empleados e ON e.id_empleado = COALESCE(
            mi.id_empleado,
            m.id_empleado_responsable,
            (a.datos_nuevos ->> 'id_empleado')::int
        )
        JOIN usuarios u ON u.id_usuario = m.id_usuario_registro
        ORDER BY fecha DESC, m.id_merma DESC
        """,
        {"tabla": TABLA_MERMA_PENDIENTE},
    )

    if reportes.empty:
        st.info("No hay reportes de mermas registrados.")
        return

    # Cada reporte genera una fila por insumo. El token de auditoría permite
    # agruparlos; los datos antiguos se agrupan por movimiento o individualmente.
    grupos: dict[tuple[str, object], list] = {}
    for _, reporte in reportes.iterrows():
        token = reporte["reporte"]
        movimiento = str(reporte["movimiento_motivo"] or "")
        if not _es_nulo(token) and str(token).strip():
            clave: tuple[str, object] = ("reporte", str(token))
        elif movimiento.startswith(PREFIJO_MOTIVO):
            clave = ("producto", movimiento)
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

    for filas in grupos.values():
        primera = filas[0]
        es_producto = str(primera["tipo_origen"]) in (
            "partida",
            "producto",
        ) or str(primera["movimiento_motivo"] or "").startswith(PREFIJO_MOTIVO)
        if es_producto:
            _render_reporte_producto(filas)
        else:
            _render_reporte_individual(primera)


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("📦 Inventario")

    tab_critico, tab_stock, tab_insumos, tab_ajuste, tab_mermas = st.tabs(
        [
            "🚨 Stock Crítico",
            "Existencias",
            "Insumos",
            "Registrar ajuste",
            "Gestión de Mermas",
        ]
    )

    with tab_critico:
        _render_stock_critico()

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

    with tab_insumos:
        _render_catalogo_insumos()

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

            auth = usuario_actual()
            if auth["id_empleado"] is None:
                st.error(
                    "Tu cuenta de usuario no está vinculada a una ficha de empleado, "
                    "así que no puede registrar movimientos de inventario. Pide a un "
                    "administrador que la vincule en el módulo Usuarios."
                )
                st.stop()

            try:
                _registrar_ajuste(
                    id_insumo=int(insumo["id_insumo"]),
                    tipo=tipo,
                    cantidad=cantidad,
                    motivo=motivo,
                    id_empleado=int(auth["id_empleado"]),
                )
            except (VentaError, SQLAlchemyError) as exc:
                st.error(f"No se pudo registrar el ajuste: {exc}")
                return

            st.success("Movimiento registrado y stock actualizado.")
            st.rerun()

    with tab_mermas:
        _render_gestion_mermas()
