# agent/brain_openrouter.py — Conexión con modelos vía OpenRouter (SDK openai)
# Generado por AgentKit

"""
Implementación de generar_respuesta() usando el SDK `openai` apuntado a la
API de OpenRouter (compatible con Chat Completions + function-calling).
Se activa cuando PROVIDER_CHAT=openrouter (ver agent/brain.py).
"""

import os
import json
import asyncio
import logging
from openai import AsyncOpenAI
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

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
# 1024 se quedaba corto en conversaciones largas: el modelo podía agotar el budget
# en razonamiento interno antes de emitir texto visible, devolviendo content vacío
# con finish_reason="length" (visto en producción el 2026-08-14, ver _llamar_modelo).
MAX_TOKENS = int(os.getenv("OPENROUTER_MAX_TOKENS", "2048"))


def _convertir_herramientas_openai(herramientas: list[dict]) -> list[dict]:
    """Convierte las herramientas del formato Anthropic (input_schema) al formato
    de function-calling de OpenAI (function.parameters), que es el que espera
    OpenRouter en Chat Completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": h["name"],
                "description": h["description"],
                "parameters": h["input_schema"],
            },
        }
        for h in herramientas
    ]


TOOLS_OPENAI = _convertir_herramientas_openai(HERRAMIENTAS)


async def _llamar_modelo(mensajes: list[dict], intentos: int = 2):
    """
    Llama a OpenRouter y reintenta una vez si la respuesta viene "vacía": sin texto
    y sin tool_calls. Esto es un glitch transitorio del proveedor bajo carga (visto
    en producción el 2026-08-14, varias veces seguidas en una sola conversación
    durante un pico de concurrencia) — no un error de la API que lance excepción,
    así que sin este retry se pierde el turno completo y el cliente recibe un
    mensaje que le echa la culpa a él por "no escribir claro".
    """
    respuesta = None
    for intento in range(intentos):
        respuesta = await client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=mensajes,
            tools=TOOLS_OPENAI,
        )
        choice = respuesta.choices[0]
        usage = respuesta.usage
        detalle_prompt = getattr(usage, "prompt_tokens_details", None)
        cache_leido = (
            getattr(usage, "cache_read_input_tokens", None)
            or getattr(detalle_prompt, "cached_tokens", None)
            or "?"
        )
        cache_escrito = getattr(usage, "cache_creation_input_tokens", "?")
        logger.info(
            f"OpenRouter/{MODEL} ({getattr(usage, 'prompt_tokens', '?')} in / "
            f"{getattr(usage, 'completion_tokens', '?')} out, cache_read={cache_leido} "
            f"cache_write={cache_escrito}, finish_reason={choice.finish_reason})"
        )

        vacia = not choice.message.tool_calls and not (choice.message.content or "").strip()
        if not vacia:
            return respuesta

        logger.warning(
            f"Respuesta vacía de OpenRouter/{MODEL} (finish_reason={choice.finish_reason}) "
            f"— intento {intento + 1}/{intentos}"
        )
        if intento < intentos - 1:
            await asyncio.sleep(0.5)

    return respuesta


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    cliente: dict | None = None,
    telefono: str | None = None,
    chat_id: str | None = None,
) -> tuple[str, str | None]:
    """
    Genera una respuesta usando un modelo servido por OpenRouter.

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

    # Prompt caching: OpenRouter pasa el bloque 'cache_control' al proveedor subyacente
    # cuando el modelo es de Anthropic (Claude vía OpenRouter) — confirmado con pruebas
    # reales el 2026-08-12 (cache_read_input_tokens != 0 en varias llamadas del mismo log).
    # Dos bloques: el estable (menu/catalogos/reglas) lleva el breakpoint; el volatil (hora
    # actual, contador, cliente) va sin cache al final — mezclarlos invalidaba el cache
    # cada vez que cambiaba de minuto, aunque el resto del prompt fuera idéntico.
    mensajes = [{
        "role": "system",
        "content": [
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
    }]
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
            response = await _llamar_modelo(mensajes)
            choice = response.choices[0]
            mensaje_respuesta = choice.message

            if choice.finish_reason != "tool_calls" or not mensaje_respuesta.tool_calls:
                if not marco_sin_intencion and chat_id:
                    await reiniciar_contador_sin_intencion(chat_id)
                if not marco_indecision and chat_id:
                    await reiniciar_contador_indecision(chat_id)
                contenido = (mensaje_respuesta.content or "").strip()
                if not contenido:
                    # Ya se reintentó una vez dentro de _llamar_modelo y siguió vacío —
                    # esto es un glitch del proveedor, no que el cliente haya escrito
                    # algo confuso, así que usamos el mensaje de error técnico y no el
                    # de "no entendí tu mensaje" (ver incidente 2026-08-14).
                    logger.error(
                        f"OpenRouter/{MODEL} devolvió respuesta vacía tras reintento "
                        f"(finish_reason={choice.finish_reason}) — chat_id={chat_id}"
                    )
                texto = contenido or obtener_mensaje_error()
                folios_previos = folios_conocidos_en_historial(historial)
                texto = validar_folios_en_respuesta(texto, folios_reales_turno, folios_previos)
                return texto, (adjuntos[-1] if adjuntos else None)

            # Ejecutar las herramientas solicitadas y devolver sus resultados al modelo
            mensajes.append({
                "role": "assistant",
                "content": mensaje_respuesta.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in mensaje_respuesta.tool_calls
                ],
            })
            for tool_call in mensaje_respuesta.tool_calls:
                nombre = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                resultado = await ejecutar_herramienta(nombre, args, client_id, telefono, chat_id)
                if nombre == "registrar_pedido" and resultado.get("ok") and resultado.get("folio"):
                    folios_reales_turno.add(int(resultado["folio"]))
                if nombre == "marcar_sin_intencion":
                    marco_sin_intencion = True
                if nombre == "marcar_indecision":
                    marco_indecision = True
                if nombre == "compartir_datos_transferencia" and resultado.get("ok") and resultado.get("adjunto"):
                    adjuntos.append(resultado["adjunto"])
                mensajes.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })

        logger.warning("Se alcanzó el tope de iteraciones de tool-use sin respuesta final (OpenRouter)")
        return obtener_mensaje_error(), None

    except Exception as e:
        logger.error(f"Error OpenRouter API: {e}")
        return obtener_mensaje_error(), None
