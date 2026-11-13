"""Configuración de pruebas: crea una BD PostgreSQL aislada y recarga el seed.

Las pruebas nunca usan la base principal: crean ``<MYSQL_DATABASE>_test`` y
reemplazan su esquema antes de cada test. Requieren que el contenedor ``db``
esté activo y que los scripts ``db/schema.sql`` y ``db/seed.sql`` estén
disponibles (en Docker se monta ``./db`` junto con ``./tests``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[1]
load_dotenv(RAIZ / ".env", override=False)

# El código vive en app/ (host) o directamente en /app (contenedor).
for candidato in (RAIZ / "app", RAIZ):
    if (candidato / "database.py").exists():
        sys.path.insert(0, str(candidato))
        break

DIR_DB = RAIZ / "db"
BD_BASE = os.environ.get("MYSQL_DATABASE", "proyectorestaurante")
BD_TEST = f"{BD_BASE}_test"
HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PUERTO = int(
    os.environ.get("MYSQL_PORT")
    or os.environ.get("MYSQL_EXPOSED_PORT")
    or "5432"
)
USUARIO = os.environ["MYSQL_USER"]
PASSWORD = os.environ["MYSQL_PASSWORD"]


def _conectar(bd: str):
    return psycopg2.connect(
        host=HOST,
        port=PUERTO,
        user=USUARIO,
        password=PASSWORD,
        dbname=bd,
    )


def _ejecutar_archivo(conn, ruta: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(ruta.read_text(encoding="utf-8"))
    conn.commit()


def _reiniciar_esquema() -> None:
    with _conectar(BD_TEST) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        conn.commit()
        _ejecutar_archivo(conn, DIR_DB / "schema.sql")
        _ejecutar_archivo(conn, DIR_DB / "seed.sql")


@pytest.fixture(scope="session", autouse=True)
def base_de_pruebas():
    try:
        admin = _conectar("postgres")
    except psycopg2.Error as exc:  # pragma: no cover - diagnóstico
        pytest.exit(f"No se pudo conectar a PostgreSQL para las pruebas: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{BD_TEST}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{BD_TEST}" WITH ENCODING \'UTF8\'')
    admin.close()

    os.environ["MYSQL_DATABASE"] = BD_TEST
    yield
    try:
        from database import get_engine

        get_engine().dispose()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def bd_limpia():
    _reiniciar_esquema()
    yield
