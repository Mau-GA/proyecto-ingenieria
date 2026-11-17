"""Existencia calculada por lote y reglas de caducidad.

Los lotes NO se descuentan individualmente al vender, mermar o ajustar. El
inventario sigue viviendo en ``insumos.stock_actual`` y aquí se reparte esa
existencia entre los lotes no desechados aplicando FEFO: se asume que primero
sale lo que caduca antes, por lo que el stock disponible cubre primero a los
lotes que caducan más tarde. Lo que exceda la suma de lotes es la existencia
inicial "Sin lote".

La fecha de hoy siempre se obtiene con ``CURRENT_DATE`` de PostgreSQL para que
la vista y los bloqueos no dependan del reloj del navegador.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from database import get_engine, transaccion

ESTADO_VIGENTE = "Vigente"
ESTADO_POR_VENCER = "Por vencer"
ESTADO_CADUCADO = "Caducado"
ESTADO_SIN_LOTE = "Sin lote"


def obtener_dias_aviso(conn: Connection | None = None) -> int:
    """Días de aviso configurados para lotes próximos a caducar."""
    sql = "SELECT dias_aviso_caducidad FROM configuracion_inventario WHERE id = 1"
    if conn is not None:
        valor = conn.execute(text(sql)).scalar_one_or_none()
    else:
        with get_engine().connect() as conexion:
            valor = conexion.execute(text(sql)).scalar_one_or_none()
    return 2 if valor is None else int(valor)


def actualizar_dias_aviso(
    dias: int, *, id_usuario: int | None = None
) -> None:
    """Guarda los días de aviso. ``id_usuario`` queda para trazabilidad futura."""
    dias = int(dias)
    if dias < 0:
        raise ValueError("Los días de aviso no pueden ser negativos.")
    with transaccion() as conn:
        conn.execute(
            text(
                """
                INSERT INTO configuracion_inventario (id, dias_aviso_caducidad)
                VALUES (1, :dias)
                ON CONFLICT (id) DO UPDATE
                SET dias_aviso_caducidad = EXCLUDED.dias_aviso_caducidad
                """
            ),
            {"dias": dias},
        )


def _estado(dias: int | None, dias_aviso: int) -> str:
    if dias is None:
        return ESTADO_SIN_LOTE
    if dias <= 0:
        return ESTADO_CADUCADO
    if dias <= dias_aviso:
        return ESTADO_POR_VENCER
    return ESTADO_VIGENTE


def existencias_por_lote(
    conn: Connection, ids_insumos: list[int] | None = None
) -> list[dict]:
    """Reparte ``stock_actual`` entre los lotes no desechados de cada insumo.

    Regresa una fila por lote y una fila adicional con ``id_lote = None`` y
    ``estado = 'Sin lote'`` para la existencia inicial que no proviene de una
    compra. Ordena los lotes del que caduca más tarde al que caduca más pronto
    para aplicar FEFO al reparto.
    """
    if ids_insumos is not None:
        ids_insumos = sorted({int(i) for i in ids_insumos})
        if not ids_insumos:
            return []

    filtro_ids = ""
    if ids_insumos is not None:
        filtro_ids = "AND i.id_insumo IN :ids"

    sql = f"""
        SELECT i.id_insumo, i.nombre AS insumo, i.stock_actual,
               u.abreviatura AS unidad,
               l.id_lote, l.cantidad_recibida, l.fecha_caducidad,
               CASE
                   WHEN l.fecha_caducidad IS NULL THEN NULL
                   ELSE l.fecha_caducidad - CURRENT_DATE
               END AS dias_para_caducar
        FROM insumos i
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        LEFT JOIN lotes_insumo l
          ON l.id_insumo = i.id_insumo AND l.desechado = FALSE
        WHERE 1 = 1 {filtro_ids}
        ORDER BY i.id_insumo,
                 l.fecha_caducidad DESC NULLS FIRST,
                 l.id_lote DESC
    """
    consulta = text(sql)
    params: dict = {}
    if ids_insumos is not None:
        consulta = consulta.bindparams(bindparam("ids", expanding=True))
        params["ids"] = ids_insumos

    filas = conn.execute(consulta, params).mappings().all()
    dias_aviso = obtener_dias_aviso(conn)

    agrupados: dict[int, dict] = {}
    for fila in filas:
        id_insumo = int(fila["id_insumo"])
        grupo = agrupados.setdefault(
            id_insumo,
            {
                "insumo": str(fila["insumo"]),
                "unidad": str(fila["unidad"]),
                "stock_actual": Decimal(str(fila["stock_actual"])),
                "lotes": [],
            },
        )
        if fila["id_lote"] is not None:
            grupo["lotes"].append(fila)

    resultado: list[dict] = []
    for id_insumo in sorted(agrupados):
        grupo = agrupados[id_insumo]
        restante = grupo["stock_actual"]

        for fila in grupo["lotes"]:
            recibida = Decimal(str(fila["cantidad_recibida"]))
            asignada = min(max(restante, Decimal("0")), recibida)
            restante -= asignada
            dias = (
                None
                if fila["dias_para_caducar"] is None
                else int(fila["dias_para_caducar"])
            )
            resultado.append(
                {
                    "id_insumo": id_insumo,
                    "insumo": grupo["insumo"],
                    "unidad": grupo["unidad"],
                    "id_lote": int(fila["id_lote"]),
                    "fecha_caducidad": fila["fecha_caducidad"],
                    "cantidad_recibida": recibida,
                    "cantidad_restante": asignada,
                    "dias_para_caducar": dias,
                    "estado": _estado(dias, dias_aviso),
                }
            )

        sin_lote = max(restante, Decimal("0"))
        resultado.append(
            {
                "id_insumo": id_insumo,
                "insumo": grupo["insumo"],
                "unidad": grupo["unidad"],
                "id_lote": None,
                "fecha_caducidad": None,
                "cantidad_recibida": None,
                "cantidad_restante": sin_lote,
                "dias_para_caducar": None,
                "estado": ESTADO_SIN_LOTE,
            }
        )
    return resultado


def stock_utilizable_por_insumo(
    conn: Connection, ids_insumos: list[int]
) -> dict[int, Decimal]:
    """Stock que puede usarse: total menos lo atrapado en lotes caducados."""
    ids = sorted({int(i) for i in ids_insumos})
    if not ids:
        return {}

    stock = {
        int(fila[0]): Decimal(str(fila[1]))
        for fila in conn.execute(
            text(
                "SELECT id_insumo, stock_actual FROM insumos "
                "WHERE id_insumo IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all()
    }
    bloqueado: dict[int, Decimal] = {i: Decimal("0") for i in ids}
    for fila in existencias_por_lote(conn, ids):
        if fila["estado"] == ESTADO_CADUCADO and fila["id_lote"] is not None:
            id_insumo = int(fila["id_insumo"])
            bloqueado[id_insumo] += Decimal(str(fila["cantidad_restante"]))

    return {
        id_insumo: max(
            stock.get(id_insumo, Decimal("0")) - bloqueado[id_insumo],
            Decimal("0"),
        )
        for id_insumo in ids
    }


def stock_utilizable(conn: Connection, id_insumo: int) -> Decimal:
    """Versión de una sola fila de :func:`stock_utilizable_por_insumo`."""
    return stock_utilizable_por_insumo(conn, [int(id_insumo)]).get(
        int(id_insumo), Decimal("0")
    )


def lotes_en_alerta(
    conn: Connection | None = None, ids_insumos: list[int] | None = None
) -> list[dict]:
    """Lotes caducados o por vencer, con cantidad restante mayor a cero."""
    if conn is None:
        with get_engine().connect() as conexion:
            filas = existencias_por_lote(conexion, ids_insumos)
    else:
        filas = existencias_por_lote(conn, ids_insumos)
    return [
        fila
        for fila in filas
        if fila["id_lote"] is not None
        and fila["estado"] in (ESTADO_CADUCADO, ESTADO_POR_VENCER)
        and Decimal(str(fila["cantidad_restante"])) > 0
    ]
