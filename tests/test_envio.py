# tests/test_envio.py — Pruebas del envío gratuito / express por colonia
#
# Correr con:  python -m pytest tests/test_envio.py -v
#
# Las pruebas de BD usan la MariaDB del .env (tabla tarifas_envio de wingskingslvl) y se
# saltan si no hay conexión. Los pedidos de prueba NUNCA se guardan: el COMMIT se
# reemplaza por ROLLBACK (después de leer la fila insertada para revisarla).

import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agent import tools  # noqa: E402
from agent.tools import _normalizar_colonia, _buscar_en_tarifas  # noqa: E402


def correr(coro):
    return asyncio.run(coro)


# ── Normalización y búsqueda (sin BD) ─────────────────────────────

def _fila(colonia, gratuito=True, monto=None):
    return {"colonia": colonia, "tipo": "colonia", "envio_gratuito": gratuito,
            "monto_express": monto, "_norm": _normalizar_colonia(colonia)}


CATALOGO = [
    _fila("Acatlima", False, 45.0),
    _fila("Aviación 1a Secc"),
    _fila("Aviación 2a. Secc"),
    _fila("Aviación Poniente"),
    _fila("El Rosario", False, 40.0),
    _fila("El Rosario 2da. Sección", False, 40.0),
    _fila("Hacienda el Rosario"),
    _fila("Heroica Ciudad de Huajuapan de León Centro"),
    _fila("Colonia Juquilita"),
    _fila("Las Peñitas"),
]


def test_normalizar_quita_prefijos_acentos_y_ordinales():
    assert _normalizar_colonia("Col. Aviación 1a. Secc") == "aviacion 1 seccion"
    assert _normalizar_colonia("Aviación primera sección") == "aviacion 1 seccion"
    assert _normalizar_colonia("colonia juquilita") == "juquilita"
    assert _normalizar_colonia("Las Peñitas") == "penitas"


def test_busqueda_exacta_y_con_prefijo():
    fila, _ = _buscar_en_tarifas("col. acatlima", CATALOGO)
    assert fila["colonia"] == "Acatlima"
    fila, _ = _buscar_en_tarifas("Aviacion 1ra seccion", CATALOGO)
    assert fila["colonia"] == "Aviación 1a Secc"


def test_nombre_corto_dentro_de_uno_solo():
    fila, _ = _buscar_en_tarifas("Centro", CATALOGO)
    assert fila["colonia"] == "Heroica Ciudad de Huajuapan de León Centro"


def test_varias_coincidencias_devuelve_sugerencias():
    fila, sug = _buscar_en_tarifas("Aviación", CATALOGO)
    assert fila is None
    assert len(sug) == 3 and all(s.startswith("Aviación") for s in sug)


def test_el_rosario_exacto_no_confunde_con_hacienda():
    fila, _ = _buscar_en_tarifas("El Rosario", CATALOGO)
    assert fila["colonia"] == "El Rosario"
    fila, _ = _buscar_en_tarifas("Hacienda el Rosario", CATALOGO)
    assert fila["colonia"] == "Hacienda el Rosario"


def test_error_de_dedo():
    fila, _ = _buscar_en_tarifas("Acatlma", CATALOGO)
    assert fila["colonia"] == "Acatlima"


def test_colonia_inexistente():
    fila, sug = _buscar_en_tarifas("Marte", CATALOGO)
    assert fila is None and sug == []


# ── Con base de datos ─────────────────────────────────────────────

def _hay_bd() -> bool:
    try:
        return bool(correr(tools._cargar_tarifas()))
    except Exception:
        return False


requiere_bd = pytest.mark.skipif(not _hay_bd(), reason="Sin conexión a MariaDB / tarifas_envio")


@requiere_bd
def test_consultar_colonia_solo_express():
    r = correr(tools.consultar_tarifa_envio("Acatlima"))
    assert r["encontrada"] and not r["envio_gratuito"]
    assert r["express_disponible"] and r["monto_express"] == 45.0
    assert r["tiempo_express"] == "30 minutos"
    assert "tiempo_gratuito" not in r


@requiere_bd
def test_consultar_colonia_gratuita_sin_tarifa_express():
    r = correr(tools.consultar_tarifa_envio("Centro"))
    assert r["encontrada"] and r["envio_gratuito"]
    assert r["express_disponible"] is False and "monto_express" not in r
    assert r["tiempo_gratuito"].startswith("entre")


@requiere_bd
def test_sugerencias_no_exponen_montos():
    r = correr(tools.consultar_tarifa_envio("Aviación"))
    assert r["encontrada"] is False and r["sugerencias"]
    assert all(isinstance(s, str) for s in r["sugerencias"])
    assert set(r) == {"encontrada", "sugerencias"}


