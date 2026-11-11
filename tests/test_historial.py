# tests/test_historial.py — Ventana de conversación (6 h) y último pedido anterior
#
# Correr con:  python -m pytest tests/test_historial.py -v
#
# El historial se prueba en una SQLite temporal (no toca agentkit.db). Las pruebas que
# leen ventas/menú usan la MariaDB del .env y se saltan si no hay conexión.

import os
import sys
import asyncio
import tempfile
from datetime import datetime, timedelta

import pytest

# SQLite temporal ANTES de importar agent.memory (el engine se crea al importar)
_DB_TMP = os.path.join(tempfile.mkdtemp(), "historial_test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_TMP}"
os.environ["HISTORIAL_VENTANA_HORAS"] = "6"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agent import memory, tools, brain_common  # noqa: E402
from agent.memory import Mensaje, async_session, inicializar_db, obtener_historial  # noqa: E402
from agent.brain_common import (  # noqa: E402
    obtener_ultimo_pedido_previo,
    validar_folios_en_respuesta,
    folios_conocidos_en_historial,
    MENSAJE_FOLIO_NO_VERIFICADO,
)


def correr(coro):
    return asyncio.run(coro)


async def _sembrar(telefono: str, mensajes: list[tuple[str, str, float]]):
    """mensajes: (role, content, horas_atras)."""
    await inicializar_db()
    ahora = datetime.utcnow()
    async with async_session() as s:
        for role, content, horas in mensajes:
            s.add(Mensaje(telefono=telefono, role=role, content=content,
                          timestamp=ahora - timedelta(hours=horas)))
        await s.commit()


# ── Ventana de conversación ───────────────────────────────────────

def test_historial_viejo_queda_fuera():
    correr(_sembrar("t-viejo", [
        ("user", "quiero alitas", 24 * 35),
        ("assistant", "Listo, tu folio #68707", 24 * 35),
    ]))
    assert correr(obtener_historial("t-viejo")) == []


def test_conversacion_reciente_se_conserva():
    correr(_sembrar("t-reciente", [
        ("user", "hola", 1.0),
        ("assistant", "¡Hola! ¿Qué se te antoja?", 0.99),
        ("user", "unas alitas", 0.5),
    ]))
    h = correr(obtener_historial("t-reciente"))
    assert [m["content"] for m in h] == ["hola", "¡Hola! ¿Qué se te antoja?", "unas alitas"]


def test_corta_en_hueco_mayor_a_la_ventana():
    # Pedido de mediodía (9 h atrás) y nueva plática en la noche (hace 1 h)
    correr(_sembrar("t-hueco", [
        ("user", "pedido de mediodia", 9.0),
        ("assistant", "folio #70001", 8.9),
        ("user", "buenas noches", 1.0),
        ("assistant", "¡Buenas noches!", 0.99),
    ]))
    h = correr(obtener_historial("t-hueco"))
    assert [m["content"] for m in h] == ["buenas noches", "¡Buenas noches!"]


def test_conversacion_larga_sin_huecos_no_se_corta():
    # Mensajes cada 2 h durante 10 h: ningún hueco > 6 h → todo es la misma conversación
    correr(_sembrar("t-larga", [("user", f"m{i}", 10 - 2 * i) for i in range(5)]))
    assert len(correr(obtener_historial("t-larga"))) == 5


# ── Último pedido anterior + candado de folios ────────────────────

def test_ultimo_pedido_se_omite_si_es_de_esta_conversacion(monkeypatch):
    async def falso(_cid):
        return {"folio": 68707, "fecha_texto": "sábado 7 de noviembre", "monto": 250.0}
    monkeypatch.setattr(brain_common, "obtener_ultimo_pedido_agente", falso)
    cliente = {"id": 183}

    historial_actual = [{"role": "assistant", "content": "Listo, tu folio #68707"}]
    assert correr(obtener_ultimo_pedido_previo(cliente, historial_actual)) is None
    assert correr(obtener_ultimo_pedido_previo(cliente, []))["folio"] == 68707
    assert correr(obtener_ultimo_pedido_previo(None, [])) is None


def test_candado_acepta_folio_anterior_inyectado():
    texto = "Veo tu pedido con folio #68707 del sábado; para detalles comunícate a sucursal."
    previos = folios_conocidos_en_historial([])
    # Sin el ajuste, se bloquearía
    assert validar_folios_en_respuesta(texto, set(), previos) == MENSAJE_FOLIO_NO_VERIFICADO
    previos.add(68707)
    assert validar_folios_en_respuesta(texto, set(), previos) == texto


# ── Pruebas contra la MariaDB del .env ────────────────────────────

def _hay_bd() -> bool:
    try:
        return bool(correr(tools.obtener_telefono_sucursal()))
    except Exception:
        return False


requiere_bd = pytest.mark.skipif(not _hay_bd(), reason="Sin conexión a MariaDB")


@requiere_bd
def test_ultimo_pedido_agente_en_bd():
    # FITO (id 183) es el cliente de prueba con pedidos del agente
    p = correr(tools.obtener_ultimo_pedido_agente(183))
    assert p is not None and p["folio"] > 0 and p["fecha_texto"]
    assert correr(tools.obtener_ultimo_pedido_agente(0)) is None


@requiere_bd
def test_prompt_incluye_regla_sin_estatus():
    pedido = {"folio": 68707, "fecha_texto": "sábado 7 de noviembre", "monto": 250.0}
    _, volatil, tel = correr(brain_common.construir_system_prompt([], {"id": 183, "name": "FITO"}, 0, 0, pedido))
    assert "Folio #68707, del sábado 7 de noviembre, por $250.00" in volatil
    assert "NUNCA des su estatus" in volatil and tel in volatil
    _, volatil_sin, _ = correr(brain_common.construir_system_prompt([], {"id": 183, "name": "FITO"}, 0, 0, None))
    assert "Último pedido del cliente" not in volatil_sin
