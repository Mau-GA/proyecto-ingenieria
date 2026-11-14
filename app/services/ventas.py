"""
Ruta: proyectorestaurante/app/services/ventas.py

Lógica de negocio de ventas e inventario, SIN interfaz (no usa widgets de
Streamlit), para que la compartan FoodHouse (modules/foodhouse.py) y el
módulo de Órdenes por mesa (modules/ordenes.py).

Regla central: todo lo que pasa al vender ocurre en UNA transacción:

    ordenes -> detalles_orden -> descuento de inventario por receta
            -> total recalculado -> pagos (si se cobra en el momento)

Descuento de inventario por receta:
  1. Se calcula el consumo total por insumo sumando todas las partidas
     (cantidad vendida x recetas.cantidad_usada). Si dos productos usan el
     mismo insumo (p. ej. Carne Res en 3 hamburguesas) se valida la SUMA.
  2. Se bloquean las filas de esos insumos con SELECT ... FOR UPDATE para
     que dos cajas vendiendo a la vez no dejen el stock en negativo.
  3. Si algún insumo no alcanza se lanza StockInsuficienteError y se hace
     ROLLBACK de toda la venta (la columna insumos.stock_actual además tiene
     CHECK (stock_actual >= 0) como segunda barrera en la BD).
  4. Se registra una fila en movimientos_inventario por insumo
     (tipo 'Salida por venta', con id_orden) y se resta insumos.stock_actual.

Los productos sin receta (p. ej. uno recién dado de alta) se venden sin
descontar nada.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from database import get_engine, transaccion

CERO = Decimal("0")


# =====================================================================
# Errores de negocio (la UI los muestra como mensajes claros)
# =====================================================================
class VentaError(Exception):
    """Base de los errores de negocio de una venta."""


@dataclass
class Faltante:
    insumo: str
    unidad: str
    requerido: Decimal
    disponible: Decimal


class StockInsuficienteError(VentaError):
    def __init__(self, faltantes: list[Faltante]):
        self.faltantes = faltantes
        detalle = "; ".join(
            f"{f.insumo}: requiere {f.requerido:.3f} {f.unidad}, hay {f.disponible:.3f}"
            for f in faltantes
        )
        super().__init__(f"Stock insuficiente — {detalle}")


class EmpleadoRequeridoError(VentaError):
    def __init__(self):
        super().__init__(
            "Tu usuario no está vinculado a una ficha de empleado y la venta "
            "incluye productos con receta: no se puede registrar la salida de "
            "inventario. Pide a un administrador que vincule tu usuario a un "
            "empleado en el módulo Usuarios."
        )


# =====================================================================
# Utilidades
# =====================================================================
def dinero(valor) -> Decimal:
    """Decimal con 2 decimales (nunca float para montos)."""
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _id_por_nombre(conn: Connection, sql: str, nombre: str) -> int:
    """Busca el id de un catálogo por nombre; error claro si falta en seed.sql."""
    valor = conn.execute(text(sql), {"n": nombre}).scalar()
    if valor is None:
        raise VentaError(f"Falta el registro de catálogo '{nombre}'. Revisa db/seed.sql.")
    return int(valor)


def _guardar_comentario(
    conn: Connection,
    *,
    id_detalle: int,
    id_usuario: int | None,
    comentario: str | None,
) -> None:
    """Guarda la nota de preparación de una partida.

    El esquema no tiene una columna específica para comentarios y no se debe
    modificar la base de datos, así que se usa la bitácora `auditoria` como
    almacén JSON asociado al id del detalle. Esa misma nota se recupera en
    Órdenes y en Comandas con una subconsulta al último registro.
    """
    texto = (comentario or "").strip()
    if not texto:
        return

    conn.execute(
        text(
            """
            INSERT INTO auditoria
                (id_usuario, tabla_afectada, id_registro_afectado, accion, datos_nuevos)
            VALUES
                (:usuario, 'detalles_orden_comentario', :detalle, 'INSERT', CAST(:datos AS jsonb))
            """
        ),
        {
            "usuario": id_usuario,
            "detalle": id_detalle,
            "datos": json.dumps({"comentario": texto}, ensure_ascii=False),
        },
    )


def recalcular_total_orden(conn: Connection, id_orden: int) -> Decimal:
    """Recalcula y guarda el total de una orden.

    Las partidas reportadas como merma se excluyen del cobro: su marca vive
    en la bitácora `auditoria` (tabla_afectada = 'detalles_orden_merma').
    Así, un producto mermado no se sigue cobrando aunque permanezca visible
    en la cuenta para que el personal sepa qué pasó.
    """
    total = dinero(
        conn.execute(
            text(
                """
                SELECT COALESCE(SUM(d.subtotal), 0)
                FROM detalles_orden d
                WHERE d.id_orden = :o
                  AND NOT EXISTS (
                      SELECT 1
                      FROM auditoria a
                      WHERE a.tabla_afectada = 'detalles_orden_merma'
                        AND a.id_registro_afectado = d.id_detalle
                  )
                """
            ),
            {"o": id_orden},
        ).scalar_one()
    )
    conn.execute(
        text("UPDATE ordenes SET total = :t WHERE id_orden = :o"),
        {"t": total, "o": id_orden},
    )
    return total


def siguiente_numero_orden_diaria(conn: Connection) -> int:
    """Siguiente consecutivo visible de orden para la fecha actual.

    Se guarda en ``ordenes.numero_orden_diaria`` y reinicia a 1 cada día.
    El advisory lock serializa la asignación entre transacciones simultáneas
    para que dos órdenes del mismo día no reciban el mismo número.
    """
    conn.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtext('ordenes_dia_' || CURRENT_DATE::text))"
        )
    )
    return int(
        conn.execute(
            text(
                """
                SELECT COALESCE(MAX(numero_orden_diaria), 0) + 1
                FROM ordenes
                WHERE DATE(fecha_orden) = CURRENT_DATE
                """
            )
        ).scalar_one()
    )


# =====================================================================
# Inventario
# =====================================================================
def calcular_consumo(conn: Connection, partidas: list[dict]) -> dict[int, Decimal]:
    """
    Regresa {id_insumo: cantidad_total_a_descontar} para las partidas dadas.
    partidas: [{"id_producto": int, "cantidad": int, ...}, ...]
    """
    cantidades: dict[int, int] = {}
    for p in partidas:
        pid = int(p["id_producto"])
        cantidades[pid] = cantidades.get(pid, 0) + int(p["cantidad"])
    if not cantidades:
        return {}

    filas = conn.execute(
        text(
            "SELECT id_producto, id_insumo, cantidad_usada "
            "FROM recetas WHERE id_producto IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": list(cantidades)},
    ).all()

    consumo: dict[int, Decimal] = {}
    for id_producto, id_insumo, cantidad_usada in filas:
        consumo[int(id_insumo)] = (
            consumo.get(int(id_insumo), CERO)
            + Decimal(str(cantidad_usada)) * cantidades[int(id_producto)]
        )
    return consumo


def porciones_disponibles(
    conn_o_none: Connection | None,
) -> dict[int, int | None]:
    """Porciones que alcanzan por producto con el stock actual.

    Para cada producto con receta se toma el mínimo de
    ``floor(stock_actual / cantidad_usada)`` entre todos sus insumos. Un
    producto sin receta regresa ``None`` (sin límite calculable). Esta lectura
    es solo informativa: la validación definitiva sigue siendo la de
    ``descontar_inventario`` con ``FOR UPDATE``.
    """

    def _calcular(conn: Connection) -> dict[int, int | None]:
        productos = conn.execute(
            text("SELECT id_producto FROM productos")
        ).scalars().all()
        resultado: dict[int, int | None] = {
            int(id_producto): None for id_producto in productos
        }

        filas = conn.execute(
            text(
                """
                SELECT r.id_producto, r.cantidad_usada, i.stock_actual
                FROM recetas r
                JOIN insumos i ON i.id_insumo = r.id_insumo
                """
            )
        ).all()
        limites: dict[int, int] = {}
        for id_producto, cantidad_usada, stock_actual in filas:
            cantidad = Decimal(str(cantidad_usada))
            if cantidad <= 0:
                continue
            porciones = int(
                (Decimal(str(stock_actual)) / cantidad).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            id_producto = int(id_producto)
            if id_producto in limites:
                limites[id_producto] = min(limites[id_producto], porciones)
            else:
                limites[id_producto] = porciones

        for id_producto, porciones in limites.items():
            resultado[id_producto] = max(0, porciones)
        return resultado

    if conn_o_none is None:
        with get_engine().connect() as conn:
            return _calcular(conn)
    return _calcular(conn_o_none)


def descontar_inventario(
    conn: Connection,
    *,
    id_orden: int,
    partidas: list[dict],
    id_empleado: int | None,
) -> list[dict]:
    """
    Descuenta el inventario según receta DENTRO de la transacción `conn`.
    Regresa un resumen [{"insumo", "unidad", "cantidad", "stock_restante"}].
    """
    consumo = calcular_consumo(conn, partidas)
    if not consumo:
        return []  # ningún producto de la venta tiene receta
    if id_empleado is None:
        raise EmpleadoRequeridoError()

    # Bloqueo de filas (orden por id para evitar deadlocks entre cajas)
    stock = conn.execute(
        text(
            """
            SELECT i.id_insumo, i.nombre, i.stock_actual, u.abreviatura
            FROM insumos i
            JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
            WHERE i.id_insumo IN :ids
            ORDER BY i.id_insumo
            FOR UPDATE
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": sorted(consumo)},
    ).all()
    por_id = {int(r.id_insumo): r for r in stock}

    faltantes = [
        Faltante(
            insumo=por_id[i].nombre,
            unidad=por_id[i].abreviatura,
            requerido=requerido,
            disponible=Decimal(str(por_id[i].stock_actual)),
        )
        for i, requerido in sorted(consumo.items())
        if Decimal(str(por_id[i].stock_actual)) < requerido
    ]
    if faltantes:
        raise StockInsuficienteError(faltantes)

    id_tipo = _id_por_nombre(
        conn,
        "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario WHERE nombre = :n",
        "Salida por venta",
    )

    conn.execute(
        text(
            """
            INSERT INTO movimientos_inventario
                (id_insumo, id_tipo_movimiento, cantidad, motivo, id_empleado, id_orden)
            VALUES (:insumo, :tipo, :cantidad, :motivo, :empleado, :orden)
            """
        ),
        [
            {
                "insumo": i, "tipo": id_tipo, "cantidad": cant,
                "motivo": f"Venta orden #{id_orden} (descuento automático por receta)",
                "empleado": id_empleado, "orden": id_orden,
            }
            for i, cant in sorted(consumo.items())
        ],
    )
    conn.execute(
        text("UPDATE insumos SET stock_actual = stock_actual - :c WHERE id_insumo = :i"),
        [{"c": cant, "i": i} for i, cant in sorted(consumo.items())],
    )

    return [
        {
            "insumo": por_id[i].nombre,
            "unidad": por_id[i].abreviatura,
            "cantidad": cant,
            "stock_restante": Decimal(str(por_id[i].stock_actual)) - cant,
        }
        for i, cant in sorted(consumo.items())
    ]


