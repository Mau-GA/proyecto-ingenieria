"""Pruebas de la Fase 4: KDS (historial, secuencia, tiempos) y delivery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import get_engine
from modules.comandas import _comandas_por_estado
from services.cocina import (
    SECUENCIA_ESTADOS,
    CocinaError,
    cambiar_estado_partida,
    color_por_espera,
    guardar_configuracion_kds,
    obtener_configuracion_kds,
)
from services.mermas import registrar_merma_detalle
from services.ventas import (
    VentaError,
    guardar_orden_mesa,
    limpiar_comandas_listas,
    registrar_comanda,
)

PRODUCTO = {
    "id_producto": 1,
    "cantidad": 1,
    "precio_unitario": 120.00,
    "id_area_preparacion": 1,
}


def _escalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _filas(sql: str, params: dict | None = None) -> list:
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).all()


def _ejecutar(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def _crear_cuenta(mesa: int = 1, producto: dict | None = None):
    resultado = guardar_orden_mesa(
        id_mesa=mesa,
        partidas=[dict(producto or PRODUCTO)],
        id_usuario=1,
        id_empleado=1,
    )
    id_orden = int(resultado["id_orden"])
    id_detalle = int(
        _escalar(
            "SELECT MIN(id_detalle) FROM detalles_orden WHERE id_orden = :o",
            {"o": id_orden},
        )
    )
    return id_orden, id_detalle


def _historial(id_detalle: int) -> list:
    return _filas(
        """
        SELECT ed.nombre, h.id_usuario, h.fecha
        FROM historial_estado_detalle h
        JOIN estados_detalle_orden ed ON ed.id_estado = h.id_estado
        WHERE h.id_detalle = :d
        ORDER BY h.id_historial
        """,
        {"d": id_detalle},
    )


def test_crear_orden_inserta_pendiente_por_cada_partida():
    resultado = registrar_comanda(
        partidas=[{**PRODUCTO, "cantidad": 2}],
        id_tipo_orden=2,  # Para Llevar
        id_usuario=1,
        id_empleado=1,
    )
    ids = [
        int(fila[0])
        for fila in _filas(
            "SELECT id_detalle FROM detalles_orden WHERE id_orden = :o "
            "ORDER BY id_detalle",
            {"o": int(resultado["id_orden"])},
        )
    ]
    assert len(ids) == 2
    for id_detalle in ids:
        filas = _historial(id_detalle)
        assert [(str(nombre), int(usuario)) for nombre, usuario, _ in filas] == [
            ("Pendiente", 1)
        ]


def test_secuencia_completa_guarda_cuatro_filas_con_usuario_y_fechas():
    _, id_detalle = _crear_cuenta()

    cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)
    cambiar_estado_partida(id_detalle, "En preparación", "Listo", 1)
    cambiar_estado_partida(id_detalle, "Listo", "Entregado", 1)

    filas = _historial(id_detalle)
    assert [str(fila[0]) for fila in filas] == list(SECUENCIA_ESTADOS)
    assert [int(fila[1]) for fila in filas] == [1, 1, 1, 1]
    fechas = [fila[2] for fila in filas]
    assert all(b >= a for a, b in zip(fechas, fechas[1:]))
    assert fechas[-1] > fechas[0]
    assert _escalar(
        "SELECT ed.nombre FROM detalles_orden d "
        "JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado "
        "WHERE d.id_detalle = :d",
        {"d": id_detalle},
    ) == "Entregado"


def test_saltos_y_retrocesos_no_cambian_estado_ni_historial():
    _, id_detalle = _crear_cuenta()

    with pytest.raises(CocinaError, match="saltar"):
        cambiar_estado_partida(id_detalle, "Pendiente", "Listo", 1)
    assert _escalar(
        "SELECT ed.nombre FROM detalles_orden d "
        "JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado "
        "WHERE d.id_detalle = :d",
        {"d": id_detalle},
    ) == "Pendiente"
    assert len(_historial(id_detalle)) == 1

    cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)
    cambiar_estado_partida(id_detalle, "En preparación", "Listo", 1)

    with pytest.raises(CocinaError, match="regresar"):
        cambiar_estado_partida(id_detalle, "Listo", "En preparación", 1)
    assert _escalar(
        "SELECT ed.nombre FROM detalles_orden d "
        "JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado "
        "WHERE d.id_detalle = :d",
        {"d": id_detalle},
    ) == "Listo"
    assert len(_historial(id_detalle)) == 3


def test_dos_hilos_solo_logran_un_cambio_y_una_fila_de_historial():
    _, id_detalle = _crear_cuenta()
    barrera = Barrier(2)

    def mover():
        barrera.wait(timeout=10)
        try:
            cambiar_estado_partida(
                id_detalle, "Pendiente", "En preparación", 1
            )
            return ("ok", "")
        except CocinaError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: mover(), range(2)))

    exitos = [r for r in resultados if r[0] == "ok"]
    errores = [r for r in resultados if r[0] == "error"]
    assert len(exitos) == 1
    assert len(errores) == 1
    assert "Otro usuario ya actualizó este producto" in errores[0][1]

    filas = _historial(id_detalle)
    assert [str(fila[0]) for fila in filas] == ["Pendiente", "En preparación"]
    assert _escalar(
        "SELECT ed.nombre FROM detalles_orden d "
        "JOIN estados_detalle_orden ed ON ed.id_estado = d.id_estado "
        "WHERE d.id_detalle = :d",
        {"d": id_detalle},
    ) == "En preparación"


def test_partida_de_orden_final_o_mermada_no_se_puede_cambiar():
    id_orden, id_detalle = _crear_cuenta(mesa=2)
    _ejecutar(
        "UPDATE ordenes SET id_estado = "
        "(SELECT id_estado FROM estados_orden WHERE nombre = 'Pagada') "
        "WHERE id_orden = :o",
        {"o": id_orden},
    )
    with pytest.raises(CocinaError, match="La orden ya está"):
        cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)
    assert len(_historial(id_detalle)) == 1

    _, id_mermado = _crear_cuenta(mesa=3)
    registrar_merma_detalle(
        id_detalle=id_mermado,
        id_usuario=1,
        id_empleado=1,
        motivo="Se quemó",
        liberar_mesa_si_total=False,
    )
    with pytest.raises(CocinaError, match="merma"):
        cambiar_estado_partida(id_mermado, "Pendiente", "En preparación", 1)
    assert len(_historial(id_mermado)) == 1


def test_color_verde_amarillo_y_rojo_segun_recepcion():
    _, id_detalle = _crear_cuenta()

    for minutos, esperado in ((5, "verde"), (15, "amarillo"), (25, "rojo")):
        _ejecutar(
            "UPDATE historial_estado_detalle "
            "SET fecha = CURRENT_TIMESTAMP - make_interval(mins => :m) "
            "WHERE id_detalle = :d",
            {"m": minutos, "d": id_detalle},
        )
        transcurridos = _escalar(
            "SELECT EXTRACT(EPOCH FROM "
            "(CURRENT_TIMESTAMP - MIN(fecha))) / 60.0 "
            "FROM historial_estado_detalle WHERE id_detalle = :d",
            {"d": id_detalle},
        )
        assert color_por_espera(float(transcurridos), 10, 20) == esperado


def test_partida_sin_historial_usa_fecha_de_la_orden():
    _ejecutar(
        "UPDATE detalles_orden SET id_estado = "
        "(SELECT id_estado FROM estados_detalle_orden WHERE nombre = 'Pendiente') "
        "WHERE id_detalle = 1"
    )
    assert _escalar(
        "SELECT COUNT(*) FROM historial_estado_detalle WHERE id_detalle = 1"
    ) == 0

    comandas = _comandas_por_estado()
    fila = comandas[comandas["id_detalle"] == 1].iloc[0]

    fecha_orden = _escalar(
        "SELECT fecha_orden FROM ordenes WHERE id_orden = 1"
    )
    assert not str(fila["fecha_recepcion"]) in ("None", "NaT", "nan")
    assert str(fila["fecha_recepcion"]).startswith(
        str(fecha_orden)[:16]
    )
    assert float(fila["minutos_espera"]) > 0


def test_umbrales_invalidos_se_rechazan():
    amarillo, rojo = (
        obtener_configuracion_kds()["umbral_amarillo_min"],
        obtener_configuracion_kds()["umbral_rojo_min"],
    )

    for valor_amarillo, valor_rojo in (
        (0, 20),
        (10, 0),
        (-5, 20),
        (10, 10),
        (20, 10),
    ):
        with pytest.raises(CocinaError):
            guardar_configuracion_kds(valor_amarillo, valor_rojo)

    config = obtener_configuracion_kds()
    assert config["umbral_amarillo_min"] == amarillo
    assert config["umbral_rojo_min"] == rojo

    with pytest.raises(IntegrityError):
        _ejecutar(
            "UPDATE configuracion_kds SET umbral_rojo_min = 5 "
            "WHERE id_configuracion = 1"
        )


def test_delivery_plataforma_exige_nombre_y_lo_crea_en_la_misma_transaccion():
    ordenes_antes = _escalar("SELECT COUNT(*) FROM ordenes")

    with pytest.raises(VentaError, match="nombre del cliente"):
        registrar_comanda(
            partidas=[dict(PRODUCTO)],
            id_tipo_orden=4,  # Plataforma Externa
            id_usuario=1,
            id_empleado=1,
            id_plataforma=1,
            referencia_externa="EXT-001",
        )
    assert _escalar("SELECT COUNT(*) FROM ordenes") == ordenes_antes

    resultado = registrar_comanda(
        partidas=[dict(PRODUCTO)],
        id_tipo_orden=4,
        id_usuario=1,
        id_empleado=1,
        id_plataforma=1,
        referencia_externa="EXT-002",
        nombre_cliente="Cliente Fase Cuatro",
    )
    id_cliente = int(resultado["id_cliente"])
    assert _escalar(
        "SELECT nombre FROM clientes WHERE id_cliente = :c",
        {"c": id_cliente},
    ) == "Cliente Fase Cuatro"
    assert int(
        _escalar(
            "SELECT id_cliente FROM ordenes WHERE id_orden = :o",
            {"o": int(resultado["id_orden"])},
        )
    ) == id_cliente


def test_mermar_y_repetir_crea_historial_pendiente_de_la_reposicion():
    id_orden, id_detalle = _crear_cuenta(mesa=2)

    registrar_merma_detalle(
        id_detalle=id_detalle,
        id_usuario=1,
        id_empleado=1,
        motivo="Se cayó al emplatar",
        liberar_mesa_si_total=False,
        repetir=True,
    )

    id_reposicion = int(
        _escalar(
            "SELECT MAX(id_detalle) FROM detalles_orden WHERE id_orden = :o",
            {"o": id_orden},
        )
    )
    assert id_reposicion != id_detalle
    filas = _historial(id_reposicion)
    assert [(str(nombre), int(usuario)) for nombre, usuario, _ in filas] == [
        ("Pendiente", 1)
    ]


def test_limpiar_listo_registra_historial_de_entrega():
    _, id_detalle = _crear_cuenta()

    cambiar_estado_partida(id_detalle, "Pendiente", "En preparación", 1)
    cambiar_estado_partida(id_detalle, "En preparación", "Listo", 1)

    assert limpiar_comandas_listas(id_usuario=1) == 1
    filas = _historial(id_detalle)
    assert [str(fila[0]) for fila in filas] == list(SECUENCIA_ESTADOS)
    assert int(filas[-1][1]) == 1
    assert limpiar_comandas_listas(id_usuario=1) == 0
    assert len(_historial(id_detalle)) == 4
