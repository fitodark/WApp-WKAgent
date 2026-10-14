# agent/brain_claude.py — Conexión con la API oficial de Anthropic (Claude)
# Generado por AgentKit

"""
Implementación de generar_respuesta() usando el SDK oficial `anthropic`.
Se activa cuando PROVIDER_CHAT=claudeapi (ver agent/brain.py).
"""

import os
import json
import asyncio
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.brain_common import (
    HERRAMIENTAS,
    construir_system_prompt,
    ejecutar_herramienta,
    obtener_mensaje_error,
    obtener_mensaje_fallback,
    folios_conocidos_en_historial,
    validar_folios_en_respuesta,
)
from agent.tools import (
    obtener_contador_sin_intencion,
    reiniciar_contador_sin_intencion,
    obtener_contador_indecision,
    reiniciar_contador_indecision,
)

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# 1024 se quedaba corto en conversaciones largas: el modelo podía agotar el budget
# en razonamiento interno antes de emitir texto visible, devolviendo respuestas sin
# texto (ver _llamar_modelo, mismo tipo de incidente que en brain_openrouter.py).
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "2048"))


def _extraer_texto(response) -> str:
    """Concatena los bloques de texto de una respuesta de Claude."""
    partes = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in partes if p).strip()


async def _llamar_modelo(mensajes: list[dict], system_estable: str, system_volatil: str, intentos: int = 2):
    """
    Llama a la API de Claude y reintenta una vez si la respuesta viene "vacía": sin
    texto y sin tool_use. Es un glitch transitorio del proveedor bajo carga (mismo
    patrón visto en producción el 2026-08-14 vía OpenRouter) — no lanza excepción,
    así que sin este retry se pierde el turno y el cliente recibe un mensaje que le
    echa la culpa a él por "no escribir claro".
    """
    response = None
    for intento in range(intentos):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Prompt caching: dos bloques de system. El primero (menu/catalogos/reglas,
            # ~13-15K tokens) es practicamente identico entre llamadas -> lleva el
            # breakpoint. El segundo (hora actual, contador, cliente) cambia por diseño
            # (la hora cambia cada minuto) y va SIN cache, al final, para no invalidar
            # el bloque grande solo por ese detalle. Como tools se renderiza antes que
            # system, el breakpoint del primer bloque cubre tools+ese bloque juntos.
            system=[
                {
                    "type": "text",
                    "text": system_estable,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": system_volatil,
                },
            ],
            messages=mensajes,
            tools=HERRAMIENTAS,
        )
        cache_leido = getattr(response.usage, "cache_read_input_tokens", 0)
        cache_escrito = getattr(response.usage, "cache_creation_input_tokens", 0)
        logger.info(
            f"Claude/{MODEL} ({response.usage.input_tokens} in / {response.usage.output_tokens} out, "
            f"cache_read={cache_leido} cache_write={cache_escrito}, stop={response.stop_reason})"
        )

        vacia = response.stop_reason != "tool_use" and not _extraer_texto(response)
        if not vacia:
            return response

        logger.warning(
            f"Respuesta vacía de Claude/{MODEL} (stop_reason={response.stop_reason}) "
            f"— intento {intento + 1}/{intentos}"
        )
        if intento < intentos - 1:
            await asyncio.sleep(0.5)

    return response


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    cliente: dict | None = None,
    telefono: str | None = None,
    chat_id: str | None = None,
) -> tuple[str, str | None]:
    """
    Genera una respuesta usando la API de Claude (Anthropic).

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        cliente: Datos del cliente registrado en Wings Kings (None si no está registrado)
        telefono: Número real del remitente (se inyecta del lado servidor)
        chat_id: Identificador de la conversación de WhatsApp (para bloquear_cliente)

    Returns:
        (texto, ruta_adjunto) — ruta_adjunto es la ruta local de una imagen a enviar
        junto con el texto (ej. datos de transferencia), o None si no aplica.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), None

    contador_sin_intencion = await obtener_contador_sin_intencion(chat_id) if chat_id else 0
    contador_indecision = await obtener_contador_indecision(chat_id) if chat_id else 0
    system_estable, system_volatil, _telefono_sucursal = await construir_system_prompt(
        historial, cliente, contador_sin_intencion, contador_indecision
    )

    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    # client_id para registrar el pedido bajo el cliente detectado (None si no está registrado)
    client_id = cliente.get("id") if cliente else None

    # Folios realmente devueltos por registrar_pedido en este turno (guardrail anti-alucinación)
    folios_reales_turno: set[int] = set()
    # True si el modelo llamó marcar_sin_intencion en este turno (evita reiniciar el contador)
    marco_sin_intencion = False
    # True si el modelo llamó marcar_indecision en este turno (evita reiniciar ese contador)
    marco_indecision = False
    # Adjuntos (ej. imagen de datos de transferencia) devueltos por alguna herramienta en
    # este turno, para enviarlos junto con el texto final (ver agent/main.py)
    adjuntos: list[str] = []

    try:
        # Loop de tool-use: el modelo puede invocar registrar_pedido antes de su respuesta final
        for _ in range(5):  # tope de seguridad de iteraciones
            response = await _llamar_modelo(mensajes, system_estable, system_volatil)

            if response.stop_reason != "tool_use":
                if not marco_sin_intencion and chat_id:
                    await reiniciar_contador_sin_intencion(chat_id)
                if not marco_indecision and chat_id:
                    await reiniciar_contador_indecision(chat_id)
                texto_extraido = _extraer_texto(response)
                if not texto_extraido:
                    # Ya se reintentó una vez dentro de _llamar_modelo y siguió vacío —
                    # es un glitch del proveedor, no que el cliente haya escrito algo
                    # confuso, así que usamos el mensaje de error técnico y no el de
                    # "no entendí tu mensaje" (mismo incidente que en brain_openrouter.py).
                    logger.error(
                        f"Claude/{MODEL} devolvió respuesta vacía tras reintento "
                        f"(stop_reason={response.stop_reason}) — chat_id={chat_id}"
                    )
                texto = texto_extraido or obtener_mensaje_error()
                folios_previos = folios_conocidos_en_historial(historial)
                texto = validar_folios_en_respuesta(texto, folios_reales_turno, folios_previos)
                return texto, (adjuntos[-1] if adjuntos else None)

            # Ejecutar las herramientas solicitadas y devolver sus resultados al modelo
            mensajes.append({"role": "assistant", "content": response.content})
            resultados = []
            for bloque in response.content:
                if getattr(bloque, "type", None) != "tool_use":
                    continue
                resultado = await ejecutar_herramienta(bloque.name, bloque.input, client_id, telefono, chat_id)
                if bloque.name == "registrar_pedido" and resultado.get("ok") and resultado.get("folio"):
                    folios_reales_turno.add(int(resultado["folio"]))
                if bloque.name == "marcar_sin_intencion":
                    marco_sin_intencion = True
                if bloque.name == "marcar_indecision":
                    marco_indecision = True
                if bloque.name == "compartir_datos_transferencia" and resultado.get("ok") and resultado.get("adjunto"):
                    adjuntos.append(resultado["adjunto"])
                resultados.append({
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })
            mensajes.append({"role": "user", "content": resultados})

        logger.warning("Se alcanzó el tope de iteraciones de tool-use sin respuesta final (Claude)")
        return obtener_mensaje_error(), None

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error(), None
