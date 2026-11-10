"""Módulo de Usuarios y Roles (Gerente y Administrador)."""
import bcrypt
import streamlit as st

from database import run_query, execute
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE


def _hash_password(password_plano: str) -> str:
    return bcrypt.hashpw(password_plano.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("👤 Usuarios y roles")

    tab_lista, tab_nuevo, tab_empleados = st.tabs(["Usuarios", "Nuevo usuario", "Empleados"])

    with tab_lista:
        usuarios = run_query(
            """
            SELECT u.id_usuario, u.nombre_usuario, u.correo, u.activo, u.ultimo_acceso,
                   GROUP_CONCAT(r.nombre SEPARATOR ', ') AS roles
            FROM usuarios u
            LEFT JOIN usuarios_roles ur ON ur.id_usuario = u.id_usuario
            LEFT JOIN roles r ON r.id_rol = ur.id_rol
            GROUP BY u.id_usuario
            ORDER BY u.nombre_usuario
            """
        )
        st.dataframe(usuarios, use_container_width=True, hide_index=True)

    with tab_nuevo:
        empleados = run_query(
            """
            SELECT e.id_empleado, CONCAT(e.nombre, ' ', e.apellido) AS nombre_completo
            FROM empleados e
            LEFT JOIN usuarios u ON u.id_empleado = e.id_empleado
            WHERE u.id_usuario IS NULL AND e.estatus = 'Activo'
            ORDER BY e.nombre
            """
        )
        roles = run_query("SELECT id_rol, nombre FROM roles WHERE activo = TRUE ORDER BY nombre")

        with st.form("nuevo_usuario"):
            opciones_empleado = [{"id_empleado": None, "nombre_completo": "— Sin vincular a empleado —"}] + empleados.to_dict("records")
            empleado = st.selectbox("Empleado a vincular (opcional)", opciones_empleado, format_func=lambda e: e["nombre_completo"])
            nombre_usuario = st.text_input("Nombre de usuario")
            correo = st.text_input("Correo (opcional)")
            password = st.text_input("Contraseña", type="password")
            roles_sel = st.multiselect("Roles", roles.to_dict("records"), format_func=lambda r: r["nombre"])
            enviado = st.form_submit_button("Crear usuario")

        if enviado:
            requerir_accion("usuarios.gestionar")
            if not nombre_usuario.strip() or not password:
                st.error("Nombre de usuario y contraseña son obligatorios.")
            elif not roles_sel:
                st.error("Asigna al menos un rol.")
            else:
                id_usuario = execute(
                    """
                    INSERT INTO usuarios (id_empleado, nombre_usuario, correo, password_hash)
                    VALUES (:emp, :u, :c, :p)
                    """,
                    {"emp": empleado["id_empleado"], "u": nombre_usuario.strip(),
                     "c": correo.strip() or None, "p": _hash_password(password)},
                )
                for rol in roles_sel:
                    execute(
                        "INSERT INTO usuarios_roles (id_usuario, id_rol) VALUES (:u, :r)",
                        {"u": id_usuario, "r": int(rol["id_rol"])},
                    )
                st.success(f"Usuario '{nombre_usuario}' creado.")
                st.rerun()

    with tab_empleados:
        empleados_full = run_query(
            """
            SELECT e.id_empleado, e.nombre, e.apellido, p.nombre AS puesto, e.estatus, e.fecha_ingreso
            FROM empleados e JOIN puestos p ON p.id_puesto = e.id_puesto
            ORDER BY e.nombre
            """
        )
        st.dataframe(empleados_full, use_container_width=True, hide_index=True)
        st.caption("Para dar de alta empleados nuevos desde aquí, amplía este formulario siguiendo el mismo patrón que 'Nuevo usuario'.")
