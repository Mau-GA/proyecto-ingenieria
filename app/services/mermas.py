"""Lógica de negocio de mermas reportadas por el personal.

Una partida de ``detalles_orden`` se marca como mermada en la bitácora
``auditoria`` (``tabla_afectada = 'detalles_orden_merma'``). Esa marca es la
que consultan Comandas y Órdenes para tachar el producto y la que usa el
recalculo de totales para no cobrarlo.

Para que el Gerente vea el reporte en Inventario > Gestión de Mermas se crea,
en la misma transacción, un movimiento de inventario tipo ``Merma`` por cada
insumo de la receta y su fila correspondiente en ``mermas``. El stock NO se
descuenta al reportar: queda pendiente hasta que el Gerente pulsa
``Subir merma``, que confirma el costo y descuenta formalmente el inventario.

También existen capturas generales sin una orden de por medio:

- ``registrar_merma_producto``: parte de un producto del menú y genera un
  movimiento por cada insumo de su receta.
- ``registrar_merma_insumo``: afecta directamente un insumo base (kg, l, pza).

Ambas quedan pendientes hasta que el Gerente las suba o elimine desde
Inventario > Gestión de Mermas.
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import transaccion
from services.ventas import VentaError, recalcular_total_orden

TABLA_MERMA_DETALLE = "detalles_orden_merma"
PREFIJO_MOTIVO = "Merma producto"
PREFIJO_INSUMO = "Merma insumo"
PATRON_DETALLE = re.compile(r"detalle#(\d+)", re.IGNORECASE)


class MermaError(VentaError):
    """Error de negocio al registrar o consultar una merma."""


def _truncar(texto: str, limite: int) -> str:
    texto = str(texto or "")
    if len(texto) <= limite:
        return texto
    return texto[: max(limite - 1, 0)].rstrip() + "…"


def _id_tipo_movimiento_merma(conn: Connection) -> int:
    id_tipo = conn.execute(
        text(
            "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario "
            "WHERE nombre = 'Merma'"
        )
    ).scalar_one_or_none()
    if id_tipo is None:
        raise MermaError("Falta el tipo de movimiento 'Merma' en el catálogo.")
    return int(id_tipo)


def _id_tipo_merma(conn: Connection, nombre: str) -> int:
    id_tipo = conn.execute(
        text("SELECT id_tipo_merma FROM tipos_merma WHERE nombre = :n"),
        {"n": nombre},
    ).scalar_one_or_none()
    if id_tipo is None:
        id_tipo = conn.execute(
            text("SELECT id_tipo_merma FROM tipos_merma WHERE nombre = 'Otro'")
        ).scalar_one_or_none()
    if id_tipo is None:
        raise MermaError("Faltan los tipos de merma en el catálogo.")
    return int(id_tipo)


def _crear_mermas_de_receta(
    conn: Connection,
    *,
    id_producto: int,
    producto: str,
    cantidad_unidades: int,
    motivo: str,
    id_usuario: int,
    id_empleado: int,
    nombre_tipo_merma: str,
    id_orden: int | None = None,
    id_detalle: int | None = None,
) -> list[int]:
    """Crea un movimiento + merma pendiente por cada insumo de la receta."""
    receta = conn.execute(
        text(
            """
            SELECT r.id_insumo, i.nombre AS insumo, r.cantidad_usada,
                   i.costo_unitario_referencia
            FROM recetas r
            JOIN insumos i ON i.id_insumo = r.id_insumo
            WHERE r.id_producto = :producto
            ORDER BY r.id_insumo
            """
        ),
        {"producto": int(id_producto)},
    ).mappings().all()
    if not receta:
        raise MermaError(
            f"El producto «{producto}» no tiene receta registrada; "
            "no se puede descontar inventario. Agrega su receta primero."
        )

    id_tipo_movimiento = _id_tipo_movimiento_merma(conn)
    id_tipo_merma = _id_tipo_merma(conn, nombre_tipo_merma)
    if id_detalle is not None:
        referencia = f" detalle#{int(id_detalle)}"
    else:
        # Token único para que dos capturas generales del mismo producto y
        # motivo no se agrupen como si fueran un solo reporte.
        referencia = f" general-{time.time_ns()}"
    descripcion = _truncar(f"Producto: {producto} | Motivo: {motivo}", 255)
    ids_mermas: list[int] = []

    for item in receta:
        cantidad = (
            Decimal(str(item["cantidad_usada"])) * Decimal(int(cantidad_unidades))
        ).quantize(Decimal("0.001"))
        if cantidad <= 0:
            continue

        motivo_movimiento = _truncar(
            f"{PREFIJO_MOTIVO}{referencia} | {producto} | {motivo}",
            150,
        )
        movimiento = conn.execute(
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
                "insumo": int(item["id_insumo"]),
                "tipo": id_tipo_movimiento,
                "cantidad": cantidad,
                "motivo": motivo_movimiento,
                "empleado": id_empleado,
                "orden": id_orden,
            },
        )
        id_merma = conn.execute(
            text(
                """
                INSERT INTO mermas
                    (id_movimiento, id_tipo_merma, id_empleado_responsable,
                     id_usuario_registro, descripcion)
                VALUES (:movimiento, :tipo, :empleado, :usuario, :descripcion)
                RETURNING id_merma
                """
            ),
            {
                "movimiento": int(movimiento.scalar_one()),
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
            },
        ).scalar_one()
        ids_mermas.append(int(id_merma))

    if not ids_mermas:
        raise MermaError("La receta no genera cantidades válidas para mermar.")
    return ids_mermas


def _todas_las_partidas_activas_mermadas(
    conn: Connection, id_orden: int
) -> bool:
    """Indica si ya no queda ninguna partida activa sin marcar como merma."""
    activas = conn.execute(
        text(
            """
            SELECT COUNT(*)
            FROM detalles_orden d
            JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
            WHERE d.id_orden = :orden
              AND ed.nombre IN ('Pendiente', 'En preparación', 'Listo', 'Entregado')
              AND NOT EXISTS (
                  SELECT 1
                  FROM auditoria a
                  WHERE a.tabla_afectada = :tabla
                    AND a.id_registro_afectado = d.id_detalle
              )
            """
        ),
        {"orden": int(id_orden), "tabla": TABLA_MERMA_DETALLE},
    ).scalar_one()
    return int(activas) == 0


def _cancelar_orden_y_liberar_mesa(
    conn: Connection, *, id_orden: int, id_mesa: int | None
) -> None:
    """Cancela una orden sin partidas activas y libera su mesa si aplica."""
    id_cancelada = conn.execute(
        text("SELECT id_estado FROM estados_orden WHERE nombre = 'Cancelada'")
    ).scalar_one_or_none()
    if id_cancelada is None:
        raise MermaError("Falta el estado 'Cancelada' en el catálogo.")

    conn.execute(
        text("UPDATE ordenes SET id_estado = :estado WHERE id_orden = :orden"),
        {"estado": int(id_cancelada), "orden": int(id_orden)},
    )
    if id_mesa is None:
        return

    conn.execute(
        text(
            """
            UPDATE mesas
            SET estado = 'Disponible'
            WHERE id_mesa = :mesa
              AND NOT EXISTS (
                  SELECT 1
                  FROM ordenes o
                  JOIN estados_orden e ON e.id_estado = o.id_estado
                  WHERE o.id_mesa = mesas.id_mesa
                    AND e.es_final = FALSE
              )
            """
        ),
        {"mesa": int(id_mesa)},
    )


def registrar_merma_detalle(
    *,
    id_detalle: int,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str,
    origen: str = "orden",
    liberar_mesa_si_total: bool = True,
) -> list[int]:
    """Reporta la merma de una partida y devuelve los ids de ``mermas``.

    ``origen`` distingue el área que reporta (``comanda`` u ``orden``) para
    elegir el tipo de merma del catálogo. Todo ocurre en una sola transacción:
    si falla cualquier paso no queda ni la marca ni el movimiento.

    ``liberar_mesa_si_total`` desactiva la cancelación automática cuando el
    flujo va a reponer la partida (por ejemplo, "Mermar y repetir").
    """
    motivo = (motivo or "").strip()
    if not motivo:
        raise MermaError("Captura el motivo de la merma.")
    if id_empleado is None:
        raise MermaError(
            "Tu usuario no está vinculado a una ficha de empleado, así que no "
            "puede registrar movimientos de inventario. Pide a un administrador "
            "que lo vincule en el módulo Usuarios."
        )

    origen = (origen or "orden").strip().lower()
    nombre_tipo_merma = "Merma de cocina" if origen == "comanda" else "Accidente"

    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT d.id_detalle, d.id_orden, d.id_producto, d.cantidad,
                       p.nombre AS producto, o.id_mesa, eo.nombre AS estado_orden
                FROM detalles_orden d
                JOIN productos p ON p.id_producto = d.id_producto
                JOIN ordenes o ON o.id_orden = d.id_orden
                JOIN estados_orden eo ON eo.id_estado = o.id_estado
                WHERE d.id_detalle = :detalle
                FOR UPDATE
                """
            ),
            {"detalle": id_detalle},
        ).mappings().first()
        if fila is None:
            raise MermaError("La partida ya no existe.")
        estado_orden = str(fila["estado_orden"])
        if estado_orden == "Cancelada":
            raise MermaError(
                "La orden está cancelada; no se puede reportar una merma sobre ella."
            )

        ya_reportada = conn.execute(
            text(
                """
                SELECT 1
                FROM auditoria
                WHERE tabla_afectada = :tabla AND id_registro_afectado = :detalle
                LIMIT 1
                """
            ),
            {"tabla": TABLA_MERMA_DETALLE, "detalle": id_detalle},
        ).scalar()
        if ya_reportada:
            raise MermaError("Este producto ya fue reportado como merma.")

        ids_mermas = _crear_mermas_de_receta(
            conn,
            id_producto=int(fila["id_producto"]),
            producto=str(fila["producto"]),
            cantidad_unidades=int(fila["cantidad"]),
            motivo=motivo,
            id_usuario=id_usuario,
            id_empleado=id_empleado,
            nombre_tipo_merma=nombre_tipo_merma,
            id_orden=int(fila["id_orden"]),
            id_detalle=id_detalle,
        )

        conn.execute(
            text(
                """
                INSERT INTO auditoria
                    (id_usuario, tabla_afectada, id_registro_afectado,
                     accion, datos_nuevos)
                VALUES (:usuario, :tabla, :detalle, 'INSERT', CAST(:datos AS jsonb))
                """
            ),
            {
                "usuario": id_usuario,
                "tabla": TABLA_MERMA_DETALLE,
                "detalle": id_detalle,
                "datos": json.dumps(
                    {
                        "producto": fila["producto"],
                        "motivo": motivo,
                        "origen": origen,
                        "id_orden": int(fila["id_orden"]),
                        "id_mermas": ids_mermas,
                    },
                    ensure_ascii=False,
                ),
            },
        )

        # En cuentas abiertas el producto mermado deja de sumar al total. Si ya
        # no queda ninguna partida activa sin mermar, se cancela la orden y se
        # libera la mesa. En órdenes pagadas el pago histórico no se altera.
        if estado_orden not in ("Pagada", "Cancelada"):
            recalcular_total_orden(conn, int(fila["id_orden"]))
            if liberar_mesa_si_total and _todas_las_partidas_activas_mermadas(
                conn, int(fila["id_orden"])
            ):
                _cancelar_orden_y_liberar_mesa(
                    conn,
                    id_orden=int(fila["id_orden"]),
                    id_mesa=(
                        int(fila["id_mesa"])
                        if fila["id_mesa"] is not None
                        else None
                    ),
                )

    return ids_mermas


