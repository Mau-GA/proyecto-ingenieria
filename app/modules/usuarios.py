"""Módulo de Usuarios y Roles (Gerente y Administrador)."""
from datetime import date

import bcrypt
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database import run_query, execute, transaccion
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE


def _hash_password(password_plano: str) -> str:
    return bcrypt.hashpw(password_plano.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _crear_usuario_con_roles(
    *,
    id_empleado: int | None,
    nombre_usuario: str,
    correo: str | None,
    password_hash: str,
    ids_roles: list[int],
) -> int:
    """Crea usuario y roles en una sola transacción."""
    with transaccion() as conn:
        id_usuario = conn.execute(
            text(
                """
                INSERT INTO usuarios
                    (id_empleado, nombre_usuario, correo, password_hash)
                VALUES (:emp, :u, :c, :p)
                RETURNING id_usuario
                """
            ),
            {
                "emp": id_empleado,
                "u": nombre_usuario,
                "c": correo,
                "p": password_hash,
            },
        ).scalar_one()
        for id_rol in ids_roles:
            conn.execute(
                text(
                    "INSERT INTO usuarios_roles (id_usuario, id_rol) "
                    "VALUES (:u, :r)"
                ),
                {"u": int(id_usuario), "r": int(id_rol)},
            )
    return int(id_usuario)


def _render_gestion_empleados() -> None:
    """Lista y alta de fichas de empleado."""
    empleados = run_query(
        """
        SELECT e.id_empleado, e.nombre, e.apellido, e.telefono,
               p.nombre AS puesto, e.salario, e.estatus, e.fecha_ingreso
        FROM empleados e
        JOIN puestos p ON p.id_puesto = e.id_puesto
        ORDER BY e.nombre, e.apellido
        """
    )
    st.dataframe(empleados, use_container_width=True, hide_index=True)

    puestos = run_query("SELECT id_puesto, nombre FROM puestos ORDER BY nombre")
    if puestos.empty:
        st.warning("No hay puestos registrados; registra uno antes de dar de alta empleados.")
        return

    with st.form("nuevo_empleado"):
        nombre = st.text_input("Nombre", max_chars=60)
        apellido = st.text_input("Apellido", max_chars=60)
        telefono = st.text_input("Teléfono (opcional)", max_chars=20)
        puesto = st.selectbox(
            "Puesto",
            puestos.to_dict("records"),
            format_func=lambda p: p["nombre"],
        )
        salario = st.number_input(
            "Salario (opcional, usa 0 si no aplica)",
            min_value=0.0,
            value=0.0,
            step=100.0,
            format="%.2f",
        )
        fecha_ingreso = st.date_input(
            "Fecha de ingreso",
            value=date.today(),
            format="DD/MM/YYYY",
        )
        estatus = st.selectbox("Estatus", ["Activo", "Inactivo", "Baja"])
        enviado = st.form_submit_button("Dar de alta empleado")

    if not enviado:
        return

    requerir_accion("usuarios.gestionar")
    if not nombre.strip() or not apellido.strip():
        st.error("Nombre y apellido son obligatorios.")
        return

    try:
        execute(
            """
            INSERT INTO empleados
                (nombre, apellido, telefono, id_puesto, salario,
                 fecha_ingreso, fecha_baja, estatus)
            VALUES
                (:nombre, :apellido, :telefono, :puesto, :salario,
                 :fecha_ingreso, :fecha_baja, :estatus)
            RETURNING id_empleado
            """,
            {
                "nombre": nombre.strip(),
                "apellido": apellido.strip(),
                "telefono": telefono.strip() or None,
                "puesto": int(puesto["id_puesto"]),
                "salario": salario if salario > 0 else None,
                "fecha_ingreso": fecha_ingreso,
                "fecha_baja": date.today() if estatus == "Baja" else None,
                "estatus": estatus,
            },
        )
    except SQLAlchemyError as exc:
        st.error(f"No se pudo dar de alta al empleado: {exc}")
        return

    st.success(f"Empleado '{nombre.strip()} {apellido.strip()}' dado de alta.")
    st.rerun()


def _render_gestion_cuentas() -> None:
    """Lista de usuarios y alta de cuentas vinculadas a empleados."""
    usuarios = run_query(
        """
        SELECT u.id_usuario, u.nombre_usuario, u.correo, u.activo, u.ultimo_acceso,
               STRING_AGG(r.nombre::text, ', ' ORDER BY r.nombre) AS roles
        FROM usuarios u
        LEFT JOIN usuarios_roles ur ON ur.id_usuario = u.id_usuario
        LEFT JOIN roles r ON r.id_rol = ur.id_rol
        GROUP BY u.id_usuario
        ORDER BY u.nombre_usuario
        """
    )
    st.dataframe(usuarios, use_container_width=True, hide_index=True)

    empleados = run_query(
        """
        SELECT e.id_empleado, CONCAT(e.nombre, ' ', e.apellido) AS nombre_completo
        FROM empleados e
        LEFT JOIN usuarios u ON u.id_empleado = e.id_empleado
        WHERE u.id_usuario IS NULL AND e.estatus = 'Activo'
        ORDER BY e.nombre, e.apellido
        """
    )
    roles = run_query(
        """
        SELECT id_rol, nombre
        FROM roles
        WHERE activo = TRUE
          AND nombre NOT IN (:limpieza, :inventario)
        ORDER BY nombre
        """,
        {"limpieza": "Limpieza", "inventario": "Inventario"},
    )
    if roles.empty:
        st.warning("No hay roles disponibles para asignar.")
        return

    st.caption(
        "Solo aparecen empleados activos que todavía no tienen una cuenta vinculada."
    )
    with st.form("nuevo_usuario"):
        opciones_empleado = [
            {"id_empleado": None, "nombre_completo": "— Sin vincular a empleado —"}
        ] + empleados.to_dict("records")
        empleado = st.selectbox(
            "Empleado a vincular (opcional)",
            opciones_empleado,
            format_func=lambda e: e["nombre_completo"],
        )
        nombre_usuario = st.text_input("Nombre de usuario")
        correo = st.text_input("Correo (opcional)")
        password = st.text_input("Contraseña", type="password")
        roles_sel = st.multiselect(
            "Roles",
            roles.to_dict("records"),
            format_func=lambda r: r["nombre"],
        )
        enviado = st.form_submit_button("Crear usuario")

    if not enviado:
        return

    requerir_accion("usuarios.gestionar")
    if not nombre_usuario.strip() or not password:
        st.error("Nombre de usuario y contraseña son obligatorios.")
        return
    if not roles_sel:
        st.error("Asigna al menos un rol.")
        return

    password_hash = _hash_password(password)
    try:
        _crear_usuario_con_roles(
            id_empleado=empleado["id_empleado"],
            nombre_usuario=nombre_usuario.strip(),
            correo=correo.strip() or None,
            password_hash=password_hash,
            ids_roles=[int(rol["id_rol"]) for rol in roles_sel],
        )
    except IntegrityError as exc:
        origen = getattr(exc, "orig", None)
        codigo = getattr(origen, "pgcode", None)
        restriccion = getattr(getattr(origen, "diag", None), "constraint_name", None)
        if codigo == "23505" and restriccion == "uq_usuario_nombre":
            st.error("Ya existe un usuario con ese nombre.")
        elif codigo == "23505" and restriccion == "uq_usuario_correo":
            st.error("Ya existe un usuario con ese correo.")
        elif codigo == "23505" and restriccion == "uq_usuario_empleado":
            st.error("Ese empleado ya tiene una cuenta de usuario.")
        else:
            st.error("No se pudo crear el usuario: revisa los roles asignados.")
        return
    except SQLAlchemyError as exc:
        st.error(f"No se pudo crear el usuario: {exc}")
        return

    st.success(f"Usuario '{nombre_usuario}' creado.")
    st.rerun()


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("👤 Usuarios y roles")

    tab_empleados, tab_cuentas = st.tabs(
        ["Gestión de Empleados", "Gestión de Cuentas de Usuario"]
    )

    with tab_empleados:
        _render_gestion_empleados()

    with tab_cuentas:
        _render_gestion_cuentas()
