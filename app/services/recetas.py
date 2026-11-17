"""Reglas de negocio para administrar recetas de productos.

La receta relaciona un producto del menú con los insumos que consume por
porción. Toda escritura se hace dentro de ``transaccion()`` para que un alta
o cambio quede completo o no deje nada a medias.

Importante: editar una receta no reescribe ventas pasadas ni movimientos de
inventario. Esos movimientos son el kardex histórico y permanecen intactos.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import transaccion


class RecetaError(Exception):
    """Error de negocio con un mensaje apto para mostrar en la interfaz."""


def _cantidad_positiva(cantidad) -> Decimal:
    """Convierte la cantidad a Decimal y valida que sea mayor que cero."""
    try:
        valor = Decimal(str(cantidad))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RecetaError("La cantidad debe ser un número válido.") from exc
    if not valor.is_finite() or valor <= 0:
        raise RecetaError("La cantidad debe ser mayor que cero.")
    return valor


def _validar_producto(conn, id_producto: int) -> None:
    existe = conn.execute(
        text("SELECT 1 FROM productos WHERE id_producto = :p"),
        {"p": int(id_producto)},
    ).scalar()
    if not existe:
        raise RecetaError("El producto seleccionado no existe.")


def _validar_insumo(conn, id_insumo: int) -> None:
    existe = conn.execute(
        text("SELECT 1 FROM insumos WHERE id_insumo = :i"),
        {"i": int(id_insumo)},
    ).scalar()
    if not existe:
        raise RecetaError("El insumo seleccionado no existe.")


def agregar_insumo_receta(
    id_producto: int, id_insumo: int, cantidad
) -> int:
    """Agrega un insumo a la receta del producto y regresa su id_receta."""
    cantidad_dec = _cantidad_positiva(cantidad)
    with transaccion() as conn:
        _validar_producto(conn, id_producto)
        _validar_insumo(conn, id_insumo)

        repetido = conn.execute(
            text(
                "SELECT 1 FROM recetas "
                "WHERE id_producto = :p AND id_insumo = :i"
            ),
            {"p": int(id_producto), "i": int(id_insumo)},
        ).scalar()
        if repetido:
            raise RecetaError("Ese insumo ya está en la receta.")

        try:
            id_receta = conn.execute(
                text(
                    """
                    INSERT INTO recetas (id_producto, id_insumo, cantidad_usada)
                    VALUES (:p, :i, :c)
                    RETURNING id_receta
                    """
                ),
                {
                    "p": int(id_producto),
                    "i": int(id_insumo),
                    "c": cantidad_dec,
                },
            ).scalar_one()
        except IntegrityError as exc:
            raise RecetaError("Ese insumo ya está en la receta.") from exc
    return int(id_receta)


def cambiar_cantidad_receta(
    id_producto: int, id_insumo: int, cantidad
) -> None:
    """Cambia la cantidad usada de un insumo ya presente en la receta."""
    cantidad_dec = _cantidad_positiva(cantidad)
    with transaccion() as conn:
        _validar_producto(conn, id_producto)
        _validar_insumo(conn, id_insumo)
        resultado = conn.execute(
            text(
                """
                UPDATE recetas
                SET cantidad_usada = :c
                WHERE id_producto = :p AND id_insumo = :i
                """
            ),
            {
                "p": int(id_producto),
                "i": int(id_insumo),
                "c": cantidad_dec,
            },
        )
        if int(resultado.rowcount or 0) == 0:
            raise RecetaError("Ese insumo no está en la receta.")


def quitar_insumo_receta(id_producto: int, id_insumo: int) -> None:
    """Quita un insumo de la receta del producto."""
    with transaccion() as conn:
        _validar_producto(conn, id_producto)
        _validar_insumo(conn, id_insumo)
        resultado = conn.execute(
            text(
                "DELETE FROM recetas "
                "WHERE id_producto = :p AND id_insumo = :i"
            ),
            {"p": int(id_producto), "i": int(id_insumo)},
        )
        if int(resultado.rowcount or 0) == 0:
            raise RecetaError("Ese insumo no está en la receta.")
