# tests/test_impresion.py — Impresión de la orden de cocina/barra tras registrar el pedido
#
# Correr con:  python -m pytest tests/test_impresion.py -v
#
# Ninguna prueba imprime un ticket real: la de Laravel usa un folio que no es del
# agente (responde 404 sin imprimir).

import os
import sys
import asyncio

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agent import tools, brain_common  # noqa: E402


def correr(coro):
    return asyncio.run(coro)


def _cliente_falso(monkeypatch, handler):
    """Reemplaza httpx.AsyncClient en tools por uno con transporte simulado."""
    real = httpx.AsyncClient

    def fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)
    monkeypatch.setattr(tools.httpx, "AsyncClient", fabrica)


def test_sin_configuracion_no_llama(monkeypatch):
    monkeypatch.setenv("LARAVEL_API_URL", "")
    r = correr(tools.imprimir_orden_cocina(123))
    assert r["ok"] is False and r["status"] is None


def test_impresion_ok_manda_token_y_url(monkeypatch):
    monkeypatch.setenv("LARAVEL_API_URL", "http://laravel.test/")
    monkeypatch.setenv("AGENTE_TOKEN", "secreto")
    vistos = {}

    def handler(req):
        vistos["url"] = str(req.url)
        vistos["token"] = req.headers.get("X-Agente-Token")
        return httpx.Response(200, json={"success": True, "folio": 123, "cocina": True, "barra": None})
    _cliente_falso(monkeypatch, handler)

    r = correr(tools.imprimir_orden_cocina(123))
    assert r["ok"] is True
    assert vistos == {"url": "http://laravel.test/api/agente/ventas/123/imprimir-orden", "token": "secreto"}


@pytest.mark.parametrize("status,cuerpo", [
    (422, {"success": False, "error": "sabores no cuadran"}),
    (502, {"success": False, "cocina": False, "barra": None}),
    (503, {"success": False, "error": "printStatus"}),
])
def test_errores_de_laravel_no_lanzan(monkeypatch, status, cuerpo):
    monkeypatch.setenv("LARAVEL_API_URL", "http://laravel.test")
    monkeypatch.setenv("AGENTE_TOKEN", "x")
    _cliente_falso(monkeypatch, lambda req: httpx.Response(status, json=cuerpo))
    r = correr(tools.imprimir_orden_cocina(1))
    assert r["ok"] is False and r["status"] == status


def test_laravel_caido_no_lanza(monkeypatch):
    monkeypatch.setenv("LARAVEL_API_URL", "http://laravel.test")
    monkeypatch.setenv("AGENTE_TOKEN", "x")

    def handler(req):
        raise httpx.ConnectError("sin conexión")
    _cliente_falso(monkeypatch, handler)
    assert correr(tools.imprimir_orden_cocina(1))["ok"] is False


# ── Disparo desde registrar_pedido ────────────────────────────────

@pytest.mark.parametrize("resultado,debe_imprimir", [
    ({"ok": True, "folio": 70001}, True),
    ({"ok": True, "folio": 70001, "duplicado": True}, False),
    ({"ok": False, "error": "x"}, False),
])
def test_imprime_solo_folios_nuevos(monkeypatch, resultado, debe_imprimir):
    impresos = []

    async def registrar_falso(**kw):
        return resultado

    async def imprimir_falso(folio):
        impresos.append(folio)
        return {"ok": True}
    monkeypatch.setattr(brain_common, "registrar_pedido", registrar_falso)
    monkeypatch.setattr(brain_common, "imprimir_orden_cocina", imprimir_falso)

    async def flujo():
        r = await brain_common.ejecutar_herramienta("registrar_pedido", {}, 183, "5219535409577", "chat")
        await asyncio.sleep(0)  # deja correr la tarea en segundo plano
        await asyncio.sleep(0)
        return r
    assert correr(flujo()) == resultado
    assert impresos == ([70001] if debe_imprimir else [])


# ── Contra el Laravel local (sin imprimir) ────────────────────────

def _laravel_arriba() -> bool:
    base = os.getenv("LARAVEL_API_URL", "")
    if not base:
        return False
    try:
        httpx.get(base, timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _laravel_arriba(), reason="Laravel local no responde")
def test_laravel_real_autoriza_token():
    # Folio 1 no es del agente → 404 (token aceptado, no imprime nada). Con token malo sería 401.
    r = correr(tools.imprimir_orden_cocina(1))
    assert r["status"] == 404, r