def repetir_detalle_mermado(*, id_detalle: int) -> int:
    """Clona una partida mermada en la misma orden, en estado ``Pendiente``.

    Se usa cuando el producto se perdió pero debe prepararse de nuevo. La copia
    conserva producto, cantidad, precio y área de preparación, y vuelve a entrar
    al KDS. En cuentas abiertas el total se recalcula para incluir la reposición;
    en órdenes ya pagadas no se altera el pago histórico.
    """
    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT d.id_orden, d.id_producto, d.cantidad,
                       d.precio_unitario, d.id_area_preparacion,
                       eo.nombre AS estado_orden
                FROM detalles_orden d
                JOIN ordenes o ON o.id_orden = d.id_orden
                JOIN estados_orden eo ON eo.id_estado = o.id_estado
                WHERE d.id_detalle = :detalle
                FOR UPDATE OF d, o
                """
            ),
            {"detalle": int(id_detalle)},
        ).mappings().first()
        if fila is None:
            raise MermaError("La partida ya no existe.")

        mermada = conn.execute(
            text(
                """
                SELECT 1
                FROM auditoria
                WHERE tabla_afectada = :tabla AND id_registro_afectado = :detalle
                LIMIT 1
                """
            ),
            {"tabla": TABLA_MERMA_DETALLE, "detalle": int(id_detalle)},
        ).scalar()
        if not mermada:
            raise MermaError("La partida no está reportada como merma.")

        id_pendiente = conn.execute(
            text(
                "SELECT id_estado FROM estados_detalle_orden "
                "WHERE nombre = 'Pendiente'"
            )
        ).scalar_one_or_none()
        if id_pendiente is None:
            raise MermaError("Falta el estado 'Pendiente' en el catálogo.")

        comentario = conn.execute(
            text(
                """
                SELECT id_usuario, datos_nuevos ->> 'comentario' AS comentario
                FROM auditoria
                WHERE tabla_afectada = 'detalles_orden_comentario'
                  AND id_registro_afectado = :detalle
                ORDER BY id_auditoria DESC
                LIMIT 1
                """
            ),
            {"detalle": int(id_detalle)},
        ).mappings().first()

        id_nuevo = conn.execute(
            text(
                """
                INSERT INTO detalles_orden
                    (id_orden, id_producto, cantidad, precio_unitario,
                     id_area_preparacion, id_estado)
                VALUES (:orden, :producto, :cantidad, :precio, :area, :estado)
                RETURNING id_detalle
                """
            ),
            {
                "orden": int(fila["id_orden"]),
                "producto": int(fila["id_producto"]),
                "cantidad": int(fila["cantidad"]),
                "precio": fila["precio_unitario"],
                "area": int(fila["id_area_preparacion"]),
                "estado": int(id_pendiente),
            },
        ).scalar_one()

        if comentario and (comentario["comentario"] or "").strip():
            conn.execute(
                text(
                    """
                    INSERT INTO auditoria
                        (id_usuario, tabla_afectada, id_registro_afectado,
                         accion, datos_nuevos)
                    VALUES (:usuario, 'detalles_orden_comentario', :detalle,
                            'INSERT', CAST(:datos AS jsonb))
                    """
                ),
                {
                    "usuario": comentario["id_usuario"],
                    "detalle": int(id_nuevo),
                    "datos": json.dumps(
                        {"comentario": comentario["comentario"]},
                        ensure_ascii=False,
                    ),
                },
            )

        if str(fila["estado_orden"]) not in ("Pagada", "Cancelada"):
            recalcular_total_orden(conn, int(fila["id_orden"]))

    return int(id_nuevo)


def registrar_merma_producto(
    *,
    id_producto: int,
    cantidad: int,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str,
) -> list[int]:
    """Captura una merma general de producto, sin orden asociada.

    Queda pendiente de revisión: el Gerente la verá en Inventario y decidirá
    si la sube (descuenta stock y fija el costo) o la elimina.
    """
    motivo = (motivo or "").strip()
    if not motivo:
        raise MermaError("Captura el motivo de la merma.")
    if int(cantidad) <= 0:
        raise MermaError("La cantidad de unidades debe ser mayor a cero.")
    if id_empleado is None:
        raise MermaError(
            "Tu usuario no está vinculado a una ficha de empleado, así que no "
            "puede registrar movimientos de inventario. Pide a un administrador "
            "que lo vincule en el módulo Usuarios."
        )

    with transaccion() as conn:
        producto = conn.execute(
            text(
                """
                SELECT id_producto, nombre
                FROM productos
                WHERE id_producto = :producto AND activo = TRUE
                """
            ),
            {"producto": int(id_producto)},
        ).mappings().first()
        if producto is None:
            raise MermaError("El producto seleccionado no existe o está inactivo.")

        return _crear_mermas_de_receta(
            conn,
            id_producto=int(producto["id_producto"]),
            producto=str(producto["nombre"]),
            cantidad_unidades=int(cantidad),
            motivo=motivo,
            id_usuario=id_usuario,
            id_empleado=id_empleado,
            nombre_tipo_merma="Merma de cocina",
        )


def registrar_merma_insumo(
    *,
    id_insumo: int,
    cantidad: Decimal,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str,
) -> list[int]:
    """Captura una merma general de un insumo base, sin orden asociada.

    No descuenta stock al reportar: crea el movimiento y la fila en ``mermas``
    pendientes. El Gerente la sube desde Inventario, donde se descuenta
    directamente ``insumos.stock_actual`` y se fija el costo de la pérdida.
    """
    motivo = (motivo or "").strip()
    if not motivo:
        raise MermaError("Captura el motivo de la merma.")
    cantidad = Decimal(str(cantidad)).quantize(Decimal("0.001"))
    if cantidad <= 0:
        raise MermaError("La cantidad a mermar debe ser mayor a cero.")
    if id_empleado is None:
        raise MermaError(
            "Tu usuario no está vinculado a una ficha de empleado, así que no "
            "puede registrar movimientos de inventario. Pide a un administrador "
            "que lo vincule en el módulo Usuarios."
        )

    with transaccion() as conn:
        insumo = conn.execute(
            text(
                """
                SELECT i.id_insumo, i.nombre, um.abreviatura AS unidad
                FROM insumos i
                JOIN unidades_medida um ON um.id_unidad = i.id_unidad_medida
                WHERE i.id_insumo = :insumo AND i.activo = TRUE
                """
            ),
            {"insumo": int(id_insumo)},
        ).mappings().first()
        if insumo is None:
            raise MermaError("El insumo seleccionado no existe o está inactivo.")

        id_tipo_movimiento = _id_tipo_movimiento_merma(conn)
        id_tipo_merma = _id_tipo_merma(conn, "Otro")
        referencia = f" general-{time.time_ns()}"
        motivo_movimiento = _truncar(
            f"{PREFIJO_INSUMO}{referencia} | {insumo['nombre']} | {motivo}",
            150,
        )
        movimiento = conn.execute(
            text(
                """
                INSERT INTO movimientos_inventario
                    (id_insumo, id_tipo_movimiento, cantidad, motivo,
                     id_empleado, id_orden)
                VALUES (:insumo, :tipo, :cantidad, :motivo, :empleado, NULL)
                RETURNING id_movimiento
                """
            ),
            {
                "insumo": int(insumo["id_insumo"]),
                "tipo": id_tipo_movimiento,
                "cantidad": cantidad,
                "motivo": motivo_movimiento,
                "empleado": id_empleado,
            },
        )
        descripcion = _truncar(
            f"Insumo: {insumo['nombre']} | Motivo: {motivo}", 255
        )
        id_merma = conn.execute(
            text(
                """
                INSERT INTO mermas
                    (id_movimiento, id_tipo_merma, id_empleado_responsable,
                     id_usuario_registro, descripcion)
                VALUES (:movimiento, :tipo, :empleado, :usuario, :descripcion)
                RETURNING id_merma
                """
            ),
            {
                "movimiento": int(movimiento.scalar_one()),
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
            },
        ).scalar_one()

    return [int(id_merma)]
