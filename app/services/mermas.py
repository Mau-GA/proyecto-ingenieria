"""Lógica de negocio de mermas reportadas por el personal.

Una partida de ``detalles_orden`` se marca como mermada en ``detalle_mermas``.
Esa marca es la que consultan Comandas y Órdenes para tachar el producto y la
que usa el recalculo de totales para no cobrarlo.

Al reportar se crean filas en ``mermas`` con ``id_movimiento = NULL`` y una
fila en ``mermas_pendientes`` con el insumo, cantidad y contexto del reporte.
No se crean movimientos ni se toca el stock: la venta ya descontó la receta
cuando se cobró la orden.

Al pulsar ``Subir merma`` el Gerente confirma la pérdida. Para partidas ya
vendidas solo se registra el costo (no se vuelve a descontar stock); para
capturas manuales de producto/insumo se inserta el movimiento tipo ``Merma``
y se descuenta el inventario en ese momento.

También existen capturas generales sin una orden de por medio:

- ``registrar_merma_producto``: parte de un producto del menú y genera una
  merma pendiente por cada insumo de su receta.
- ``registrar_merma_insumo``: afecta directamente un insumo base (kg, l, pza).

Ambas quedan pendientes hasta que el Gerente las suba o elimine desde
Inventario > Gestión de Mermas.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext
from decimal import Decimal

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from database import transaccion
from services.cocina import registrar_historial_pendiente
from services.modificadores import foto_de_detalle, guardar_foto_modificadores
from services.lotes import existencias_por_lote
from services.ventas import (
    StockInsuficienteError,
    VentaError,
    calcular_consumo,
    descontar_inventario,
    guardar_consumo_detalle,
    recalcular_total_orden,
)

TABLA_MERMA_DETALLE = "detalle_mermas"
TABLA_MERMA_PENDIENTE = "mermas_pendientes"
TABLA_REPOSICION = "detalle_reposiciones"
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
    origen_tipo: str,
    id_orden: int | None = None,
    id_detalle: int | None = None,
    reporte: str | None = None,
    modificadores: list[dict] | None = None,
    registrar_pendiente: bool = True,
    permitir_sin_receta: bool = False,
) -> list[int]:
    """Crea una merma pendiente por cada insumo consumido, sin tocar stock.

    Si la partida trae una foto de modificadores, el consumo se calcula con
    ella (receta - resta + suma), igual que en la venta original.
    """
    consumo = calcular_consumo(
        conn,
        [
            {
                "id_producto": int(id_producto),
                "cantidad": int(cantidad_unidades),
                "id_detalle": id_detalle,
                "modificadores": modificadores or [],
            }
        ],
    )
    if not consumo:
        if not permitir_sin_receta:
            raise MermaError(
                f"El producto «{producto}» no tiene receta registrada; "
                "no se puede descontar inventario. Agrega su receta primero."
            )
        # En una cancelación autorizada de una partida ya vendida no hay
        # insumos que descontar, pero sí debe quedar el registro de la merma.
        id_tipo_merma = _id_tipo_merma(conn, nombre_tipo_merma)
        id_merma = conn.execute(
            text(
                """
                INSERT INTO mermas
                    (id_movimiento, id_tipo_merma, id_empleado_responsable,
                     id_usuario_registro, descripcion, costo_estimado_perdida)
                VALUES (NULL, :tipo, :empleado, :usuario, :descripcion, 0)
                RETURNING id_merma
                """
            ),
            {
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": _truncar(
                    f"Producto: {producto} | Motivo: {motivo}", 255
                ),
            },
        ).scalar_one()
        return [int(id_merma)]

    filas = conn.execute(
        text(
            """
            SELECT i.id_insumo, i.nombre AS insumo,
                   i.costo_unitario_referencia
            FROM insumos i
            WHERE i.id_insumo IN :ids
            ORDER BY i.id_insumo
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": sorted(consumo)},
    ).mappings().all()
    por_id = {int(fila["id_insumo"]): fila for fila in filas}

    id_tipo_merma = _id_tipo_merma(conn, nombre_tipo_merma)
    reporte = reporte or f"{origen_tipo}-{time.time_ns()}"
    descripcion = _truncar(f"Producto: {producto} | Motivo: {motivo}", 255)
    ids_mermas: list[int] = []

    for id_insumo in sorted(consumo):
        cantidad = Decimal(str(consumo[id_insumo])).quantize(Decimal("0.001"))
        if cantidad <= 0:
            continue
        item = por_id.get(int(id_insumo))
        if item is None:
            raise MermaError(
                "Un insumo del consumo ya no existe en el catálogo."
            )

        id_merma = conn.execute(
            text(
                """
                INSERT INTO mermas
                    (id_movimiento, id_tipo_merma, id_empleado_responsable,
                     id_usuario_registro, descripcion)
                VALUES (NULL, :tipo, :empleado, :usuario, :descripcion)
                RETURNING id_merma
                """
            ),
            {
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
            },
        ).scalar_one()
        id_merma = int(id_merma)
        ids_mermas.append(id_merma)

        if registrar_pendiente:
            conn.execute(
                text(
                    """
                    INSERT INTO mermas_pendientes
                        (id_merma, id_insumo, cantidad, id_orden, id_detalle,
                         origen, reporte, motivo, producto, id_empleado,
                         id_usuario, tipo_merma)
                    VALUES
                        (:merma, :insumo, :cantidad, :orden, :detalle,
                         :origen, :reporte, :motivo, :producto, :empleado,
                         :usuario, :tipo_merma)
                    """
                ),
                {
                    "merma": id_merma,
                    "insumo": int(item["id_insumo"]),
                    "cantidad": cantidad,
                    "orden": id_orden,
                    "detalle": id_detalle,
                    "origen": origen_tipo,
                    "reporte": reporte,
                    "motivo": motivo,
                    "producto": producto,
                    "empleado": id_empleado,
                    "usuario": id_usuario,
                    "tipo_merma": nombre_tipo_merma,
                },
            )
        else:
            # La partida ya se vendió y la receta ya descontó stock; solo se
            # registra el costo estimado de la pérdida.
            costo = Decimal(str(item["costo_unitario_referencia"] or 0))
            conn.execute(
                text(
                    """
                    UPDATE mermas
                    SET costo_estimado_perdida = :costo
                    WHERE id_merma = :merma
                    """
                ),
                {
                    "costo": (cantidad * costo).quantize(Decimal("0.01")),
                    "merma": id_merma,
                },
            )

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
                  FROM detalle_mermas m
                  WHERE m.id_detalle = d.id_detalle
              )
            """
        ),
        {"orden": int(id_orden)},
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
    repetir: bool = False,
    conn: Connection | None = None,
    nombre_tipo_merma: str | None = None,
    autorizada: bool = False,
) -> list[int]:
    """Reporta la merma de una partida y devuelve los ids de ``mermas``.

    ``origen`` distingue el área que reporta (``comanda`` u ``orden``) para
    elegir el tipo de merma del catálogo. Todo ocurre en una sola transacción:
    si falla cualquier paso no queda ni la marca ni el movimiento.

    ``liberar_mesa_si_total`` desactiva la cancelación automática cuando el
    flujo va a reponer la partida. ``repetir`` crea la reposición y descuenta
    su receta dentro de la misma transacción.

    ``conn`` permite participar de una transacción ya abierta (por ejemplo, la
    cancelación autorizada de una orden). ``nombre_tipo_merma`` sobrescribe el
    tipo inferido y ``autorizada`` evita la bandeja de pendientes y el recálculo
    automático, porque la cancelación ya validó y cerró la orden.
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
    tipo_merma = nombre_tipo_merma or (
        "Merma de cocina" if origen == "comanda" else "Accidente"
    )

    gestor = nullcontext(conn) if conn is not None else transaccion()
    with gestor as conexion:
        fila = conexion.execute(
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

        ya_reportada = conexion.execute(
            text(
                """
                SELECT 1
                FROM detalle_mermas
                WHERE id_detalle = :detalle
                LIMIT 1
                """
            ),
            {"detalle": id_detalle},
        ).scalar()
        if ya_reportada:
            raise MermaError("Este producto ya fue reportado como merma.")

        foto_modificadores = foto_de_detalle(conexion, id_detalle)
        ids_mermas = _crear_mermas_de_receta(
            conexion,
            id_producto=int(fila["id_producto"]),
            producto=str(fila["producto"]),
            cantidad_unidades=int(fila["cantidad"]),
            motivo=motivo,
            id_usuario=id_usuario,
            id_empleado=id_empleado,
            nombre_tipo_merma=tipo_merma,
            origen_tipo="partida",
            id_orden=int(fila["id_orden"]),
            id_detalle=id_detalle,
            reporte=f"partida-{int(id_detalle)}-{time.time_ns()}",
            modificadores=foto_modificadores,
            registrar_pendiente=not autorizada,
            permitir_sin_receta=autorizada,
        )

        conexion.execute(
            text(
                """
                INSERT INTO detalle_mermas
                    (id_detalle, id_orden, id_usuario, producto, motivo,
                     origen, id_mermas)
                VALUES
                    (:detalle, :orden, :usuario, :producto, :motivo,
                     :origen, CAST(:id_mermas AS jsonb))
                """
            ),
            {
                "usuario": id_usuario,
                "detalle": id_detalle,
                "orden": int(fila["id_orden"]),
                "producto": str(fila["producto"]),
                "motivo": motivo,
                "origen": origen,
                "id_mermas": json.dumps(ids_mermas, ensure_ascii=False),
            },
        )

        # En cuentas abiertas el producto mermado deja de sumar al total. Si ya
        # no queda ninguna partida activa sin mermar, se cancela la orden y se
        # libera la mesa. En órdenes pagadas el pago histórico no se altera.
        if not autorizada and estado_orden not in ("Pagada", "Cancelada"):
            recalcular_total_orden(conexion, int(fila["id_orden"]))
            if (
                not repetir
                and liberar_mesa_si_total
                and _todas_las_partidas_activas_mermadas(
                    conexion, int(fila["id_orden"])
                )
            ):
                _cancelar_orden_y_liberar_mesa(
                    conexion,
                    id_orden=int(fila["id_orden"]),
                    id_mesa=(
                        int(fila["id_mesa"])
                        if fila["id_mesa"] is not None
                        else None
                    ),
                )

        if repetir:
            _repetir_detalle_en(
                conexion,
                id_detalle,
                id_empleado=id_empleado,
                id_usuario=id_usuario,
            )

    return ids_mermas


def _repetir_detalle_en(
    conn: Connection,
    id_detalle: int,
    *,
    id_empleado: int | None,
    id_usuario: int | None = None,
) -> int:
    """Crea la reposición y descuenta su receta dentro de la transacción actual."""
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
        text("SELECT 1 FROM detalle_mermas WHERE id_detalle = :detalle LIMIT 1"),
        {"detalle": int(id_detalle)},
    ).scalar()
    if not mermada:
        raise MermaError("La partida no está reportada como merma.")

    foto_modificadores = foto_de_detalle(conn, int(id_detalle))

    ya_repetida = conn.execute(
        text(
            "SELECT 1 FROM detalle_reposiciones "
            "WHERE id_detalle = :detalle LIMIT 1"
        ),
        {"detalle": int(id_detalle)},
    ).scalar()
    if ya_repetida:
        raise MermaError("Esta merma ya tiene una reposición registrada.")

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
            SELECT id_usuario, comentario
            FROM detalle_notas
            WHERE id_detalle = :detalle
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
    id_nuevo = int(id_nuevo)
    # La reposición también nace en "Pendiente" y debe aparecer en Comandas
    # con su propia hora de recepción.
    registrar_historial_pendiente(
        conn,
        id_detalle=id_nuevo,
        id_usuario=id_usuario,
        id_estado=int(id_pendiente),
    )
    guardar_foto_modificadores(conn, id_nuevo, foto_modificadores)
    if foto_modificadores:
        consumo = calcular_consumo(
            conn,
            [
                {
                    "id_producto": int(fila["id_producto"]),
                    "cantidad": int(fila["cantidad"]),
                    "id_detalle": int(id_detalle),
                    "modificadores": foto_modificadores,
                }
            ],
        )
        guardar_consumo_detalle(conn, id_detalle=id_nuevo, consumo=consumo)

    if comentario and (comentario["comentario"] or "").strip():
        conn.execute(
            text(
                """
                INSERT INTO detalle_notas (id_detalle, comentario, id_usuario)
                VALUES (:detalle, :comentario, :usuario)
                ON CONFLICT (id_detalle) DO NOTHING
                """
            ),
            {
                "usuario": comentario["id_usuario"],
                "detalle": id_nuevo,
                "comentario": comentario["comentario"],
            },
        )

    conn.execute(
        text(
            """
            INSERT INTO detalle_reposiciones
                (id_detalle, id_detalle_reposicion, id_usuario)
            VALUES (:detalle, :nuevo, :usuario)
            """
        ),
        {
            "detalle": int(id_detalle),
            "nuevo": id_nuevo,
            "usuario": id_usuario,
        },
    )

    if id_empleado is not None:
        try:
            descontar_inventario(
                conn,
                id_orden=int(fila["id_orden"]),
                partidas=[
                    {
                        "id_producto": int(fila["id_producto"]),
                        "cantidad": int(fila["cantidad"]),
                        "id_detalle": id_nuevo,
                        "modificadores": foto_modificadores,
                    }
                ],
                id_empleado=id_empleado,
            )
        except StockInsuficienteError as exc:
            raise MermaError(
                "No se puede repetir la partida: " + str(exc)
            ) from exc

    if str(fila["estado_orden"]) not in ("Pagada", "Cancelada"):
        recalcular_total_orden(conn, int(fila["id_orden"]))

    return id_nuevo


