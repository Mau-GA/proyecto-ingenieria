"""
Autenticación y manejo de sesión.

Roles esperados en la tabla `roles` (ver db/seed.sql): Administrador,
Gerente, Mesero, Cocina, Bar y Caja. El rol Inventario quedó retirado y
sus funciones las absorbe el Gerente. Este módulo valida credenciales y
carga roles/permisos; app.py, el sidebar y cada módulo de negocio deciden
qué mostrar/permitir según esos roles (ver permissions.py).

Un usuario sin roles activos no puede iniciar sesión: no tendría ningún
módulo autorizado y quedaría en un estado inconsistente.
"""
import bcrypt
import streamlit as st
from sqlalchemy import text

from database import run_query, transaccion
from services.bitacora import registrar_evento

# Claves de sesión que pertenecen a un usuario/módulo y deben limpiarse al
# cambiar de sesión para no filtrar estado entre usuarios.
_CLAVES_SESION = (
    "modulo_activo",
    "nav_modulo",
    "navegar_a",
    "mesa_seleccionada",
    "carrito",
    "carrito_compra",
    "foodhouse_carrito",
    "foodhouse_ultima_comanda",
)

# Roles retirados de la operación: se ignoran al iniciar sesión aunque la
# fila siga existiendo en usuarios_roles. Sus funciones las absorbe el Gerente.
_ROLES_RETIRADOS = {"Inventario"}


def _verificar_password(password_plano: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password_plano.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Hash corrupto o con formato inválido (p. ej. el placeholder del seed
        # antes de haberlo regenerado con set_admin_password.py)
        return False


def _roles_activos(id_usuario: int) -> list[str]:
    roles_df = run_query(
        """
        SELECT r.nombre
        FROM usuarios_roles ur
        JOIN roles r ON r.id_rol = ur.id_rol
        WHERE ur.id_usuario = :id_usuario AND r.activo = TRUE
        ORDER BY r.nombre
        """,
        {"id_usuario": id_usuario},
    )
    roles = roles_df["nombre"].tolist() if not roles_df.empty else []
    return [rol for rol in roles if rol not in _ROLES_RETIRADOS]


def _limpiar_estado_sesion() -> None:
    for clave in _CLAVES_SESION:
        st.session_state.pop(clave, None)
    # Widgets de captura de mermas y compras: no deben arrastrar datos
    # capturados de una sesión a otra.
    for clave in list(st.session_state.keys()):
        if str(clave).startswith(("merma_", "compras_")):
            st.session_state.pop(clave, None)


def _registrar_intento(
    nombre_usuario: str,
    id_usuario: int | None,
    tipo_evento: str,
    motivo: str | None = None,
) -> None:
    """Registra LOGIN/LOGIN_FALLIDO/LOGOUT sin tocar la sesión de Streamlit."""
    with transaccion() as conn:
        registrar_evento(
            conn,
            tipo_evento=tipo_evento,
            entidad="autenticacion",
            id_entidad=id_usuario,
            id_usuario=id_usuario,
            datos_nuevos={
                "nombre_usuario": (nombre_usuario or "").strip(),
                "motivo": motivo,
            },
        )


def _validar_credenciales(nombre_usuario: str, password: str) -> dict | None:
    """
    Valida un usuario/contraseña y registra el intento en la bitácora.

    No modifica `st.session_state`: la usa `login` y también la cancelación
    autorizada, que necesita validar credenciales sin abrir una sesión nueva.
    """
    nombre_usuario = (nombre_usuario or "").strip()
    if not nombre_usuario or not password:
        _registrar_intento(
            nombre_usuario, None, "LOGIN_FALLIDO", "Credenciales incompletas"
        )
        return None

    df = run_query(
        """
        SELECT id_usuario, nombre_usuario, password_hash, activo
        FROM usuarios
        WHERE nombre_usuario = :u
        """,
        {"u": nombre_usuario},
    )
    if df.empty:
        _registrar_intento(nombre_usuario, None, "LOGIN_FALLIDO", "Usuario inexistente")
        return None

    usuario = df.iloc[0]
    id_usuario = int(usuario["id_usuario"])
    if not bool(usuario["activo"]):
        _registrar_intento(
            nombre_usuario, id_usuario, "LOGIN_FALLIDO", "Usuario inactivo"
        )
        return None
    if not _verificar_password(password, usuario["password_hash"]):
        _registrar_intento(
            nombre_usuario, id_usuario, "LOGIN_FALLIDO", "Contraseña incorrecta"
        )
        return None

    roles = _roles_activos(id_usuario)
    if not roles:
        # Sin roles no hay módulos autorizados: no se crea sesión.
        _registrar_intento(
            nombre_usuario, id_usuario, "LOGIN_FALLIDO", "Usuario sin roles activos"
        )
        return None

    empleado_df = run_query(
        "SELECT id_empleado FROM usuarios WHERE id_usuario = :id", {"id": id_usuario}
    )
    id_empleado = empleado_df.iloc[0]["id_empleado"]
    id_empleado = (
        None
        if id_empleado is None or id_empleado != id_empleado
        else int(id_empleado)
    )

    with transaccion() as conn:
        conn.execute(
            text("UPDATE usuarios SET ultimo_acceso = NOW() WHERE id_usuario = :id"),
            {"id": id_usuario},
        )
        registrar_evento(
            conn,
            tipo_evento="LOGIN",
            entidad="autenticacion",
            id_entidad=id_usuario,
            id_usuario=id_usuario,
            datos_nuevos={
                "nombre_usuario": str(usuario["nombre_usuario"]),
                "roles": roles,
            },
        )

    return {
        "id_usuario": id_usuario,
        "nombre_usuario": str(usuario["nombre_usuario"]),
        "roles": roles,
        # Puede ser None: hay usuarios administrativos sin ficha de empleado
        # (ver comentario en db/schema.sql sobre `usuarios.id_empleado`).
        # Los módulos que registran movimientos de inventario/compras
        # requieren un id_empleado válido y deben avisar si es None.
        "id_empleado": id_empleado,
    }


def login(nombre_usuario: str, password: str) -> bool:
    sesion = _validar_credenciales(nombre_usuario, password)
    if sesion is None:
        return False

    _limpiar_estado_sesion()
    st.session_state["auth"] = sesion
    return True


def logout() -> None:
    auth = usuario_actual()
    if auth:
        try:
            _registrar_intento(
                str(auth.get("nombre_usuario") or ""),
                int(auth["id_usuario"]),
                "LOGOUT",
            )
        except Exception:
            # El cierre de sesión no debe fallar si la bitácora no responde.
            pass
    st.session_state.pop("auth", None)
    _limpiar_estado_sesion()


def usuario_actual() -> dict | None:
    return st.session_state.get("auth")


def esta_autenticado() -> bool:
    auth = usuario_actual()
    return bool(auth and auth.get("roles"))
