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
                """
            ),
            {
                "movimiento": int(movimiento.lastrowid),
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
            },
        ).lastrowid
        ids_mermas.append(int(id_merma))

    if not ids_mermas:
        raise MermaError("La receta no genera cantidades válidas para mermar.")
    return ids_mermas


def registrar_merma_detalle(
    *,
    id_detalle: int,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str,
    origen: str = "orden",
) -> list[int]:
    """Reporta la merma de una partida y devuelve los ids de ``mermas``.

    ``origen`` distingue el área que reporta (``comanda`` u ``orden``) para
    elegir el tipo de merma del catálogo. Todo ocurre en una sola transacción:
    si falla cualquier paso no queda ni la marca ni el movimiento.
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
                VALUES (:usuario, :tabla, :detalle, 'INSERT', :datos)
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

        # En cuentas abiertas el producto mermado deja de sumar al total. En
        # órdenes ya pagadas (FoodHouse) el pago histórico no se altera.
        if estado_orden not in ("Pagada", "Cancelada"):
            recalcular_total_orden(conn, int(fila["id_orden"]))

    return ids_mermas


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
                """
            ),
            {
                "movimiento": int(movimiento.lastrowid),
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
            },
        ).lastrowid

    return [int(id_merma)]