def _revertir_inventario(
    conn: Connection,
    *,
    id_orden: int,
    partida: dict,
    id_empleado: int | None,
    motivo: str | None = None,
) -> None:
    """Devuelve al stock los insumos de una partida eliminada o reducida.

    Se registra un movimiento de 'Ajuste positivo' para conservar el historial
    completo; no se borra el movimiento de salida original. ``motivo`` permite
    distinguir una eliminación de una reducción de cantidad.

    Limitación conocida: el consumo se calcula con la receta vigente, no con
    una foto histórica. Si la receta cambió entre la venta y esta reversa, la
    cantidad devuelta puede diferir de la descontada originalmente. Corregirlo
    requeriría guardar la receta aplicada por partida (cambio de esquema), por
    lo que en esta fase solo se documenta.
    """
    consumo = calcular_consumo(conn, [partida])
    if not consumo:
        return
    if id_empleado is None:
        raise EmpleadoRequeridoError()

    id_tipo = _id_por_nombre(
        conn,
        "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario WHERE nombre = :n",
        "Ajuste positivo",
    )
    conn.execute(
        text(
            """
            INSERT INTO movimientos_inventario
                (id_insumo, id_tipo_movimiento, cantidad, motivo, id_empleado, id_orden)
            VALUES (:insumo, :tipo, :cantidad, :motivo, :empleado, :orden)
            """
        ),
        [
            {
                "insumo": i,
                "tipo": id_tipo,
                "cantidad": cant,
                "motivo": motivo
                or f"Reversa por eliminación de partida de la orden #{id_orden}",
                "empleado": id_empleado,
                "orden": id_orden,
            }
            for i, cant in sorted(consumo.items())
        ],
    )
    conn.execute(
        text("UPDATE insumos SET stock_actual = stock_actual + :c WHERE id_insumo = :i"),
        [{"c": cant, "i": i} for i, cant in sorted(consumo.items())],
    )