def repetir_detalle_mermado(
    *, id_detalle: int, id_usuario: int | None = None
) -> int:
    """Clona una partida mermada y descuenta la receta de la reposición."""
    with transaccion() as conn:
        empleado = conn.execute(
            text(
                """
                SELECT id_empleado
                FROM mermas_pendientes
                WHERE id_detalle = :detalle
                ORDER BY id_merma DESC
                LIMIT 1
                """
            ),
            {"detalle": int(id_detalle)},
        ).scalar()
        if empleado is None:
            raise MermaError(
                "No se encontró el empleado que reportó la merma; "
                "no se puede descontar la reposición."
            )
        return _repetir_detalle_en(
            conn,
            int(id_detalle),
            id_empleado=int(empleado),
            id_usuario=id_usuario,
        )


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
            origen_tipo="producto",
            reporte=f"producto-{time.time_ns()}",
        )


def registrar_merma_insumo(
    *,
    id_insumo: int,
    cantidad: Decimal,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str,
    conn: Connection | None = None,
    nombre_tipo_merma: str = "Otro",
    id_lote: int | None = None,
) -> list[int]:
    """Captura una merma general de un insumo base, sin orden asociada.

    No descuenta stock al reportar: crea la fila en ``mermas`` con
    ``id_movimiento = NULL`` y su fila en ``mermas_pendientes``. El Gerente la
    sube desde Inventario, donde se inserta el movimiento tipo ``Merma``, se
    descuenta ``insumos.stock_actual`` y se fija el costo de la pérdida.

    ``conn`` permite participar de una transacción ya abierta. ``id_lote`` se
    usa para mermas de caducidad y queda en ``mermas.id_lote``.
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

    contexto = nullcontext(conn) if conn is not None else transaccion()
    with contexto as conexion:
        insumo = conexion.execute(
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

        id_tipo_merma = _id_tipo_merma(conexion, nombre_tipo_merma)
        descripcion = _truncar(
            f"Insumo: {insumo['nombre']} | Motivo: {motivo}", 255
        )
        id_merma = conexion.execute(
            text(
                """
                INSERT INTO mermas
                    (id_movimiento, id_tipo_merma, id_empleado_responsable,
                     id_usuario_registro, descripcion, id_lote)
                VALUES (NULL, :tipo, :empleado, :usuario, :descripcion, :lote)
                RETURNING id_merma
                """
            ),
            {
                "tipo": id_tipo_merma,
                "empleado": id_empleado,
                "usuario": id_usuario,
                "descripcion": descripcion,
                "lote": int(id_lote) if id_lote is not None else None,
            },
        ).scalar_one()
        id_merma = int(id_merma)

        conexion.execute(
            text(
                """
                INSERT INTO mermas_pendientes
                    (id_merma, id_insumo, cantidad, origen, reporte, motivo,
                     producto, id_empleado, id_usuario, tipo_merma)
                VALUES
                    (:merma, :insumo, :cantidad, 'insumo', :reporte, :motivo,
                     :producto, :empleado, :usuario, :tipo_merma)
                """
            ),
            {
                "merma": id_merma,
                "insumo": int(insumo["id_insumo"]),
                "cantidad": cantidad,
                "reporte": (
                    f"lote-{int(id_lote)}-{time.time_ns()}"
                    if id_lote is not None
                    else f"insumo-{time.time_ns()}"
                ),
                "motivo": motivo,
                "producto": insumo["nombre"],
                "empleado": id_empleado,
                "usuario": id_usuario,
                "tipo_merma": nombre_tipo_merma,
            },
        )

    return [id_merma]


def registrar_merma_lote(
    *,
    id_lote: int,
    id_usuario: int,
    id_empleado: int | None,
    motivo: str = "Caducidad",
) -> list[int]:
    """Registra la merma pendiente de la existencia restante de un lote caducado.

    La cantidad se calcula con el mismo reparto FEFO usado en Inventario. No
    toca stock todavía: la merma se confirma al subirla desde Gestión de Mermas.
    """
    with transaccion() as conn:
        lote = conn.execute(
            text(
                """
                SELECT l.id_lote, l.id_insumo, l.fecha_caducidad, l.desechado,
                       i.nombre AS insumo
                FROM lotes_insumo l
                JOIN insumos i ON i.id_insumo = l.id_insumo
                WHERE l.id_lote = :lote
                FOR UPDATE OF l
                """
            ),
            {"lote": int(id_lote)},
        ).mappings().first()
        if lote is None:
            raise MermaError("El lote seleccionado ya no existe.")
        if lote["desechado"]:
            raise MermaError("Este lote ya fue desechado.")
        hoy = conn.execute(text("SELECT CURRENT_DATE")).scalar_one()
        if lote["fecha_caducidad"] is None:
            raise MermaError("El lote no tiene fecha de caducidad.")
        if lote["fecha_caducidad"] > hoy:
            raise MermaError("Solo se pueden mermar lotes caducados.")
        pendiente = conn.execute(
            text(
                """
                SELECT 1
                FROM mermas m
                WHERE m.id_lote = :lote
                  AND m.costo_estimado_perdida IS NULL
                LIMIT 1
                """
            ),
            {"lote": int(id_lote)},
        ).scalar()
        if pendiente:
            raise MermaError(
                "Este lote ya tiene una merma pendiente de autorización."
            )

        existencias = existencias_por_lote(conn, [int(lote["id_insumo"])])
        restante = next(
            (
                Decimal(str(fila["cantidad_restante"]))
                for fila in existencias
                if fila["id_lote"] == int(id_lote)
            ),
            Decimal("0"),
        )
        if restante <= 0:
            raise MermaError(
                "El lote no tiene existencia disponible para mermar."
            )
        motivo = (motivo or "Caducidad").strip()
        return registrar_merma_insumo(
            id_insumo=int(lote["id_insumo"]),
            cantidad=restante,
            id_usuario=id_usuario,
            id_empleado=id_empleado,
            motivo=motivo,
            conn=conn,
            nombre_tipo_merma="Caducado",
            id_lote=int(id_lote),
        )
