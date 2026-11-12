"""
Control de acceso basado en roles (RBAC).

Reglas del sistema:
  - Gerente: acceso total. Absorbe los permisos de Administrador e Inventario.
  - Mesero: Mesas, Órdenes y Mermas.
  - Caja: FoodHouse, Órdenes y Mermas.
  - Cocina / Bar: Comandas y Mermas.
  - Inventario: rol retirado; el Gerente absorbe sus funciones.

Los permisos NO son solo cosméticos: cada módulo llama a `requerir_rol`
al inicio de su `render()` y cada acción sensible vuelve a validar con
`requerir_accion(...)` antes de leer/escribir datos. Así, aunque alguien
manipule la sesión o intente forzar una página, el backend bloquea el
acceso.
"""
from __future__ import annotations

import streamlit as st

from auth import usuario_actual

ADMIN = "Administrador"
GERENTE = "Gerente"
MESERO = "Mesero"
COCINA = "Cocina"
BAR = "Bar"
CAJA = "Caja"

# Módulos visibles en el menú lateral.
# clave = id interno del módulo, valor = roles que pueden abrirlo.
# El orden importa: el primer módulo visible es la pantalla inicial.
# Este orden garantiza la navegación pedida para cada rol:
#   Mesero -> Mesas, Órdenes, Mermas
#   Caja   -> FoodHouse, Órdenes, Mermas
#   Cocina/Bar -> Comandas, Mermas
MODULOS: dict[str, tuple[str, ...]] = {
    "foodhouse": (ADMIN, GERENTE, CAJA),
    "mesas": (ADMIN, GERENTE, MESERO),
    "comandas": (ADMIN, GERENTE, COCINA, BAR),
    "ordenes": (ADMIN, GERENTE, MESERO, CAJA),
    "mermas": (ADMIN, GERENTE, MESERO, CAJA, COCINA, BAR),
    "productos": (ADMIN, GERENTE),
    "inventario": (ADMIN, GERENTE),
    "compras": (ADMIN, GERENTE),
    "usuarios": (ADMIN, GERENTE),
    "reportes": (ADMIN, GERENTE),
}

# Acciones sensibles dentro de un módulo. Permiten separar, por ejemplo,
# "crear orden" (Mesero) de "cobrar orden" (Caja) aunque ambos entren a
# modules/ordenes.py.
ACCIONES: dict[str, tuple[str, ...]] = {
    "foodhouse.vender": (ADMIN, GERENTE, CAJA),
    "mesas.abrir_orden": (ADMIN, GERENTE, MESERO),
    "ordenes.crear": (ADMIN, GERENTE, MESERO),
    "ordenes.cobrar": (ADMIN, GERENTE, CAJA),
    "ordenes.eliminar_partida": (ADMIN, GERENTE, CAJA),
    "comandas.preparar": (ADMIN, GERENTE, COCINA, BAR),
    "comandas.limpiar": (GERENTE,),
    "mermas.reportar": (ADMIN, GERENTE, MESERO, CAJA, COCINA, BAR),
    "mermas.gestionar": (ADMIN, GERENTE),
    "productos.gestionar": (ADMIN, GERENTE),
    "inventario.gestionar": (ADMIN, GERENTE),
    "compras.registrar": (ADMIN, GERENTE),
    "usuarios.gestionar": (ADMIN, GERENTE),
    "reportes.ver": (ADMIN, GERENTE),
}


def roles_usuario() -> list[str]:
    """Roles efectivos del usuario en sesión.

    El Gerente absorbe al Administrador: si tiene el rol Gerente también
    se le considera Administrador para cualquier validación futura.
    """
    auth = usuario_actual()
    if not auth:
        return []

    roles = list(auth.get("roles") or [])
    if GERENTE in roles and ADMIN not in roles:
        roles.append(ADMIN)
    return roles


def tiene_rol(*roles_permitidos: str) -> bool:
    if not roles_permitidos:
        return False
    roles = set(roles_usuario())
    return bool(roles.intersection(roles_permitidos))


def puede_modulo(modulo: str) -> bool:
    return tiene_rol(*MODULOS.get(modulo, ()))


def puede_accion(accion: str) -> bool:
    return tiene_rol(*ACCIONES.get(accion, ()))


def requerir_rol(*roles_permitidos: str) -> None:
    """Detiene la página actual si el usuario no tiene ninguno de los roles.

    Se llama al inicio de cada módulo y antes de acciones sensibles.
    """
    if not tiene_rol(*roles_permitidos):
        st.error("🚫 Acceso Denegado: tu rol no tiene permiso para acceder a esta sección.")
        st.caption("Si crees que es un error, contacta al Gerente o al Administrador.")
        st.stop()


def requerir_modulo(modulo: str) -> None:
    requerir_rol(*MODULOS.get(modulo, ()))


def requerir_accion(accion: str) -> None:
    requerir_rol(*ACCIONES.get(accion, ()))


def modulos_visibles() -> list[str]:
    """Módulos que el usuario puede ver en el sidebar, en orden de menú."""
    roles = set(roles_usuario())
    return [modulo for modulo, permitidos in MODULOS.items() if roles.intersection(permitidos)]