# =====================================================================
# Órdenes
# =====================================================================
def _expandir_partidas(partidas: list[dict]) -> list[dict]:
    """Expande cada partida en N partidas individuales de cantidad 1.

    Así Comandas y Mermas pueden marcar una sola unidad (p. ej. una alita)
    sin afectar a las demás, aunque el mesero haya capturado cantidad 3.
    """
    expandidas: list[dict] = []
    for partida in partidas:
        cantidad = int(partida["cantidad"])
        if cantidad <= 0:
            raise VentaError("La cantidad de cada producto debe ser mayor a cero.")
        for _ in range(cantidad):
            expandidas.append({**partida, "cantidad": 1})
    return expandidas


def agregar_partidas(
    conn: Connection,
    *,
    id_orden: int,
    partidas: list[dict],
    id_empleado: int | None,
    id_usuario: int | None = None,
) -> tuple[Decimal, list[dict]]:
    """
    Inserta partidas en detalles_orden, descuenta inventario y recalcula el
    total de la orden. Todo dentro de `conn`.

    partidas: [{"id_producto", "cantidad", "precio_unitario",
                "id_area_preparacion", "comentario"(opcional)}]
    Regresa (total_actualizado_de_la_orden, resumen_inventario).
    """
    if not partidas:
        raise VentaError("La orden no tiene productos.")

    # Granularidad 1 a 1: cada unidad se guarda como una partida propia.
    partidas = _expandir_partidas(partidas)

    id_pendiente = _id_por_nombre(
        conn, "SELECT id_estado FROM estados_detalle_orden WHERE nombre = :n", "Pendiente"
    )
    for p in partidas:
        resultado = conn.execute(
            text(
                """
                INSERT INTO detalles_orden (id_orden, id_producto, cantidad,
                                            precio_unitario, id_area_preparacion, id_estado)
                VALUES (:orden, :producto, :cantidad, :precio, :area, :estado)
                RETURNING id_detalle
                """
            ),
            {
                "orden": id_orden, "producto": int(p["id_producto"]),
                "cantidad": int(p["cantidad"]), "precio": dinero(p["precio_unitario"]),
                "area": int(p["id_area_preparacion"]), "estado": id_pendiente,
            },
        )
        _guardar_comentario(
            conn,
            id_detalle=int(resultado.scalar_one()),
            id_usuario=id_usuario,
            comentario=p.get("comentario"),
        )

    resumen = descontar_inventario(conn, id_orden=id_orden, partidas=partidas, id_empleado=id_empleado)

    # El total lo calcula la BD desde la columna generada `subtotal`, pero
    # excluyendo las partidas ya reportadas como merma.
    total = recalcular_total_orden(conn, id_orden)
    return total, resumen


