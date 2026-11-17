"""
Ruta: proyectorestaurante/app/services/bitacora.py

Registro y verificación de la bitácora inalterable.

La tabla `bitacora` calcula su hash dentro de PostgreSQL (trigger
`trg_bitacora_hash`), de modo que la aplicación nunca decide el contenido del
hash. Este módulo solo arma el evento, toma el nombre del usuario y lo inserta
en la misma transacción que la operación de negocio.

Reglas:
- No guardar contraseñas, hashes de contraseña ni secretos en los datos.
- Toda escritura de negocio que enumera el proyecto debe llamar a
  `registrar_evento` dentro de la misma transacción (`transaccion()`).
- La bitácora no se actualiza ni se borra: los triggers lo impiden.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import run_query, transaccion

TIPOS_EVENTO = {
    "LOGIN",
    "LOGIN_FALLIDO",
    "LOGOUT",
    "PAGO",
    "CANCELACION",
    "AJUSTE_INVENTARIO",
    "MERMA",
    "COMPRA",
    "CAMBIO_PRECIO",
    "PROVEEDOR",
    "USUARIO",
}


def _texto_json(valor: Any) -> str | None:
    """
    Serializa un dict a JSON estable para JSONB.

    `default=str` convierte Decimal, fechas y otros tipos que PostgreSQL
    aceptaría pero `json.dumps` no conoce. No se usa para secretos.
    """
    if valor is None:
        return None
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)


def _insertar_evento(
    conn: Connection,
    *,
    tipo_evento: str,
    entidad: str,
    id_entidad: int | None,
    id_usuario: int | None,
    datos_anteriores: dict | None,
    datos_nuevos: dict | None,
) -> int:
    if tipo_evento not in TIPOS_EVENTO:
        raise ValueError(f"Tipo de evento no permitido: {tipo_evento}")

    nombre_usuario = None
    if id_usuario is not None:
        nombre_usuario = conn.execute(
            text("SELECT nombre_usuario FROM usuarios WHERE id_usuario = :id"),
            {"id": id_usuario},
        ).scalar()

    fila = conn.execute(
        text(
            """
            INSERT INTO bitacora
                (id_usuario, nombre_usuario, tipo_evento, entidad, id_entidad,
                 datos_anteriores, datos_nuevos)
            VALUES
                (:id_usuario, :nombre_usuario, :tipo_evento, :entidad,
                 :id_entidad, CAST(:anteriores AS jsonb), CAST(:nuevos AS jsonb))
            RETURNING id_bitacora
            """
        ),
        {
            "id_usuario": id_usuario,
            "nombre_usuario": nombre_usuario,
            "tipo_evento": tipo_evento,
            "entidad": entidad,
            "id_entidad": id_entidad,
            "anteriores": _texto_json(datos_anteriores),
            "nuevos": _texto_json(datos_nuevos),
        },
    ).scalar_one()
    return int(fila)


def registrar_evento(
    conn: Connection | None,
    *,
    tipo_evento: str,
    entidad: str,
    id_entidad: int | None = None,
    id_usuario: int | None = None,
    datos_anteriores: dict | None = None,
    datos_nuevos: dict | None = None,
) -> int:
    """
    Inserta un evento en la bitácora.

    Si `conn` es una conexión abierta, participa de esa transacción (lo normal
    en ventas, compras, inventario, cancelaciones, etc.). Si es `None`, abre
    su propia transacción y confirma al terminar (útil para login/logout o
    acciones aisladas).

    Regresa el `id_bitacora` generado.
    """
    if conn is not None:
        return _insertar_evento(
            conn,
            tipo_evento=tipo_evento,
            entidad=entidad,
            id_entidad=id_entidad,
            id_usuario=id_usuario,
            datos_anteriores=datos_anteriores,
            datos_nuevos=datos_nuevos,
        )

    with transaccion() as conexion:
        return _insertar_evento(
            conexion,
            tipo_evento=tipo_evento,
            entidad=entidad,
            id_entidad=id_entidad,
            id_usuario=id_usuario,
            datos_anteriores=datos_anteriores,
            datos_nuevos=datos_nuevos,
        )


def verificar_bitacora() -> int | None:
    """
    Recalcula la cadena completa y regresa el `id_bitacora` del primer
    registro alterado, o `None` si la cadena está intacta.

    La fórmula es idéntica a la del trigger; se evalúa en PostgreSQL para que
    el formato de fecha y de JSONB coincida exactamente.
    """
    filas = run_query(
        """
        WITH calculada AS (
            SELECT
                id_bitacora,
                hash,
                hash_anterior,
                lag(hash) OVER (ORDER BY id_bitacora) AS hash_previo,
                encode(
                    sha256(
                        convert_to(
                            COALESCE(hash_anterior, '') || '|' ||
                            id_bitacora::text || '|' ||
                            to_char(fecha, 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' ||
                            COALESCE(id_usuario::text, '') || '|' ||
                            COALESCE(nombre_usuario, '') || '|' ||
                            tipo_evento || '|' ||
                            entidad || '|' ||
                            COALESCE(id_entidad::text, '') || '|' ||
                            COALESCE(datos_anteriores::text, '') || '|' ||
                            COALESCE(datos_nuevos::text, ''),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS hash_calculado
            FROM bitacora
        )
        SELECT id_bitacora
        FROM calculada
        WHERE hash IS DISTINCT FROM hash_calculado
           OR hash_anterior IS DISTINCT FROM hash_previo
        ORDER BY id_bitacora
        LIMIT 1
        """
    )
    if filas.empty:
        return None
    return int(filas.iloc[0]["id_bitacora"])
