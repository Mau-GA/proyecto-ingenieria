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

from database import run_query, execute

# Claves de sesión que pertenecen a un usuario/módulo y deben limpiarse al
# cambiar de sesión para no filtrar estado entre usuarios.
_CLAVES_SESION = (
    "modulo_activo",
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
    # Widgets de captura de mermas: no deben arrastrar el motivo capturado
    # de una sesión a otra.
    for clave in list(st.session_state.keys()):
        if str(clave).startswith("merma_"):
            st.session_state.pop(clave, None)


def login(nombre_usuario: str, password: str) -> bool:
    nombre_usuario = (nombre_usuario or "").strip()
    if not nombre_usuario or not password:
        return False

    df = run_query(
        """
        SELECT id_usuario, nombre_usuario, password_hash, activo
        FROM usuarios
        WHERE nombre_usuario = :u
        """,
        {"u": nombre_usuario},
    )
    if df.empty or not bool(df.iloc[0]["activo"]):
        return False

    usuario = df.iloc[0]
    if not _verificar_password(password, usuario["password_hash"]):
        return False

    id_usuario = int(usuario["id_usuario"])
    roles = _roles_activos(id_usuario)
    if not roles:
        # Sin roles no hay módulos autorizados: no se crea sesión.
        return False

    empleado_df = run_query(
        "SELECT id_empleado FROM usuarios WHERE id_usuario = :id", {"id": id_usuario}
    )
    id_empleado = empleado_df.iloc[0]["id_empleado"]

    _limpiar_estado_sesion()
    st.session_state["auth"] = {
        "id_usuario": id_usuario,
        "nombre_usuario": usuario["nombre_usuario"],
        "roles": roles,
        # Puede ser None: hay usuarios administrativos sin ficha de empleado
        # (ver comentario en db/schema.sql sobre `usuarios.id_empleado`).
        # Los módulos que registran movimientos de inventario/compras
        # requieren un id_empleado válido y deben avisar si es None.
        "id_empleado": None if id_empleado is None or id_empleado != id_empleado else int(id_empleado),
    }
    execute(
        "UPDATE usuarios SET ultimo_acceso = NOW() WHERE id_usuario = :id",
        {"id": id_usuario},
    )
    return True


def logout() -> None:
    st.session_state.pop("auth", None)
    _limpiar_estado_sesion()


def usuario_actual() -> dict | None:
    return st.session_state.get("auth")


def esta_autenticado() -> bool:
    auth = usuario_actual()
    return bool(auth and auth.get("roles"))
