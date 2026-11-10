"""
Ruta: proyectorestaurante/app/database.py

Capa de acceso a datos con SQLAlchemy 2.x + PyMySQL.

- Lee la conexión de variables de entorno (inyectadas por docker-compose.yml,
  o desde un archivo .env en desarrollo local gracias a python-dotenv).
- Mantiene UN solo Engine (pool de conexiones) por proceso: Streamlit
  re-ejecuta el script en cada interacción, así que se cachea con
  st.cache_resource para no abrir conexiones nuevas en cada clic.
- Expone la misma API que ya usan los módulos (run_query, execute,
  execute_many) más `transaccion()` para operaciones de varias sentencias
  que deben confirmarse o revertirse juntas (p. ej. registrar una venta).

Regla: SIEMPRE parámetros nombrados (:nombre) en el SQL, nunca f-strings
con datos del usuario, para evitar inyección SQL.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

# Carga .env si existe (desarrollo local). En Docker no hay .env dentro del
# contenedor y las variables ya vienen del entorno: no falla.
# override=False -> las variables reales del entorno tienen prioridad.
load_dotenv(override=False)


# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------
def _env(nombre: str, defecto: str | None = None) -> str:
    """Lee una variable de entorno; lanza error claro si falta y no hay defecto."""
    valor = os.getenv(nombre, defecto)
    if valor is None or valor == "":
        raise RuntimeError(
            f"Falta la variable de entorno {nombre}. "
            "Revisa tu archivo .env (cópialo de .env.example)."
        )
    return valor


def construir_url() -> URL:
    """
    Construye la URL de conexión. URL.create escapa automáticamente
    caracteres especiales de la contraseña (@, :, /, #...), cosa que un
    f-string NO hace y que rompe la conexión de forma confusa.
    """
    return URL.create(
        drivername="mysql+pymysql",
        username=_env("MYSQL_USER"),
        password=_env("MYSQL_PASSWORD"),
        host=_env("MYSQL_HOST", "db"),          # nombre del servicio en Docker
        port=int(_env("MYSQL_PORT", "3306")),
        database=_env("MYSQL_DATABASE"),
        query={"charset": "utf8mb4"},
    )


# ---------------------------------------------------------------------
# Engine (pool de conexiones) cacheado por proceso
# ---------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_engine() -> Engine:
    return create_engine(
        construir_url(),
        pool_size=5,            # conexiones permanentes en el pool
        max_overflow=10,        # conexiones extra en picos de uso
        pool_pre_ping=True,     # detecta conexiones muertas (reinicio de la BD)
        pool_recycle=280,       # recicla antes del wait_timeout del servidor
        connect_args={"connect_timeout": 10},
        future=True,
    )


def verificar_conexion() -> tuple[bool, str]:
    """Prueba la conexión. Regresa (ok, mensaje) para mostrarlo en la UI."""
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar_one()
        return True, f"Conectado a MariaDB {version}"
    except (SQLAlchemyError, RuntimeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------
# API de consultas (compatible con los módulos existentes)
# ---------------------------------------------------------------------
def run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
    """SELECT que regresa un DataFrame."""
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def execute(sql: str, params: dict | None = None) -> int:
    """
    INSERT/UPDATE/DELETE en su propia transacción (commit automático).
    Regresa lastrowid (id autogenerado en INSERT; 0 en UPDATE/DELETE).
    """
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params or {})
        return result.lastrowid


def execute_many(sql: str, params_list: list[dict]) -> None:
    """Mismo statement con varios juegos de parámetros en UNA transacción."""
    if not params_list:
        return
    with get_engine().begin() as conn:
        conn.execute(text(sql), params_list)   # executemany nativo del driver


@contextmanager
def transaccion() -> Iterator[Connection]:
    """
    Bloque transaccional para operaciones de varias sentencias:

        with transaccion() as conn:
            r = conn.execute(text("INSERT ..."), {...})
            conn.execute(text("UPDATE ..."), {...})

    Si cualquier sentencia lanza excepción se hace ROLLBACK de todo;
    si el bloque termina bien se hace COMMIT.
    """
    with get_engine().begin() as conn:
        yield conn
