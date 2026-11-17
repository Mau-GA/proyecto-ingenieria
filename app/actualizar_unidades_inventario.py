"""Migra a piezas los insumos de alitas y refrescos en una BD existente.

Es idempotente: si un insumo ya está en ``pza`` no vuelve a convertir sus
cantidades. Ejecutar dentro del contenedor:

    docker compose exec app python actualizar_unidades_inventario.py
"""
from decimal import Decimal

from sqlalchemy import text

from database import transaccion

PIEZAS_POR_KG_ALITAS = Decimal("20")
LITROS_POR_PIEZA_REFRESCO = Decimal("0.6")


def _id_unidad(conn, nombre: str, abreviatura: str) -> int:
    return int(
        conn.execute(
            text(
                "SELECT id_unidad FROM unidades_medida "
                "WHERE nombre = :nombre OR abreviatura = :abreviatura"
            ),
            {"nombre": nombre, "abreviatura": abreviatura},
        ).scalar_one()
    )


def _convertir_alitas(conn, id_insumo: int, id_pieza: int) -> None:
    factor = PIEZAS_POR_KG_ALITAS
    parametros = {"pza": id_pieza, "factor": factor, "id": id_insumo}
    conn.execute(
        text(
            """
            UPDATE insumos
            SET id_unidad_medida = :pza,
                stock_actual = stock_actual * :factor,
                stock_minimo = stock_minimo * :factor,
                costo_unitario_referencia = costo_unitario_referencia / :factor,
                descripcion = 'Alitas frescas por pieza'
            WHERE id_insumo = :id
            """
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE recetas SET cantidad_usada = cantidad_usada * :factor "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE movimientos_inventario SET cantidad = cantidad * :factor "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE detalle_compra "
            "SET cantidad = cantidad * :factor, "
            "    costo_unitario = costo_unitario / :factor "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )


def _convertir_refresco(conn, id_insumo: int, id_pieza: int) -> None:
    litros = LITROS_POR_PIEZA_REFRESCO
    factor = Decimal("1") / litros
    parametros = {
        "pza": id_pieza,
        "factor": factor,
        "litros": litros,
        "id": id_insumo,
    }
    conn.execute(
        text(
            """
            UPDATE insumos
            SET id_unidad_medida = :pza,
                stock_actual = ROUND(stock_actual * :factor, 3),
                stock_minimo = GREATEST(CEIL(stock_minimo * :factor), 1),
                costo_unitario_referencia = costo_unitario_referencia * :litros,
                descripcion = 'Refresco envasado por pieza'
            WHERE id_insumo = :id
            """
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE recetas "
            "SET cantidad_usada = ROUND(cantidad_usada / :litros, 3) "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE movimientos_inventario "
            "SET cantidad = ROUND(cantidad / :litros, 3) "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )
    conn.execute(
        text(
            "UPDATE detalle_compra "
            "SET cantidad = ROUND(cantidad / :litros, 3), "
            "    costo_unitario = ROUND(costo_unitario * :litros, 2) "
            "WHERE id_insumo = :id"
        ),
        parametros,
    )


def main() -> None:
    with transaccion() as conn:
        id_kg = _id_unidad(conn, "Kilogramo", "kg")
        id_litro = _id_unidad(conn, "Litro", "l")
        id_pieza = _id_unidad(conn, "Pieza", "pza")

        alitas = conn.execute(
            text(
                """
                SELECT id_insumo, nombre, id_unidad_medida
                FROM insumos
                WHERE nombre ILIKE '%alita%'
                ORDER BY id_insumo
                FOR UPDATE
                """
            )
        ).mappings().all()
        refrescos = conn.execute(
            text(
                """
                SELECT id_insumo, nombre, id_unidad_medida
                FROM insumos
                WHERE nombre ILIKE '%coca%' OR nombre ILIKE '%refresco%'
                   OR nombre ILIKE '%soda%'
                ORDER BY id_insumo
                FOR UPDATE
                """
            )
        ).mappings().all()

        procesados: set[int] = set()
        for fila in alitas:
            id_insumo = int(fila["id_insumo"])
            unidad_actual = int(fila["id_unidad_medida"])
            if id_insumo in procesados:
                continue
            if unidad_actual == id_pieza:
                print(f"OMITIDO {fila['nombre']}: ya está en piezas.")
                procesados.add(id_insumo)
                continue
            if unidad_actual != id_kg:
                print(
                    f"OMITIDO {fila['nombre']}: unidad inesperada "
                    f"{unidad_actual}."
                )
                procesados.add(id_insumo)
                continue
            _convertir_alitas(conn, id_insumo, id_pieza)
            procesados.add(id_insumo)
            print(f"CONVERTIDO {fila['nombre']}: kg -> piezas.")

        for fila in refrescos:
            id_insumo = int(fila["id_insumo"])
            unidad_actual = int(fila["id_unidad_medida"])
            if id_insumo in procesados:
                continue
            if unidad_actual == id_pieza:
                print(f"OMITIDO {fila['nombre']}: ya está en piezas.")
                procesados.add(id_insumo)
                continue
            if unidad_actual != id_litro:
                print(
                    f"OMITIDO {fila['nombre']}: unidad inesperada "
                    f"{unidad_actual}."
                )
                procesados.add(id_insumo)
                continue
            _convertir_refresco(conn, id_insumo, id_pieza)
            procesados.add(id_insumo)
            print(f"CONVERTIDO {fila['nombre']}: litros -> piezas.")

    print("Actualización de unidades terminada.")


if __name__ == "__main__":
    main()
