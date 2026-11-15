"""Reglas de negocio de los modificadores de productos.

Un modificador permite quitar un insumo de la receta, agregar un extra o
sustituir un insumo por otro. El catálogo vive en
``modificadores_producto``; cada partida guarda una foto histórica en
``detalle_modificadores`` para que editar el catálogo después no altere
ventas pasadas.

Todas las escrituras se hacen dentro de ``transaccion()``.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from database import get_engine, transaccion

TIPOS = ("Quitar", "Extra", "Sustituir")
CERO = Decimal("0")


class ModificadorError(Exception):
    """Error de negocio con un mensaje apto para mostrar en la interfaz."""


def _decimal_cantidad(valor) -> Decimal:
    try:
        cantidad = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModificadorError("La cantidad debe ser un número válido.") from exc
    if not cantidad.is_finite() or cantidad <= 0:
        raise ModificadorError("La cantidad debe ser mayor que cero.")
    return cantidad.quantize(Decimal("0.001"))


def _decimal_precio(valor) -> Decimal:
    try:
        precio = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModificadorError("El precio extra debe ser un número válido.") from exc
    if not precio.is_finite() or precio < 0:
        raise ModificadorError("El precio extra no puede ser negativo.")
    return precio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _validar_tipo(tipo: str) -> str:
    tipo = (tipo or "").strip().capitalize()
    if tipo not in TIPOS:
        raise ModificadorError("El tipo debe ser Quitar, Extra o Sustituir.")
    return tipo


def _validar_producto(conn: Connection, id_producto: int) -> None:
    existe = conn.execute(
        text(
            "SELECT 1 FROM productos "
            "WHERE id_producto = :p AND activo = TRUE"
        ),
        {"p": int(id_producto)},
    ).scalar()
    if not existe:
        raise ModificadorError("El producto seleccionado no existe o está inactivo.")


def _validar_insumo(conn: Connection, id_insumo: int) -> None:
    existe = conn.execute(
        text(
            "SELECT 1 FROM insumos "
            "WHERE id_insumo = :i AND activo = TRUE"
        ),
        {"i": int(id_insumo)},
    ).scalar()
    if not existe:
        raise ModificadorError("El insumo seleccionado no existe o está inactivo.")


def _cantidad_en_receta(
    conn: Connection, *, id_producto: int, id_insumo: int
) -> Decimal | None:
    valor = conn.execute(
        text(
            "SELECT cantidad_usada FROM recetas "
            "WHERE id_producto = :p AND id_insumo = :i"
        ),
        {"p": int(id_producto), "i": int(id_insumo)},
    ).scalar()
    return None if valor is None else Decimal(str(valor))


def _normalizar_datos(
    conn: Connection,
    *,
    id_producto: int,
    tipo: str,
    nombre: str,
    id_insumo: int,
    id_insumo_nuevo: int | None,
    cantidad,
    precio_extra,
) -> dict:
    """Valida las reglas por tipo y regresa los valores normalizados."""
    tipo = _validar_tipo(tipo)
    nombre_limpio = (nombre or "").strip()
    if not nombre_limpio:
        raise ModificadorError("El nombre del modificador es obligatorio.")

    _validar_insumo(conn, id_insumo)
    id_insumo = int(id_insumo)
    nuevo = None if id_insumo_nuevo in (None, "") else int(id_insumo_nuevo)
    precio = _decimal_precio(precio_extra)

    if tipo == "Quitar":
        if nuevo is not None:
            raise ModificadorError("Un modificador Quitar no lleva insumo nuevo.")
        if cantidad not in (None, ""):
            raise ModificadorError("Un modificador Quitar no lleva cantidad.")
        if _cantidad_en_receta(
            conn, id_producto=id_producto, id_insumo=id_insumo
        ) is None:
            raise ModificadorError(
                "El insumo a quitar no está en la receta del producto."
            )
        cantidad_dec = None
    else:
        if cantidad in (None, ""):
            raise ModificadorError(
                "La cantidad es obligatoria para Extra y Sustituir."
            )
        cantidad_dec = _decimal_cantidad(cantidad)
        if tipo == "Extra":
            if nuevo is not None:
                raise ModificadorError("Un modificador Extra no lleva insumo nuevo.")
        else:
            if nuevo is None:
                raise ModificadorError(
                    "Un modificador Sustituir requiere el insumo nuevo."
                )
            if nuevo == id_insumo:
                raise ModificadorError(
                    "El insumo nuevo debe ser distinto del insumo original."
                )
            _validar_insumo(conn, nuevo)
            if _cantidad_en_receta(
                conn, id_producto=id_producto, id_insumo=id_insumo
            ) is None:
                raise ModificadorError(
                    "El insumo a sustituir no está en la receta del producto."
                )

    return {
        "id_producto": int(id_producto),
        "tipo": tipo,
        "nombre": nombre_limpio,
        "id_insumo": id_insumo,
        "id_insumo_nuevo": nuevo,
        "cantidad": cantidad_dec,
        "precio_extra": precio,
    }


def _es_duplicado(exc: IntegrityError) -> bool:
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "23505"


def crear_modificador(
    *,
    id_producto: int,
    tipo: str,
    nombre: str,
    id_insumo: int,
    id_insumo_nuevo: int | None = None,
    cantidad=None,
    precio_extra=0,
) -> int:
    """Crea un modificador activo y regresa su id."""
    with transaccion() as conn:
        _validar_producto(conn, id_producto)
        datos = _normalizar_datos(
            conn,
            id_producto=id_producto,
            tipo=tipo,
            nombre=nombre,
            id_insumo=id_insumo,
            id_insumo_nuevo=id_insumo_nuevo,
            cantidad=cantidad,
            precio_extra=precio_extra,
        )
        try:
            id_modificador = conn.execute(
                text(
                    """
                    INSERT INTO modificadores_producto
                        (id_producto, tipo, nombre, id_insumo,
                         id_insumo_nuevo, cantidad, precio_extra, activo)
                    VALUES
                        (:producto, :tipo, :nombre, :insumo,
                         :nuevo, :cantidad, :precio, TRUE)
                    RETURNING id_modificador
                    """
                ),
                {
                    "producto": datos["id_producto"],
                    "tipo": datos["tipo"],
                    "nombre": datos["nombre"],
                    "insumo": datos["id_insumo"],
                    "nuevo": datos["id_insumo_nuevo"],
                    "cantidad": datos["cantidad"],
                    "precio": datos["precio_extra"],
                },
            ).scalar_one()
        except IntegrityError as exc:
            if _es_duplicado(exc):
                raise ModificadorError(
                    "Ya existe un modificador con ese nombre para el producto."
                ) from exc
            raise ModificadorError(
                "No se pudo crear el modificador; revisa los datos."
            ) from exc
    return int(id_modificador)


def actualizar_modificador(
    id_modificador: int,
    *,
    nombre: str,
    id_insumo: int,
    id_insumo_nuevo: int | None = None,
    cantidad=None,
    precio_extra=0,
) -> None:
    """Edita nombre, insumo, cantidad y precio; el tipo no cambia."""
    with transaccion() as conn:
        actual = conn.execute(
            text(
                "SELECT id_producto, tipo FROM modificadores_producto "
                "WHERE id_modificador = :m FOR UPDATE"
            ),
            {"m": int(id_modificador)},
        ).mappings().first()
        if actual is None:
            raise ModificadorError("El modificador seleccionado no existe.")

        datos = _normalizar_datos(
            conn,
            id_producto=int(actual["id_producto"]),
            tipo=str(actual["tipo"]),
            nombre=nombre,
            id_insumo=id_insumo,
            id_insumo_nuevo=id_insumo_nuevo,
            cantidad=cantidad,
            precio_extra=precio_extra,
        )
        try:
            conn.execute(
                text(
                    """
                    UPDATE modificadores_producto
                    SET nombre = :nombre, id_insumo = :insumo,
                        id_insumo_nuevo = :nuevo, cantidad = :cantidad,
                        precio_extra = :precio
                    WHERE id_modificador = :m
                    """
                ),
                {
                    "nombre": datos["nombre"],
                    "insumo": datos["id_insumo"],
                    "nuevo": datos["id_insumo_nuevo"],
                    "cantidad": datos["cantidad"],
                    "precio": datos["precio_extra"],
                    "m": int(id_modificador),
                },
            )
        except IntegrityError as exc:
            if _es_duplicado(exc):
                raise ModificadorError(
                    "Ya existe un modificador con ese nombre para el producto."
                ) from exc
            raise ModificadorError(
                "No se pudo actualizar el modificador; revisa los datos."
            ) from exc


def cambiar_activo_modificador(id_modificador: int, activo: bool) -> None:
    """Activa o da de baja lógica un modificador (nunca lo borra)."""
    with transaccion() as conn:
        resultado = conn.execute(
            text(
                "UPDATE modificadores_producto SET activo = :activo "
                "WHERE id_modificador = :m"
            ),
            {"activo": bool(activo), "m": int(id_modificador)},
        )
        if int(resultado.rowcount or 0) == 0:
            raise ModificadorError("El modificador seleccionado no existe.")


def modificadores_de_producto(
    id_producto: int, *, solo_activos: bool = True
) -> list[dict]:
    """Modificadores de un producto; por defecto solo los activos."""
    filtro = "AND m.activo = TRUE" if solo_activos else ""
    with get_engine().connect() as conn:
        filas = conn.execute(
            text(
                f"""
                SELECT m.id_modificador, m.id_producto, m.tipo, m.nombre,
                       m.id_insumo, i.nombre AS insumo,
                       u.abreviatura AS unidad,
                       m.id_insumo_nuevo,
                       n.nombre AS insumo_nuevo,
                       un.abreviatura AS unidad_nueva,
                       r.cantidad_usada AS cantidad_receta,
                       m.cantidad, m.precio_extra, m.activo
                FROM modificadores_producto m
                JOIN insumos i ON i.id_insumo = m.id_insumo
                JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
                LEFT JOIN insumos n ON n.id_insumo = m.id_insumo_nuevo
                LEFT JOIN unidades_medida un ON un.id_unidad = n.id_unidad_medida
                LEFT JOIN recetas r
                  ON r.id_producto = m.id_producto
                 AND r.id_insumo = m.id_insumo
                WHERE m.id_producto = :p {filtro}
                ORDER BY m.tipo, m.nombre
                """
            ),
            {"p": int(id_producto)},
        ).mappings().all()
    return [dict(fila) for fila in filas]


def resolver_modificadores(
    conn: Connection,
    id_producto: int,
    ids_modificadores,
) -> list[dict]:
    """Valida los ids y arma la foto que se guardará en la partida.

    Para Quitar y Sustituir, ``cantidad_resta`` se toma de la receta vigente
    en este momento. La foto queda congelada en ``detalle_modificadores``.
    """
    ids = [int(valor) for valor in (ids_modificadores or [])]
    if not ids:
        return []
    if len(set(ids)) != len(ids):
        raise ModificadorError(
            "No se puede aplicar el mismo modificador dos veces en una partida."
        )

    filas = conn.execute(
        text(
            """
            SELECT id_modificador, tipo, nombre, id_insumo,
                   id_insumo_nuevo, cantidad, precio_extra
            FROM modificadores_producto
            WHERE id_producto = :p
              AND activo = TRUE
              AND id_modificador IN :ids
            FOR SHARE
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"p": int(id_producto), "ids": ids},
    ).mappings().all()
    por_id = {int(fila["id_modificador"]): fila for fila in filas}
    if len(por_id) != len(ids):
        raise ModificadorError(
            "Uno o más modificadores no pertenecen al producto o están inactivos."
        )

    foto: list[dict] = []
    for id_modificador in ids:
        fila = por_id[id_modificador]
        tipo = str(fila["tipo"])
        cantidad_resta = None
        cantidad_suma = None
        id_insumo_resta = None
        id_insumo_suma = None

        if tipo in ("Quitar", "Sustituir"):
            cantidad_resta = _cantidad_en_receta(
                conn,
                id_producto=int(id_producto),
                id_insumo=int(fila["id_insumo"]),
            )
            if cantidad_resta is None:
                raise ModificadorError(
                    "El insumo de un modificador ya no está en la receta del producto."
                )
            id_insumo_resta = int(fila["id_insumo"])

        if tipo in ("Extra", "Sustituir"):
            if fila["cantidad"] is None:
                raise ModificadorError(
                    "El modificador no tiene cantidad configurada."
                )
            id_insumo_suma = int(
                fila["id_insumo_nuevo"]
                if tipo == "Sustituir"
                else fila["id_insumo"]
            )
            cantidad_suma = Decimal(str(fila["cantidad"]))

        foto.append(
            {
                "id_modificador": int(id_modificador),
                "tipo": tipo,
                "nombre": str(fila["nombre"]),
                "id_insumo_resta": id_insumo_resta,
                "cantidad_resta": cantidad_resta,
                "id_insumo_suma": id_insumo_suma,
                "cantidad_suma": cantidad_suma,
                "precio_extra": Decimal(str(fila["precio_extra"])),
            }
        )
    return foto


