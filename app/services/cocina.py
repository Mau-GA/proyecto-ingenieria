"""
Lógica de negocio del KDS (Comandas) sin interfaz de Streamlit.

Centraliza dos responsabilidades:

1. El cambio de estado de una partida respetando la secuencia
   ``Pendiente -> En preparación -> Listo -> Entregado``. El UPDATE es
   condicional al estado que la pantalla está mostrando, de modo que si otro
   cocinero cambió la partida primero, el segundo recibe un error claro en
   lugar de sobrescribir el trabajo ajeno. El cambio y su fila de historial
   se guardan en la MISMA transacción.

2. El semáforo de tiempos de espera del KDS y la configuración de sus
   umbrales (tabla ``configuracion_kds``).

Las horas siempre las pone PostgreSQL (``CURRENT_TIMESTAMP``), nunca el
reloj del navegador ni el de Python, para que todos los equipos coincidan.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import run_query, transaccion

# Nombres en estados_detalle_orden (se leen del catálogo, no se fijan ids).
SECUENCIA_ESTADOS = ("Pendiente", "En preparación", "Listo", "Entregado")
MENSAJE_CONFLICTO = (
    "Otro usuario ya actualizó este producto; la pantalla se refrescó."
)


class CocinaError(Exception):
    """Error de negocio del KDS; la UI muestra su mensaje al usuario."""


# =====================================================================
# Historial de estados
# =====================================================================
def registrar_historial_pendiente(
    conn: Connection,
    *,
    id_detalle: int,
    id_usuario: int | None = None,
    id_estado: int | None = None,
) -> None:
    """Inserta la fila inicial ``Pendiente`` de una partida recién creada.

    Debe llamarse dentro de la transacción que inserta la partida, para que
    la hora de recepción en cocina no se pierda si la venta hace ROLLBACK.
    """
    if id_estado is None:
        id_estado = conn.execute(
            text(
                "SELECT id_estado FROM estados_detalle_orden "
                "WHERE nombre = :nombre"
            ),
            {"nombre": "Pendiente"},
        ).scalar_one_or_none()
        if id_estado is None:
            raise CocinaError(
                "Falta el estado 'Pendiente' en estados_detalle_orden. "
                "Revisa db/seed.sql."
            )

    conn.execute(
        text(
            """
            INSERT INTO historial_estado_detalle
                (id_detalle, id_estado, id_usuario)
            VALUES (:detalle, :estado, :usuario)
            """
        ),
        {
            "detalle": int(id_detalle),
            "estado": int(id_estado),
            "usuario": id_usuario,
        },
    )


def _insertar_historial(
    conn: Connection,
    *,
    id_detalle: int,
    id_estado: int,
    id_usuario: int | None,
) -> None:
    """Agrega una fila de historial con la hora del servidor de base de datos."""
    conn.execute(
        text(
            """
            INSERT INTO historial_estado_detalle
                (id_detalle, id_estado, id_usuario)
            VALUES (:detalle, :estado, :usuario)
            """
        ),
        {
            "detalle": int(id_detalle),
            "estado": int(id_estado),
            "usuario": id_usuario,
        },
    )


# =====================================================================
# Cambio de estado con control de secuencia y concurrencia
# =====================================================================
def _validar_secuencia(estado_esperado: str, estado_nuevo: str) -> None:
    """Rechaza saltos y retrocesos comparando los nombres del catálogo."""
    if estado_esperado not in SECUENCIA_ESTADOS:
        raise CocinaError(
            f"El estado actual '{estado_esperado}' no forma parte de la "
            "secuencia de preparación."
        )
    if estado_nuevo not in SECUENCIA_ESTADOS:
        raise CocinaError(
            f"El estado nuevo '{estado_nuevo}' no forma parte de la "
            "secuencia de preparación."
        )

    indice_actual = SECUENCIA_ESTADOS.index(estado_esperado)
    indice_nuevo = SECUENCIA_ESTADOS.index(estado_nuevo)
    if indice_nuevo == indice_actual + 1:
        return
    if indice_nuevo <= indice_actual:
        raise CocinaError(
            f"No se puede regresar una partida de '{estado_esperado}' a "
            f"'{estado_nuevo}'."
        )
    raise CocinaError(
        f"No se puede saltar de '{estado_esperado}' a '{estado_nuevo}'; "
        "avanza un estado a la vez."
    )


def cambiar_estado_partida(
    id_detalle: int,
    estado_esperado: str,
    estado_nuevo: str,
    id_usuario: int | None,
) -> None:
    """Cambia el estado de una partida y registra quién lo hizo.

    ``estado_esperado`` es el estado que la pantalla mostró al usuario. El
    UPDATE incluye esa condición: si otra persona ya movió la partida, no se
    actualiza ninguna fila y se lanza ``CocinaError``. El UPDATE y la fila de
    historial se confirman juntos o se revierten juntos.
    """
    _validar_secuencia(estado_esperado, estado_nuevo)

    with transaccion() as conn:
        fila = conn.execute(
            text(
                """
                SELECT d.id_estado, ed.nombre AS estado_actual,
                       eo.nombre AS estado_orden, eo.es_final,
                       EXISTS (
                           SELECT 1
                           FROM auditoria a
                           WHERE a.tabla_afectada = 'detalles_orden_merma'
                             AND a.id_registro_afectado = d.id_detalle
                       ) AS mermada
                FROM detalles_orden d
                JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
                JOIN ordenes o ON o.id_orden = d.id_orden
                JOIN estados_orden eo ON eo.id_estado = o.id_estado
                WHERE d.id_detalle = :detalle
                """
            ),
            {"detalle": int(id_detalle)},
        ).mappings().first()

        if fila is None:
            raise CocinaError("La partida ya no existe.")

        if bool(fila["es_final"]):
            raise CocinaError(
                f"La orden ya está en estado '{fila['estado_orden']}'; "
                "no se puede cambiar la preparación."
            )
        if bool(fila["mermada"]):
            raise CocinaError(
                "Esta partida está reportada como merma; no se puede cambiar "
                "su estado de preparación."
            )
        if str(fila["estado_actual"]) != estado_esperado:
            raise CocinaError(MENSAJE_CONFLICTO)

        id_esperado = int(fila["id_estado"])
        id_nuevo = conn.execute(
            text(
                "SELECT id_estado FROM estados_detalle_orden "
                "WHERE nombre = :nombre"
            ),
            {"nombre": estado_nuevo},
        ).scalar_one_or_none()
        if id_nuevo is None:
            raise CocinaError(
                f"Falta el estado '{estado_nuevo}' en estados_detalle_orden."
            )

        # Condición doble: id_estado esperado y que la partida siga activa.
        # Si alguien más ya la movió (o la mermó), rowcount será 0.
        resultado = conn.execute(
            text(
                """
                UPDATE detalles_orden
                SET id_estado = :nuevo
                WHERE id_detalle = :detalle
                  AND id_estado = :esperado
                  AND NOT EXISTS (
                      SELECT 1
                      FROM auditoria a
                      WHERE a.tabla_afectada = 'detalles_orden_merma'
                        AND a.id_registro_afectado = detalles_orden.id_detalle
                  )
                """
            ),
            {
                "nuevo": int(id_nuevo),
                "detalle": int(id_detalle),
                "esperado": id_esperado,
            },
        )
        if int(resultado.rowcount or 0) != 1:
            raise CocinaError(MENSAJE_CONFLICTO)

        _insertar_historial(
            conn,
            id_detalle=int(id_detalle),
            id_estado=int(id_nuevo),
            id_usuario=id_usuario,
        )


# =====================================================================
# Configuración de umbrales y semáforo de espera
# =====================================================================
def validar_umbrales_kds(
    umbral_amarillo_min: int, umbral_rojo_min: int
) -> tuple[int, int]:
    """Valida los umbrales y los regresa como enteros positivos."""
    try:
        amarillo = int(umbral_amarillo_min)
        rojo = int(umbral_rojo_min)
    except (TypeError, ValueError) as exc:
        raise CocinaError("Los umbrales deben ser números enteros de minutos.") from exc

    if amarillo <= 0 or rojo <= 0:
        raise CocinaError("Los umbrales deben ser mayores a cero.")
    if rojo <= amarillo:
        raise CocinaError(
            "El umbral rojo debe ser mayor que el umbral amarillo."
        )
    return amarillo, rojo


def obtener_configuracion_kds() -> dict:
    """Lee la fila única de configuración del KDS."""
    df = run_query(
        "SELECT umbral_amarillo_min, umbral_rojo_min "
        "FROM configuracion_kds WHERE id_configuracion = 1"
    )
    if df.empty:
        raise CocinaError(
            "No existe la configuración del KDS. Recrea la base de datos con "
            "db/schema.sql y db/seed.sql."
        )
    fila = df.iloc[0]
    return {
        "umbral_amarillo_min": int(fila["umbral_amarillo_min"]),
        "umbral_rojo_min": int(fila["umbral_rojo_min"]),
    }


def guardar_configuracion_kds(
    umbral_amarillo_min: int, umbral_rojo_min: int
) -> None:
    """Actualiza (o crea, si faltara) la fila única de configuración."""
    amarillo, rojo = validar_umbrales_kds(
        umbral_amarillo_min, umbral_rojo_min
    )
    with transaccion() as conn:
        resultado = conn.execute(
            text(
                """
                UPDATE configuracion_kds
                SET umbral_amarillo_min = :amarillo,
                    umbral_rojo_min = :rojo
                WHERE id_configuracion = 1
                """
            ),
            {"amarillo": amarillo, "rojo": rojo},
        )
        if int(resultado.rowcount or 0) == 0:
            conn.execute(
                text(
                    """
                    INSERT INTO configuracion_kds
                        (id_configuracion, umbral_amarillo_min, umbral_rojo_min)
                    VALUES (1, :amarillo, :rojo)
                    """
                ),
                {"amarillo": amarillo, "rojo": rojo},
            )


def color_por_espera(
    minutos_transcurridos: float,
    umbral_amarillo_min: int,
    umbral_rojo_min: int,
) -> str:
    """Devuelve ``verde``, ``amarillo`` o ``rojo`` según los umbrales.

    Los umbrales se validan aquí también para que una configuración inválida
    nunca pinte una tarjeta con un color engañoso.
    """
    amarillo, rojo = validar_umbrales_kds(
        umbral_amarillo_min, umbral_rojo_min
    )
    minutos = float(minutos_transcurridos)
    if minutos < amarillo:
        return "verde"
    if minutos < rojo:
        return "amarillo"
    return "rojo"