def registrar_venta(
    *,
    partidas: list[dict],
    id_tipo_orden: int,
    id_usuario: int,
    id_empleado: int | None,
    id_metodo_pago: int,
    id_mesa: int | None = None,
    id_plataforma: int | None = None,
    referencia_externa: str | None = None,
    referencia_pago: str | None = None,
) -> dict:
    """
    Venta de mostrador cobrada en el momento (FoodHouse).
    Crea la orden en estado 'Pagada', sus partidas en 'Pendiente' (para que
    cocina/bar las preparen), descuenta inventario y registra el pago.
    Si cualquier paso falla -> ROLLBACK completo.
    """
    with transaccion() as conn:
        id_pagada = _id_por_nombre(
            conn, "SELECT id_estado FROM estados_orden WHERE nombre = :n", "Pagada"
        )
        numero_dia = siguiente_numero_orden_diaria(conn)
        res = conn.execute(
            text(
                """
                INSERT INTO ordenes (id_tipo_orden, id_mesa, id_empleado_atiende, id_usuario_registro,
                                     id_plataforma_externa, referencia_externa, id_estado, total,
                                     numero_orden_diaria)
                VALUES (:tipo, :mesa, :empleado, :usuario, :plataforma, :ref_ext, :estado, 0,
                        :numero_dia)
                RETURNING id_orden, numero_orden_diaria
                """
            ),
            {
                "tipo": id_tipo_orden, "mesa": id_mesa, "empleado": id_empleado,
                "usuario": id_usuario, "plataforma": id_plataforma,
                "ref_ext": referencia_externa or None, "estado": id_pagada,
                "numero_dia": numero_dia,
            },
        )
        fila_orden = res.mappings().one()
        id_orden = int(fila_orden["id_orden"])
        numero_dia = int(fila_orden["numero_orden_diaria"])

        total, resumen = agregar_partidas(
            conn, id_orden=id_orden, partidas=partidas, id_empleado=id_empleado,
            id_usuario=id_usuario,
        )
        if total <= 0:
            raise VentaError("El total de la venta debe ser mayor a cero.")

        conn.execute(
            text(
                """
                INSERT INTO pagos (id_orden, id_metodo_pago, monto, referencia_pago, id_usuario_registro)
                VALUES (:orden, :metodo, :monto, :ref, :usuario)
                """
            ),
            {
                "orden": id_orden, "metodo": id_metodo_pago, "monto": total,
                "ref": referencia_pago or None, "usuario": id_usuario,
            },
        )

    return {
        "id_orden": id_orden,
        "numero_dia": numero_dia,
        "total": total,
        "inventario": resumen,
    }