def foto_de_detalle(conn: Connection, id_detalle: int) -> list[dict]:
    """Lee la foto histórica de modificadores de una partida."""
    filas = conn.execute(
        text(
            """
            SELECT tipo, nombre, id_insumo_resta, cantidad_resta,
                   id_insumo_suma, cantidad_suma, precio_extra
            FROM detalle_modificadores
            WHERE id_detalle = :d
            ORDER BY id_detalle_modificador
            """
        ),
        {"d": int(id_detalle)},
    ).mappings().all()
    return [dict(fila) for fila in filas]


def guardar_foto_modificadores(
    conn: Connection, id_detalle: int, foto: list[dict]
) -> None:
    """Inserta las filas de ``detalle_modificadores`` para una partida."""
    if not foto:
        return
    conn.execute(
        text(
            """
            INSERT INTO detalle_modificadores
                (id_detalle, tipo, nombre, id_insumo_resta, cantidad_resta,
                 id_insumo_suma, cantidad_suma, precio_extra)
            VALUES
                (:detalle, :tipo, :nombre, :resta, :cantidad_resta,
                 :suma, :cantidad_suma, :precio)
            """
        ),
        [
            {
                "detalle": int(id_detalle),
                "tipo": item["tipo"],
                "nombre": item["nombre"],
                "resta": item.get("id_insumo_resta"),
                "cantidad_resta": item.get("cantidad_resta"),
                "suma": item.get("id_insumo_suma"),
                "cantidad_suma": item.get("cantidad_suma"),
                "precio": item.get("precio_extra", CERO),
            }
            for item in foto
        ],
    )
