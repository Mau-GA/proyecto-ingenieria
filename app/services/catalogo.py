"""Reglas de negocio del catálogo de productos y categorías.

Los cambios de precio solo actualizan ``productos.precio_actual``. Las
partidas ya capturadas conservan su ``precio_unitario`` histórico; por eso no
se tocan ``detalles_orden`` desde aquí.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import transaccion
from services.bitacora import registrar_evento


class CatalogoError(Exception):
    """Error de negocio con un mensaje apto para mostrar en la interfaz."""


def _precio_valido(precio) -> Decimal:
    try:
        valor = Decimal(str(precio))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CatalogoError("El precio debe ser un número válido.") from exc
    if not valor.is_finite() or valor < 0:
        raise CatalogoError("El precio no puede ser negativo.")
    return valor


def actualizar_producto(
    *,
    id_producto: int,
    nombre: str,
    descripcion: str | None,
    id_categoria: int,
    id_area_preparacion: int,
    precio,
    disponible: bool,
    id_usuario: int | None = None,
) -> None:
    """Actualiza los datos editables de un producto sin tocar ventas pasadas."""
    nombre_limpio = (nombre or "").strip()
    if not nombre_limpio:
        raise CatalogoError("El nombre es obligatorio.")
    precio_dec = _precio_valido(precio)

    with transaccion() as conn:
        anterior = conn.execute(
            text(
                "SELECT precio_actual FROM productos "
                "WHERE id_producto = :id FOR UPDATE"
            ),
            {"id": int(id_producto)},
        ).scalar_one_or_none()
        if anterior is None:
            raise CatalogoError("El producto seleccionado no existe.")

        try:
            resultado = conn.execute(
                text(
                    """
                    UPDATE productos
                    SET nombre = :n, descripcion = :d, id_categoria = :c,
                        id_area_preparacion = :a, precio_actual = :p,
                        disponible = :disponible
                    WHERE id_producto = :id
                    """
                ),
                {
                    "n": nombre_limpio,
                    "d": (descripcion or "").strip() or None,
                    "c": int(id_categoria),
                    "a": int(id_area_preparacion),
                    "p": precio_dec,
                    "disponible": bool(disponible),
                    "id": int(id_producto),
                },
            )
        except IntegrityError as exc:
            if getattr(getattr(exc, "orig", None), "pgcode", None) == "23505":
                raise CatalogoError(
                    "Ya existe un producto con ese nombre."
                ) from exc
            raise CatalogoError(
                "No se pudo actualizar el producto: revisa la categoría y el área."
            ) from exc
        if int(resultado.rowcount or 0) == 0:
            raise CatalogoError("El producto seleccionado no existe.")

        precio_anterior = Decimal(str(anterior))
        if precio_anterior != precio_dec:
            registrar_evento(
                conn,
                tipo_evento="CAMBIO_PRECIO",
                entidad="productos",
                id_entidad=int(id_producto),
                id_usuario=id_usuario,
                datos_anteriores={"precio_actual": precio_anterior},
                datos_nuevos={"precio_actual": precio_dec},
            )


def crear_categoria(nombre: str, descripcion: str | None = None) -> int:
    """Crea una categoría de producto; los duplicados se informan claro."""
    nombre_limpio = (nombre or "").strip()
    if not nombre_limpio:
        raise CatalogoError("El nombre de la categoría es obligatorio.")
    with transaccion() as conn:
        try:
            id_categoria = conn.execute(
                text(
                    """
                    INSERT INTO categorias_productos (nombre, descripcion)
                    VALUES (:n, :d)
                    RETURNING id_categoria
                    """
                ),
                {
                    "n": nombre_limpio,
                    "d": (descripcion or "").strip() or None,
                },
            ).scalar_one()
        except IntegrityError as exc:
            if getattr(getattr(exc, "orig", None), "pgcode", None) == "23505":
                raise CatalogoError(
                    "Ya existe una categoría con ese nombre."
                ) from exc
            raise CatalogoError("No se pudo crear la categoría.") from exc
    return int(id_categoria)


def renombrar_categoria(id_categoria: int, nombre: str) -> None:
    """Renombra una categoría existente."""
    nombre_limpio = (nombre or "").strip()
    if not nombre_limpio:
        raise CatalogoError("El nombre de la categoría es obligatorio.")
    with transaccion() as conn:
        try:
            resultado = conn.execute(
                text(
                    "UPDATE categorias_productos SET nombre = :n "
                    "WHERE id_categoria = :id"
                ),
                {"n": nombre_limpio, "id": int(id_categoria)},
            )
        except IntegrityError as exc:
            if getattr(getattr(exc, "orig", None), "pgcode", None) == "23505":
                raise CatalogoError(
                    "Ya existe una categoría con ese nombre."
                ) from exc
            raise CatalogoError("No se pudo renombrar la categoría.") from exc
        if int(resultado.rowcount or 0) == 0:
            raise CatalogoError("La categoría seleccionada no existe.")
