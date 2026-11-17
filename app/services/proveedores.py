"""Administración de proveedores y costos acordados por insumo."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import run_query, transaccion
from services.bitacora import registrar_evento


class ProveedorError(Exception):
    """Error de negocio de proveedores; la UI muestra el mensaje."""


def _dinero(valor) -> Decimal:
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _texto(valor, limite: int) -> str | None:
    texto = str(valor or "").strip()
    return texto[:limite] if texto else None


def _validar_nombre(nombre: str) -> str:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ProveedorError("El nombre del proveedor es obligatorio.")
    if len(nombre) > 100:
        raise ProveedorError("El nombre no puede exceder 100 caracteres.")
    return nombre


def _existe_nombre(conn, nombre: str, excluir: int | None = None) -> bool:
    sql = "SELECT 1 FROM proveedores WHERE LOWER(nombre) = LOWER(:nombre)"
    params = {"nombre": nombre}
    if excluir is not None:
        sql += " AND id_proveedor <> :id"
        params["id"] = int(excluir)
    return conn.execute(text(sql + " LIMIT 1"), params).first() is not None


def listar_proveedores(incluir_inactivos: bool = False):
    filtro = "" if incluir_inactivos else "WHERE activo = TRUE"
    return run_query(
        f"""
        SELECT id_proveedor, nombre, telefono, direccion, correo, condiciones,
               activo
        FROM proveedores
        {filtro}
        ORDER BY activo DESC, nombre
        """
    )


def listar_insumos_proveedor(id_proveedor: int):
    return run_query(
        """
        SELECT pi.id_insumo, i.nombre AS insumo, i.activo AS insumo_activo,
               u.abreviatura AS unidad, pi.costo_acordado, pi.activo
        FROM proveedor_insumos pi
        JOIN insumos i ON i.id_insumo = pi.id_insumo
        JOIN unidades_medida u ON u.id_unidad = i.id_unidad_medida
        WHERE pi.id_proveedor = :proveedor
        ORDER BY pi.activo DESC, i.nombre
        """,
        {"proveedor": int(id_proveedor)},
    )


def costos_acordados(id_proveedor: int) -> dict[int, Decimal]:
    """Costos activos del proveedor, indexados por id_insumo."""
    filas = run_query(
        """
        SELECT id_insumo, costo_acordado
        FROM proveedor_insumos
        WHERE id_proveedor = :proveedor AND activo = TRUE
        """,
        {"proveedor": int(id_proveedor)},
    )
    return {
        int(fila["id_insumo"]): Decimal(str(fila["costo_acordado"]))
        for _, fila in filas.iterrows()
    }


def listar_historial_precios(id_proveedor: int):
    return run_query(
        """
        SELECT h.id, i.nombre AS insumo, h.costo_anterior, h.costo_nuevo,
               u.nombre_usuario, h.fecha
        FROM historial_precios_proveedor h
        JOIN insumos i ON i.id_insumo = h.id_insumo
        JOIN usuarios u ON u.id_usuario = h.id_usuario
        WHERE h.id_proveedor = :proveedor
        ORDER BY h.fecha DESC, h.id DESC
        """,
        {"proveedor": int(id_proveedor)},
    )


def crear_proveedor(
    *,
    nombre: str,
    telefono: str | None,
    direccion: str | None,
    correo: str | None,
    condiciones: str | None,
    id_usuario: int,
) -> int:
    nombre = _validar_nombre(nombre)
    with transaccion() as conn:
        if _existe_nombre(conn, nombre):
            raise ProveedorError("Ya existe un proveedor con ese nombre.")
        try:
            id_proveedor = conn.execute(
                text(
                    """
                    INSERT INTO proveedores
                        (nombre, telefono, direccion, correo, condiciones,
                         activo)
                    VALUES (:nombre, :telefono, :direccion, :correo,
                            :condiciones, TRUE)
                    RETURNING id_proveedor
                    """
                ),
                {
                    "nombre": nombre,
                    "telefono": _texto(telefono, 20),
                    "direccion": _texto(direccion, 255),
                    "correo": _texto(correo, 100),
                    "condiciones": _texto(condiciones, 200),
                },
            ).scalar_one()
        except IntegrityError as exc:
            if "uq_proveedor_nombre_lower" in str(exc.orig):
                raise ProveedorError(
                    "Ya existe un proveedor con ese nombre."
                ) from exc
            raise
        registrar_evento(
            conn,
            tipo_evento="PROVEEDOR",
            entidad="proveedores",
            id_entidad=int(id_proveedor),
            id_usuario=int(id_usuario),
            datos_nuevos={
                "accion": "alta",
                "nombre": nombre,
                "telefono": _texto(telefono, 20),
                "direccion": _texto(direccion, 255),
                "correo": _texto(correo, 100),
                "condiciones": _texto(condiciones, 200),
                "activo": True,
            },
        )
    return int(id_proveedor)


def actualizar_proveedor(
    *,
    id_proveedor: int,
    nombre: str,
    telefono: str | None,
    direccion: str | None,
    correo: str | None,
    condiciones: str | None,
    activo: bool | None,
    id_usuario: int,
) -> None:
    nombre = _validar_nombre(nombre)
    with transaccion() as conn:
        actual = conn.execute(
            text(
                """
                SELECT nombre, telefono, direccion, correo, condiciones, activo
                FROM proveedores
                WHERE id_proveedor = :id
                FOR UPDATE
                """
            ),
            {"id": int(id_proveedor)},
        ).mappings().first()
        if actual is None:
            raise ProveedorError("El proveedor ya no existe.")
        if _existe_nombre(conn, nombre, excluir=int(id_proveedor)):
            raise ProveedorError("Ya existe un proveedor con ese nombre.")

        nuevo_activo = bool(actual["activo"]) if activo is None else bool(activo)
        try:
            conn.execute(
                text(
                    """
                    UPDATE proveedores
                    SET nombre = :nombre, telefono = :telefono,
                        direccion = :direccion, correo = :correo,
                        condiciones = :condiciones, activo = :activo
                    WHERE id_proveedor = :id
                    """
                ),
                {
                    "id": int(id_proveedor),
                    "nombre": nombre,
                    "telefono": _texto(telefono, 20),
                    "direccion": _texto(direccion, 255),
                    "correo": _texto(correo, 100),
                    "condiciones": _texto(condiciones, 200),
                    "activo": nuevo_activo,
                },
            )
        except IntegrityError as exc:
            if "uq_proveedor_nombre_lower" in str(exc.orig):
                raise ProveedorError(
                    "Ya existe un proveedor con ese nombre."
                ) from exc
            raise
        registrar_evento(
            conn,
            tipo_evento="PROVEEDOR",
            entidad="proveedores",
            id_entidad=int(id_proveedor),
            id_usuario=int(id_usuario),
            datos_anteriores=dict(actual),
            datos_nuevos={
                "accion": "edicion",
                "nombre": nombre,
                "telefono": _texto(telefono, 20),
                "direccion": _texto(direccion, 255),
                "correo": _texto(correo, 100),
                "condiciones": _texto(condiciones, 200),
                "activo": nuevo_activo,
            },
        )


def cambiar_estado_proveedor(
    *, id_proveedor: int, activo: bool, id_usuario: int
) -> None:
    with transaccion() as conn:
        actual = conn.execute(
            text(
                "SELECT activo FROM proveedores WHERE id_proveedor = :id "
                "FOR UPDATE"
            ),
            {"id": int(id_proveedor)},
        ).scalar_one_or_none()
        if actual is None:
            raise ProveedorError("El proveedor ya no existe.")
        conn.execute(
            text("UPDATE proveedores SET activo = :activo WHERE id_proveedor = :id"),
            {"activo": bool(activo), "id": int(id_proveedor)},
        )
        registrar_evento(
            conn,
            tipo_evento="PROVEEDOR",
            entidad="proveedores",
            id_entidad=int(id_proveedor),
            id_usuario=int(id_usuario),
            datos_anteriores={"activo": bool(actual)},
            datos_nuevos={"accion": "activar" if activo else "desactivar",
                          "activo": bool(activo)},
        )


def vincular_insumo(
    *,
    id_proveedor: int,
    id_insumo: int,
    costo_acordado,
    id_usuario: int,
) -> None:
    """Crea o reactiva el vínculo y registra el cambio de costo si aplica."""
    costo = _dinero(costo_acordado)
    if costo < 0:
        raise ProveedorError("El costo acordado no puede ser negativo.")

    with transaccion() as conn:
        proveedor = conn.execute(
            text("SELECT 1 FROM proveedores WHERE id_proveedor = :id"),
            {"id": int(id_proveedor)},
        ).scalar()
        if proveedor is None:
            raise ProveedorError("El proveedor ya no existe.")
        insumo = conn.execute(
            text("SELECT 1 FROM insumos WHERE id_insumo = :id"),
            {"id": int(id_insumo)},
        ).scalar()
        if insumo is None:
            raise ProveedorError("El insumo seleccionado ya no existe.")

        anterior = conn.execute(
            text(
                """
                SELECT costo_acordado, activo
                FROM proveedor_insumos
                WHERE id_proveedor = :proveedor AND id_insumo = :insumo
                FOR UPDATE
                """
            ),
            {"proveedor": int(id_proveedor), "insumo": int(id_insumo)},
        ).mappings().first()

        if anterior is None:
            conn.execute(
                text(
                    """
                    INSERT INTO proveedor_insumos
                        (id_proveedor, id_insumo, costo_acordado, activo)
                    VALUES (:proveedor, :insumo, :costo, TRUE)
                    """
                ),
                {
                    "proveedor": int(id_proveedor),
                    "insumo": int(id_insumo),
                    "costo": costo,
                },
            )
            accion = "vinculo"
            costo_anterior = None
        else:
            costo_anterior = Decimal(str(anterior["costo_acordado"]))
            conn.execute(
                text(
                    """
                    UPDATE proveedor_insumos
                    SET costo_acordado = :costo, activo = TRUE
                    WHERE id_proveedor = :proveedor AND id_insumo = :insumo
                    """
                ),
                {
                    "proveedor": int(id_proveedor),
                    "insumo": int(id_insumo),
                    "costo": costo,
                },
            )
            accion = "reactivacion" if not anterior["activo"] else "vinculo"
            if costo != costo_anterior:
                conn.execute(
                    text(
                        """
                        INSERT INTO historial_precios_proveedor
                            (id_proveedor, id_insumo, costo_anterior,
                             costo_nuevo, id_usuario)
                        VALUES (:proveedor, :insumo, :anterior, :nuevo, :usuario)
                        """
                    ),
                    {
                        "proveedor": int(id_proveedor),
                        "insumo": int(id_insumo),
                        "anterior": costo_anterior,
                        "nuevo": costo,
                        "usuario": int(id_usuario),
                    },
                )
                accion = "cambio_costo"

        registrar_evento(
            conn,
            tipo_evento="PROVEEDOR",
            entidad="proveedores",
            id_entidad=int(id_proveedor),
            id_usuario=int(id_usuario),
            datos_anteriores=(
                None
                if costo_anterior is None
                else {"costo_acordado": costo_anterior}
            ),
            datos_nuevos={
                "accion": accion,
                "id_insumo": int(id_insumo),
                "costo_acordado": costo,
            },
        )


def desvincular_insumo(
    *, id_proveedor: int, id_insumo: int, id_usuario: int
) -> None:
    with transaccion() as conn:
        actual = conn.execute(
            text(
                """
                SELECT costo_acordado, activo
                FROM proveedor_insumos
                WHERE id_proveedor = :proveedor AND id_insumo = :insumo
                FOR UPDATE
                """
            ),
            {"proveedor": int(id_proveedor), "insumo": int(id_insumo)},
        ).mappings().first()
        if actual is None:
            raise ProveedorError("El insumo no está vinculado a este proveedor.")
        if not actual["activo"]:
            return
        conn.execute(
            text(
                """
                UPDATE proveedor_insumos SET activo = FALSE
                WHERE id_proveedor = :proveedor AND id_insumo = :insumo
                """
            ),
            {"proveedor": int(id_proveedor), "insumo": int(id_insumo)},
        )
        registrar_evento(
            conn,
            tipo_evento="PROVEEDOR",
            entidad="proveedores",
            id_entidad=int(id_proveedor),
            id_usuario=int(id_usuario),
            datos_anteriores={
                "activo": True,
                "costo_acordado": Decimal(str(actual["costo_acordado"])),
            },
            datos_nuevos={"accion": "desvinculo", "id_insumo": int(id_insumo)},
        )
