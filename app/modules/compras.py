"""Módulo de Compras a proveedores: registra la compra y actualiza el stock."""
import streamlit as st

from auth import usuario_actual
from database import run_query, execute
from permissions import requerir_rol, requerir_accion, ADMIN, GERENTE


def render():
    requerir_rol(ADMIN, GERENTE)
    st.subheader("🛒 Compras a proveedores")

    auth = usuario_actual()
    if auth["id_empleado"] is None:
        st.error(
            "Tu cuenta de usuario no está vinculada a una ficha de empleado, "
            "así que no puede registrar compras. Pide a un administrador que la "
            "vincule en el módulo Usuarios."
        )
        return

    proveedores = run_query("SELECT id_proveedor, nombre FROM proveedores WHERE activo = TRUE ORDER BY nombre")
    insumos = run_query("SELECT id_insumo, nombre FROM insumos WHERE activo = TRUE ORDER BY nombre")
    if proveedores.empty or insumos.empty:
        st.warning("Registra al menos un proveedor y un insumo antes de capturar una compra.")
        return

    carrito = st.session_state.setdefault("carrito_compra", [])

    proveedor = st.selectbox("Proveedor", proveedores.to_dict("records"), format_func=lambda p: p["nombre"])

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        insumo_sel = st.selectbox("Insumo", insumos.to_dict("records"), format_func=lambda i: i["nombre"])
    with col2:
        cantidad = st.number_input("Cantidad", min_value=0.001, step=0.5, format="%.3f")
    with col3:
        costo = st.number_input("Costo unitario", min_value=0.0, step=1.0, format="%.2f")

    if st.button("➕ Agregar a la compra"):
        carrito.append({
            "id_insumo": int(insumo_sel["id_insumo"]), "nombre": insumo_sel["nombre"],
            "cantidad": float(cantidad), "costo_unitario": float(costo),
        })

    if carrito:
        st.markdown("#### Partidas de la compra")
        total = 0.0
        for i, item in enumerate(carrito):
            subtotal = item["cantidad"] * item["costo_unitario"]
            total += subtotal
            c1, c2 = st.columns([5, 1])
            c1.write(f"{item['cantidad']} × {item['nombre']} — ${subtotal:.2f}")
            if c2.button("Quitar", key=f"quitar_compra_{i}"):
                carrito.pop(i)
                st.rerun()
        st.markdown(f"**Total de la compra: ${total:.2f}**")

        if st.button("✅ Registrar compra", type="primary"):
            requerir_accion("compras.registrar")
            id_compra = execute(
                "INSERT INTO compras (id_proveedor, id_empleado, total) VALUES (:p, :e, :t)",
                {"p": int(proveedor["id_proveedor"]), "e": auth["id_empleado"], "t": total},
            )
            id_tipo_entrada = run_query(
                "SELECT id_tipo_movimiento FROM tipos_movimiento_inventario WHERE nombre = 'Entrada por compra'"
            ).iloc[0]["id_tipo_movimiento"]

            for item in carrito:
                execute(
                    """
                    INSERT INTO detalle_compra (id_compra, id_insumo, cantidad, costo_unitario)
                    VALUES (:c, :i, :cant, :costo)
                    """,
                    {"c": id_compra, "i": item["id_insumo"], "cant": item["cantidad"], "costo": item["costo_unitario"]},
                )
                execute(
                    """
                    INSERT INTO movimientos_inventario (id_insumo, id_tipo_movimiento, cantidad, motivo, id_empleado, id_compra)
                    VALUES (:i, :t, :c, 'Entrada por compra a proveedor', :e, :compra)
                    """,
                    {"i": item["id_insumo"], "t": int(id_tipo_entrada), "c": item["cantidad"],
                     "e": auth["id_empleado"], "compra": id_compra},
                )
                execute(
                    "UPDATE insumos SET stock_actual = stock_actual + :c WHERE id_insumo = :i",
                    {"c": item["cantidad"], "i": item["id_insumo"]},
                )

            st.session_state["carrito_compra"] = []
            st.success(f"Compra #{id_compra} registrada y stock actualizado.")
            st.rerun()