def registrar_comanda(
    *,
    partidas: list[dict],
    id_tipo_orden: int,
    id_usuario: int,
    id_empleado: int | None,
    id_mesa: int | None = None,
    id_plataforma: int | None = None,
    referencia_externa: str | None = None,
) -> dict:
    """Registra una comanda que queda pendiente de cobro (FoodHouse).

    Crea la orden en estado 'Pendiente' (sin pago), inserta sus partidas ya
    desglosadas 1 a 1, descuenta inventario y asigna el número de orden del
    día. El cobro posterior se hace desde Órdenes.
    """
    with transaccion() as conn:
        id_pendiente = _id_por_nombre(
            conn, "SELECT id_estado FROM estados_orden WHERE nombre = :n", "Pendiente"
        )
        numero_dia = siguiente_numero_orden_diaria(conn)
        res = conn.execute(
            text(
                """
                INSERT INTO ordenes (id_tipo_orden, id_mesa, id_empleado_atiende,
                                     id_usuario_registro, id_plataforma_externa,
                                     referencia_externa, id_estado, total,
                                     numero_orden_diaria)
                VALUES (:tipo, :mesa, :empleado, :usuario, :plataforma, :ref_ext,
                        :estado, 0, :numero_dia)
                RETURNING id_orden, numero_orden_diaria
                """
            ),
            {
                "tipo": id_tipo_orden, "mesa": id_mesa, "empleado": id_empleado,
                "usuario": id_usuario, "plataforma": id_plataforma,
                "ref_ext": referencia_externa or None, "estado": id_pendiente,
                "numero_dia": numero_dia,
            },
        )
        fila_orden = res.mappings().one()
        id_orden = int(fila_orden["id_orden"])
        numero_dia = int(fila_orden["numero_orden_diaria"])

        total, resumen = agregar_partidas(
            conn, id_orden=id_orden, partidas=partidas, id_empleado=id_empleado,
            id_usuario=id_usuario,
        )
        if total <= 0:
            raise VentaError("La comanda debe tener un total mayor a cero.")

    return {
        "id_orden": id_orden,
        "numero_dia": numero_dia,
        "total": total,
        "inventario": resumen,
    }