@requiere_bd
def test_detectar_colonia_en_direccion():
    detectar = tools.detectar_colonia_en_direccion
    assert correr(detectar("Calle Morelos 12, Col. Acatlima")) == "Acatlima"
    assert correr(detectar("Av. Juárez 5, colonia Las Peñitas")) == "Las Peñitas"


# registrar_pedido: horario forzado a abierto y COMMIT -> ROLLBACK

@pytest.fixture
def pedido_sin_guardar(monkeypatch):
    async def abierto():
        return {"esta_abierto": True, "horario": "13:00–24:00"}
    monkeypatch.setattr(tools, "obtener_horario", abierto)

    capturado = {}
    conectar_original = tools.aiomysql.connect

    async def conectar(**kw):
        conn = await conectar_original(**kw)

        async def commit_y_deshacer():
            async with conn.cursor(tools.aiomysql.DictCursor) as cur:
                # La venta recién insertada (aún sin commit) solo la ve esta conexión
                await cur.execute(
                    "SELECT montoTotal, cantidadRecibida, tipo_envio, monto_envio_express, colonia_envio "
                    "FROM ventas ORDER BY ventaId DESC LIMIT 1")
                capturado["venta"] = await cur.fetchone()
            await conn.rollback()

        conn.commit = commit_y_deshacer
        return conn

    monkeypatch.setattr(tools.aiomysql, "connect", conectar)
    return capturado


def _producto_para_llevar() -> dict:
    async def buscar():
        conn = await tools.aiomysql.connect(**tools._db_config())
        async with conn.cursor(tools.aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, price FROM products WHERE active = 1 AND type <> 2 "
                "AND promotion_type = 2 ORDER BY id LIMIT 1")
            fila = await cur.fetchone()
        conn.close()
        return fila
    return correr(buscar())


def _registrar(**extra):
    p = _producto_para_llevar()
    base = dict(
        items=[{"producto_id": p["id"], "cantidad": 1}],
        tipo_entrega="domicilio", cantidad_recibida=1000, metodo_pago="efectivo",
        direccion="Calle de prueba 1 (casa de prueba)", telefono="0000000000",
    )
    base.update(extra)
    return float(p["price"]), correr(tools.registrar_pedido(**base))


@requiere_bd
def test_domicilio_sin_tipo_envio_falla(pedido_sin_guardar):
    _, r = _registrar(colonia="Acatlima")
    assert not r["ok"] and "tipo de envío" in r["error"]


@requiere_bd
def test_gratuito_en_colonia_solo_express_falla(pedido_sin_guardar):
    _, r = _registrar(tipo_envio="gratuito", colonia="Acatlima")
    assert not r["ok"] and "solo tiene envío express" in r["error"]


@requiere_bd
def test_express_en_colonia_sin_tarifa_falla(pedido_sin_guardar):
    _, r = _registrar(tipo_envio="express", colonia="Centro")
    assert not r["ok"] and "solo está disponible el envío gratuito" in r["error"]


@requiere_bd
def test_express_guarda_monto_de_bd_y_no_toca_total(pedido_sin_guardar):
    precio, r = _registrar(tipo_envio="express", colonia="col. acatlima")
    assert r["ok"], r
    assert r["monto_envio_express"] == 45.0 and r["tiempo_entrega"] == "30 minutos"
    assert r["total"] == precio and r["cambio"] == round(1000 - precio, 2)
    v = pedido_sin_guardar["venta"]
    assert v["tipo_envio"] == 2 and float(v["monto_envio_express"]) == 45.0
    assert v["colonia_envio"] == "Acatlima" and float(v["montoTotal"]) == precio


@requiere_bd
def test_gratuito_guarda_tipo_1_sin_monto(pedido_sin_guardar):
    _, r = _registrar(tipo_envio="gratuito", colonia="Centro")
    assert r["ok"], r
    v = pedido_sin_guardar["venta"]
    assert v["tipo_envio"] == 1 and v["monto_envio_express"] is None
    assert v["colonia_envio"] == "Heroica Ciudad de Huajuapan de León Centro"


@requiere_bd
def test_recoger_ignora_envio(pedido_sin_guardar):
    _, r = _registrar(tipo_entrega="recoger", direccion=None, tipo_envio="express", colonia="Acatlima")
    assert r["ok"], r
    v = pedido_sin_guardar["venta"]
    assert v["tipo_envio"] is None and v["monto_envio_express"] is None and v["colonia_envio"] is None
