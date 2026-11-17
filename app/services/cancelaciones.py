"""
Ruta: proyectorestaurante/app/services/cancelaciones.py

Cancelación autorizada de una orden completa.

Reglas:
- Solo un usuario con rol Gerente o Administrador puede autorizar.
- La autorización se valida con la misma contraseña bcrypt del login; nunca
  se guarda la contraseña, solo el usuario que autorizó.
- Si la orden ya fue cobrada, no se cancela: el proyecto no incluye reembolsos.
- Partidas Pendiente devuelven su consumo exacto al inventario.
- Partidas En preparación, Listo o Entregado se registran como merma con tipo
  "Cancelación de orden", sin tocar stock (la receta ya se descontó y no se
  vuelve a descontar), y sin pasar por la bandeja de mermas pendientes.
- Todo ocurre en UNA transacción: o se cancela completa o no se modifica nada.
- Cada intento fallido de autorización deja un LOGIN_FALLIDO en la bitácora.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from auth import _roles_activos, _verificar_password
from database import run_query, transaccion
from permissions import ADMIN, GERENTE
from services.bitacora import registrar_evento
from services.mermas import MermaError, registrar_merma_detalle
from services.modificadores import foto_de_detalle
from services.ventas import EmpleadoRequeridoError, _revertir_inventario


class CancelacionError(Exception):
    """Error de negocio al cancelar una orden; la UI muestra el mensaje."""


def _registrar_fallo(
    nombre_usuario: str, id_usuario: int | None, motivo: str
) -> None:
    """Registra un intento de autorización fallido en una transacción propia."""
    with transaccion() as conn:
        registrar_evento(
            conn,
            tipo_evento="LOGIN_FALLIDO",
            entidad="autenticacion",
            id_entidad=id_usuario,
            id_usuario=id_usuario,
            datos_nuevos={
                "nombre_usuario": (nombre_usuario or "").strip(),
                "motivo": motivo,
                "contexto": "cancelacion_orden",
            },
        )


def _autorizar(nombre_usuario: str, password: str) -> dict:
    """Valida credenciales y rol del autorizante; lanza CancelacionError."""
    nombre_usuario = (nombre_usuario or "").strip()
    if not nombre_usuario or not password:
        _registrar_fallo(nombre_usuario, None, "Credenciales incompletas")
        raise CancelacionError("Autorización inválida.")

    usuarios = run_query(
        """
        SELECT id_usuario, nombre_usuario, password_hash, activo
        FROM usuarios
        WHERE nombre_usuario = :u
        """,
        {"u": nombre_usuario},
    )
    if usuarios.empty:
        _registrar_fallo(nombre_usuario, None, "Usuario inexistente")
        raise CancelacionError("Autorización inválida.")

    usuario = usuarios.iloc[0]
    id_usuario = int(usuario["id_usuario"])
    if not bool(usuario["activo"]):
        _registrar_fallo(nombre_usuario, id_usuario, "Usuario inactivo")
        raise CancelacionError("Autorización inválida.")
    if not _verificar_password(str(password), str(usuario["password_hash"])):
        _registrar_fallo(nombre_usuario, id_usuario, "Contraseña incorrecta")
        raise CancelacionError("Autorización inválida.")

    roles = set(_roles_activos(id_usuario))
    if not roles & {ADMIN, GERENTE}:
        _registrar_fallo(nombre_usuario, id_usuario, "Sin rol autorizado")
        raise CancelacionError("Autorización inválida.")

    return {
        "id_usuario": id_usuario,
        "nombre_usuario": str(usuario["nombre_usuario"]),
        "roles": roles,
    }


def _empleado_para_cancelacion(
    conn: Connection, *, id_orden: int, id_solicita: int, id_autoriza: int
) -> int | None:
    """Empleado que respaldará movimientos: autorizante, solicitante o mesero."""
    valor = conn.execute(
        text(
            """
            SELECT COALESCE(a.id_empleado, s.id_empleado, o.id_empleado_atiende)
            FROM ordenes o
            LEFT JOIN usuarios a ON a.id_usuario = :autoriza
            LEFT JOIN usuarios s ON s.id_usuario = :solicita
            WHERE o.id_orden = :orden
            """
        ),
        {
            "autoriza": id_autoriza,
            "solicita": id_solicita,
            "orden": id_orden,
        },
    ).scalar()
    return None if valor is None else int(valor)


def _cancelar_detalle(
    conn: Connection,
    *,
    id_detalle: int,
    id_cancelado: int,
    id_usuario: int,
) -> None:
    """Cambia la partida a Cancelado y deja su fila de historial."""
    conn.execute(
        text("UPDATE detalles_orden SET id_estado = :estado WHERE id_detalle = :detalle"),
        {"estado": id_cancelado, "detalle": id_detalle},
    )
    conn.execute(
        text(
            """
            INSERT INTO historial_estado_detalle (id_detalle, id_estado, id_usuario)
            VALUES (:detalle, :estado, :usuario)
            """
        ),
        {
            "detalle": id_detalle,
            "estado": id_cancelado,
            "usuario": id_usuario,
        },
    )


def cancelar_orden(
    *,
    id_orden: int,
    id_motivo: int,
    id_usuario_solicita: int,
    usuario_autoriza: str,
    password_autoriza: str,
    comentario: str | None = None,
) -> dict:
    """
    Cancela una orden completa con autorización de Gerente/Administrador.

    Regresa un resumen con ``id_cancelacion``, ``devueltas`` y ``mermadas``.
    """
    autorizante = _autorizar(usuario_autoriza, password_autoriza)
    comentario = (comentario or "").strip() or None
    if comentario and len(comentario) > 200:
        raise CancelacionError("El comentario no puede exceder 200 caracteres.")

    with transaccion() as conn:
        orden = conn.execute(
            text(
                """
                SELECT o.id_orden, o.numero_orden_diaria, o.id_mesa, o.total,
                       o.id_empleado_atiende, e.nombre AS estado
                FROM ordenes o
                JOIN estados_orden e ON e.id_estado = o.id_estado
                WHERE o.id_orden = :orden
                FOR UPDATE OF o
                """
            ),
            {"orden": int(id_orden)},
        ).mappings().first()
        if orden is None:
            raise CancelacionError("La orden no existe.")

        estado_anterior = str(orden["estado"])
        if estado_anterior == "Pagada":
            raise CancelacionError(
                "La orden ya fue cobrada; requiere un reembolso."
            )
        if estado_anterior == "Cancelada":
            raise CancelacionError("La orden ya está cancelada.")

        pagado = conn.execute(
            text("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE id_orden = :orden"),
            {"orden": int(id_orden)},
        ).scalar_one()
        if pagado and pagado > 0:
            raise CancelacionError(
                "La orden tiene pagos registrados; requiere un reembolso."
            )

        motivo = conn.execute(
            text(
                """
                SELECT id, nombre
                FROM motivos_cancelacion
                WHERE id = :motivo AND activo = TRUE
                """
            ),
            {"motivo": int(id_motivo)},
        ).mappings().first()
        if motivo is None:
            raise CancelacionError("Selecciona un motivo de cancelación activo.")

        nombre_motivo = str(motivo["nombre"])
        if nombre_motivo.casefold() == "otro" and not comentario:
            raise CancelacionError(
                "Captura el comentario cuando el motivo es «Otro»."
            )

        id_cancelado = conn.execute(
            text(
                "SELECT id_estado FROM estados_detalle_orden "
                "WHERE nombre = 'Cancelado'"
            )
        ).scalar_one_or_none()
        if id_cancelado is None:
            raise CancelacionError(
                "Falta el estado 'Cancelado' en estados_detalle_orden."
            )
        id_cancelado = int(id_cancelado)

        id_empleado = _empleado_para_cancelacion(
            conn,
            id_orden=int(id_orden),
            id_solicita=int(id_usuario_solicita),
            id_autoriza=int(autorizante["id_usuario"]),
        )

        detalles = conn.execute(
            text(
                """
                SELECT d.id_detalle, d.id_producto, d.cantidad,
                       ed.nombre AS estado,
                       EXISTS (
                           SELECT 1
                           FROM detalle_mermas dm
                           WHERE dm.id_detalle = d.id_detalle
                       ) AS mermada
                FROM detalles_orden d
                JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado
                WHERE d.id_orden = :orden
                ORDER BY d.id_detalle
                FOR UPDATE OF d
                """
            ),
            {"orden": int(id_orden)},
        ).mappings().all()

        devueltas: list[int] = []
        mermadas: list[int] = []

        for detalle in detalles:
            id_detalle = int(detalle["id_detalle"])
            estado_detalle = str(detalle["estado"])
            if estado_detalle == "Cancelado":
                continue

            if bool(detalle["mermada"]):
                # Ya tenía su merma registrada antes de cancelar la orden:
                # solo se cierra la partida y no se duplica el costo.
                _cancelar_detalle(
                    conn,
                    id_detalle=id_detalle,
                    id_cancelado=id_cancelado,
                    id_usuario=int(id_usuario_solicita),
                )
                continue

            if estado_detalle == "Pendiente":
                if id_empleado is None:
                    raise CancelacionError(
                        "No hay una ficha de empleado vinculada para respaldar "
                        "la reversa de inventario."
                    )
                foto = foto_de_detalle(conn, id_detalle)
                try:
                    _revertir_inventario(
                        conn,
                        id_orden=int(id_orden),
                        partida={
                            "id_producto": int(detalle["id_producto"]),
                            "cantidad": int(detalle["cantidad"]),
                            "id_detalle": id_detalle,
                            "modificadores": foto,
                        },
                        id_empleado=id_empleado,
                        motivo=(
                            f"Reversa por cancelación de la orden #{id_orden}"
                        ),
                    )
                except EmpleadoRequeridoError as exc:
                    raise CancelacionError(str(exc)) from exc
                devueltas.append(id_detalle)

            elif estado_detalle in ("En preparación", "Listo", "Entregado"):
                if id_empleado is None:
                    raise CancelacionError(
                        "No hay una ficha de empleado vinculada para respaldar "
                        "la merma por cancelación."
                    )
                try:
                    registrar_merma_detalle(
                        conn=conn,
                        id_detalle=id_detalle,
                        id_usuario=int(id_usuario_solicita),
                        id_empleado=id_empleado,
                        motivo=f"Cancelación de orden: {nombre_motivo}",
                        origen="orden",
                        liberar_mesa_si_total=False,
                        nombre_tipo_merma="Cancelación de orden",
                        autorizada=True,
                    )
                except MermaError as exc:
                    raise CancelacionError(str(exc)) from exc
                mermadas.append(id_detalle)
            else:
                raise CancelacionError(
                    f"No se puede cancelar una partida en estado «{estado_detalle}»."
                )

            _cancelar_detalle(
                conn,
                id_detalle=id_detalle,
                id_cancelado=id_cancelado,
                id_usuario=int(id_usuario_solicita),
            )

        id_cancelada = conn.execute(
            text("SELECT id_estado FROM estados_orden WHERE nombre = 'Cancelada'")
        ).scalar_one_or_none()
        if id_cancelada is None:
            raise CancelacionError("Falta el estado 'Cancelada' en el catálogo.")
        conn.execute(
            text("UPDATE ordenes SET id_estado = :estado WHERE id_orden = :orden"),
            {"estado": int(id_cancelada), "orden": int(id_orden)},
        )

        if orden["id_mesa"] is not None:
            conn.execute(
                text(
                    """
                    UPDATE mesas
                    SET estado = 'Disponible'
                    WHERE id_mesa = :mesa
                      AND NOT EXISTS (
                          SELECT 1
                          FROM ordenes o
                          JOIN estados_orden e ON e.id_estado = o.id_estado
                          WHERE o.id_mesa = mesas.id_mesa
                            AND o.id_orden <> :orden
                            AND e.es_final = FALSE
                      )
                    """
                ),
                {"mesa": int(orden["id_mesa"]), "orden": int(id_orden)},
            )

        id_cancelacion = conn.execute(
            text(
                """
                INSERT INTO cancelaciones
                    (id_orden, id_motivo, comentario, id_usuario_solicita,
                     id_usuario_autoriza)
                VALUES
                    (:orden, :motivo, :comentario, :solicita, :autoriza)
                RETURNING id
                """
            ),
            {
                "orden": int(id_orden),
                "motivo": int(id_motivo),
                "comentario": comentario,
                "solicita": int(id_usuario_solicita),
                "autoriza": int(autorizante["id_usuario"]),
            },
        ).scalar_one()

        registrar_evento(
            conn,
            tipo_evento="CANCELACION",
            entidad="ordenes",
            id_entidad=int(id_orden),
            id_usuario=int(id_usuario_solicita),
            datos_anteriores={
                "estado": estado_anterior,
                "total": Decimal(str(orden["total"])),
            },
            datos_nuevos={
                "id_cancelacion": int(id_cancelacion),
                "id_orden": int(id_orden),
                "numero_orden_diaria": int(orden["numero_orden_diaria"]),
                "motivo": nombre_motivo,
                "id_motivo": int(id_motivo),
                "comentario": comentario,
                "id_usuario_solicita": int(id_usuario_solicita),
                "id_usuario_autoriza": int(autorizante["id_usuario"]),
                "autorizante": autorizante["nombre_usuario"],
                "partidas_devueltas": devueltas,
                "partidas_mermadas": mermadas,
            },
        )

    return {
        "id_cancelacion": int(id_cancelacion),
        "id_orden": int(id_orden),
        "numero_dia": int(orden["numero_orden_diaria"]),
        "devueltas": devueltas,
        "mermadas": mermadas,
    }