def guardar_orden_mesa(
    *,
    id_mesa: int,
    partidas: list[dict],
    id_usuario: int,
    id_empleado: int | None,
) -> dict:
    """
    Orden de mesa (cuenta abierta): reutiliza la orden no finalizada de la
    mesa o crea una nueva en 'Pendiente', marca la mesa 'Ocupada', agrega las
    partidas y descuenta inventario. Todo en una transacción.
    """
    with transaccion() as conn:
        existente = conn.execute(
            text(
                """
                SELECT o.id_orden, o.numero_orden_diaria
                FROM ordenes o
                JOIN estados_orden e ON e.id_estado = o.id_estado
                WHERE o.id_mesa = :m AND e.es_final = FALSE
                ORDER BY o.id_orden DESC LIMIT 1
                FOR UPDATE
                """
            ),
            {"m": id_mesa},
        ).mappings().first()

        if existente is None:
            id_local = _id_por_nombre(
                conn, "SELECT id_tipo_orden FROM tipos_orden WHERE nombre = :n", "Local"
            )
            id_pendiente = _id_por_nombre(
                conn, "SELECT id_estado FROM estados_orden WHERE nombre = :n", "Pendiente"
            )
            numero_dia = siguiente_numero_orden_diaria(conn)
            res = conn.execute(
                text(
                    """
                    INSERT INTO ordenes (id_tipo_orden, id_mesa, id_empleado_atiende,
                                         id_usuario_registro, id_estado, total,
                                         numero_orden_diaria)
                    VALUES (:tipo, :mesa, :empleado, :usuario, :estado, 0, :numero_dia)
                    RETURNING id_orden, numero_orden_diaria
                    """
                ),
                {"tipo": id_local, "mesa": id_mesa, "empleado": id_empleado,
                 "usuario": id_usuario, "estado": id_pendiente,
                 "numero_dia": numero_dia},
            )
            fila_orden = res.mappings().one()
            id_orden = int(fila_orden["id_orden"])
            numero_dia = int(fila_orden["numero_orden_diaria"])
        else:
            id_orden = int(existente["id_orden"])
            numero_dia = int(existente["numero_orden_diaria"])

        # La mesa debe quedar ocupada también al continuar una cuenta existente.
        conn.execute(
            text("UPDATE mesas SET estado = 'Ocupada' WHERE id_mesa = :m"),
            {"m": id_mesa},
        )

        total, resumen = agregar_partidas(
            conn, id_orden=id_orden, partidas=partidas, id_empleado=id_empleado,
            id_usuario=id_usuario,
        )

    return {
        "id_orden": id_orden,
        "numero_dia": numero_dia,
        "total": total,
        "inventario": resumen,
    }


def eliminar_partida(
    *,
    id_detalle: int,
    id_usuario: int,
    id_empleado: int | None,
) -> Decimal:
    """Elimina una partida Pendiente de una orden abierta y recalcula su total.

    Solo se permite mientras cocina/bar no hayan empezado la preparación.
    También devuelve al inventario los insumos que esa partida había
    descontado, para que la eliminación no deje consumos fantasma.
    """
    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT d.id_orden, d.id_producto, d.cantidad, o.id_mesa,
                       ed.nombre AS estado_detalle
                FROM detalles_orden d
                JOIN ordenes o ON o.id_orden = d.id_orden
                JOIN estados_orden e ON e.id_estado = o.id_estado
                JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
                WHERE d.id_detalle = :detalle AND e.es_final = FALSE
                FOR UPDATE OF d, o
                """
            ),
            {"detalle": id_detalle},
        ).mappings().first()
        if fila is None:
            raise VentaError("La partida no existe o la orden ya está cerrada.")
        if str(fila["estado_detalle"]) != "Pendiente":
            raise VentaError(
                "Este producto ya está en preparación; si se perdió, "
                "repórtalo como merma."
            )

        id_orden = int(fila["id_orden"])
        _revertir_inventario(
            conn,
            id_orden=id_orden,
            partida={"id_producto": int(fila["id_producto"]), "cantidad": int(fila["cantidad"])},
            id_empleado=id_empleado,
        )
        conn.execute(
            text("DELETE FROM detalles_orden WHERE id_detalle = :detalle"),
            {"detalle": id_detalle},
        )
        conn.execute(
            text(
                """
                DELETE FROM auditoria
                WHERE tabla_afectada = 'detalles_orden_comentario'
                  AND id_registro_afectado = :detalle
                """
            ),
            {"detalle": id_detalle},
        )

        total = recalcular_total_orden(conn, id_orden)

        # Si ya no quedan productos, se cancela la cuenta y se libera la mesa.
        if total <= 0:
            id_cancelada = _id_por_nombre(
                conn, "SELECT id_estado FROM estados_orden WHERE nombre = :n", "Cancelada"
            )
            conn.execute(
                text("UPDATE ordenes SET id_estado = :e WHERE id_orden = :o"),
                {"e": id_cancelada, "o": id_orden},
            )
            if fila["id_mesa"] is not None:
                conn.execute(
                    text("UPDATE mesas SET estado = 'Disponible' WHERE id_mesa = :m"),
                    {"m": int(fila["id_mesa"])},
                )

    return total


def modificar_cantidad_partida(
    id_detalle: int,
    nueva_cantidad: int,
    id_usuario: int,
    id_empleado: int | None,
) -> Decimal:
    """Cambia la cantidad de una partida Pendiente y ajusta su inventario.

    Solo se permite mientras cocina/bar no hayan empezado la preparación.
    La diferencia de consumo se calcula contra la receta vigente y se aplica
    en la misma transacción: si al subir la cantidad falta stock, hace ROLLBACK
    completo. Al bajar, devuelve los insumos con un movimiento de entrada.
    """
    try:
        nueva = int(nueva_cantidad)
    except (TypeError, ValueError) as exc:
        raise VentaError("La cantidad debe ser un número entero.") from exc
    if nueva < 1:
        raise VentaError("La cantidad debe ser al menos 1.")

    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT d.id_orden, d.id_producto, d.cantidad,
                       ed.nombre AS estado_detalle, e.es_final
                FROM detalles_orden d
                JOIN ordenes o ON o.id_orden = d.id_orden
                JOIN estados_orden e ON e.id_estado = o.id_estado
                JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
                WHERE d.id_detalle = :detalle
                FOR UPDATE OF d, o
                """
            ),
            {"detalle": int(id_detalle)},
        ).mappings().first()
        if fila is None or bool(fila["es_final"]):
            raise VentaError("La partida no existe o la orden ya está cerrada.")
        if str(fila["estado_detalle"]) != "Pendiente":
            raise VentaError(
                "Este producto ya está en preparación; si se perdió, "
                "repórtalo como merma."
            )

        id_orden = int(fila["id_orden"])
        id_producto = int(fila["id_producto"])
        actual = int(fila["cantidad"])
        diferencia = nueva - actual

        if diferencia > 0:
            descontar_inventario(
                conn,
                id_orden=id_orden,
                partidas=[{"id_producto": id_producto, "cantidad": diferencia}],
                id_empleado=id_empleado,
            )
        elif diferencia < 0:
            _revertir_inventario(
                conn,
                id_orden=id_orden,
                partida={
                    "id_producto": id_producto,
                    "cantidad": abs(diferencia),
                },
                id_empleado=id_empleado,
                motivo=(
                    "Reversa por reducción de cantidad en la orden "
                    f"#{id_orden}"
                ),
            )

        if diferencia != 0:
            conn.execute(
                text(
                    "UPDATE detalles_orden SET cantidad = :n "
                    "WHERE id_detalle = :detalle"
                ),
                {"n": nueva, "detalle": int(id_detalle)},
            )

        total = recalcular_total_orden(conn, id_orden)
    return total


def limpiar_comandas_listas() -> int:
    """Pasa todas las partidas en ``Listo`` a ``Entregado`` en una transacción."""
    with transaccion() as conn:
        id_listo = _id_por_nombre(
            conn,
            "SELECT id_estado FROM estados_detalle_orden WHERE nombre = :n",
            "Listo",
        )
        id_entregado = _id_por_nombre(
            conn,
            "SELECT id_estado FROM estados_detalle_orden WHERE nombre = :n",
            "Entregado",
        )
        resultado = conn.execute(
            text(
                "UPDATE detalles_orden SET id_estado = :entregado "
                "WHERE id_estado = :listo"
            ),
            {"entregado": id_entregado, "listo": id_listo},
        )
        return int(resultado.rowcount or 0)


def cobrar_orden_mesa(*, id_orden: int, id_mesa: int | None, id_metodo_pago: int,
                      id_usuario: int, referencia_pago: str | None = None) -> Decimal:
    """Cobra una cuenta abierta: pago + orden 'Pagada' + mesa 'Disponible' (atómico)."""
    with transaccion() as conn:
        orden = conn.execute(
            text(
                "SELECT id_estado, total FROM ordenes "
                "WHERE id_orden = :o FOR UPDATE"
            ),
            {"o": id_orden},
        ).mappings().first()
        if orden is None:
            raise VentaError("La orden no existe.")

        es_final = conn.execute(
            text("SELECT es_final FROM estados_orden WHERE id_estado = :e"),
            {"e": int(orden["id_estado"])},
        ).scalar_one()
        if bool(es_final):
            raise VentaError("La orden ya fue cobrada o cancelada.")

        total = dinero(orden["total"])
        if total <= 0:
            raise VentaError("La orden no tiene importe que cobrar.")
        bloqueantes = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM detalles_orden d
                    JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
                    WHERE d.id_orden = :o
                      AND ed.nombre NOT IN ('Listo', 'Entregado')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM auditoria a
                          WHERE a.tabla_afectada = 'detalles_orden_merma'
                            AND a.id_registro_afectado = d.id_detalle
                      )
                    """
                ),
                {"o": id_orden},
            ).scalar_one()
        )
        if bloqueantes > 0:
            raise VentaError(
                "No se puede cobrar: aún hay productos pendientes o en preparación. "
                "Cocina y bar deben marcarlos como Listo o Entregado."
            )
        id_pagada = _id_por_nombre(
            conn, "SELECT id_estado FROM estados_orden WHERE nombre = :n", "Pagada"
        )
        conn.execute(
            text(
                """
                INSERT INTO pagos (id_orden, id_metodo_pago, monto, referencia_pago, id_usuario_registro)
                VALUES (:o, :m, :monto, :ref, :u)
                """
            ),
            {"o": id_orden, "m": id_metodo_pago, "monto": total,
             "ref": referencia_pago or None, "u": id_usuario},
        )
        conn.execute(text("UPDATE ordenes SET id_estado = :e WHERE id_orden = :o"),
                     {"e": id_pagada, "o": id_orden})
        if id_mesa is not None:
            # La mesa solo se libera cuando ya no tiene otras cuentas abiertas.
            conn.execute(
                text(
                    """
                    UPDATE mesas
                    SET estado = 'Disponible'
                    WHERE id_mesa = :m
                      AND NOT EXISTS (
                          SELECT 1
                          FROM ordenes o
                          JOIN estados_orden e ON e.id_estado = o.id_estado
                          WHERE o.id_mesa = mesas.id_mesa
                            AND o.id_orden <> :orden
                            AND e.es_final = FALSE
                      )
                    """
                ),
                {"m": id_mesa, "orden": id_orden},
            )
    return total
