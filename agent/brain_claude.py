# agent/brain_claude.py — Conexión con la API oficial de Anthropic (Claude)
# Generado por AgentKit

"""
Implementación de generar_respuesta() usando el SDK oficial `anthropic`.
Se activa cuando PROVIDER_CHAT=claudeapi (ver agent/brain.py).
"""

import os
import json
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
from agent.tools import obtener_contador_sin_intencion, reiniciar_contador_sin_intencion

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))


def _extraer_texto(response) -> str:
    """Concatena los bloques de texto de una respuesta de Claude."""
    partes = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in partes if p).strip()


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    cliente: dict | None = None,
    telefono: str | None = None,
    chat_id: str | None = None,
) -> str:
    """
    Genera una respuesta usando la API de Claude (Anthropic).

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        cliente: Datos del cliente registrado en Wings Kings (None si no está registrado)
        telefono: Número real del remitente (se inyecta del lado servidor)
        chat_id: Identificador de la conversación de WhatsApp (para bloquear_cliente)

    Returns:
        La respuesta generada por Claude
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    contador_sin_intencion = await obtener_contador_sin_intencion(chat_id) if chat_id else 0
    system_prompt, _telefono_sucursal = await construir_system_prompt(historial, cliente, contador_sin_intencion)

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

    try:
        # Loop de tool-use: el modelo puede invocar registrar_pedido antes de su respuesta final
        for _ in range(5):  # tope de seguridad de iteraciones
            response = await client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                # Prompt caching: el bloque de system (menu/catalogos/reglas, ~13K tokens fijos
                # por llamada) se cachea con un breakpoint al final. Como tools se renderiza
                # antes que system, este unico breakpoint cubre tools+system juntos. Un pedido
                # normal hace ~10-12 llamadas con este mismo prefijo -> la primera "escribe" el
                # cache (~1.25x costo) y las demas lo "leen" (~10% costo).
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=mensajes,
                tools=HERRAMIENTAS,
            )
            cache_leido = getattr(response.usage, "cache_read_input_tokens", 0)
            cache_escrito = getattr(response.usage, "cache_creation_input_tokens", 0)
            logger.info(
                f"Claude/{MODEL} ({response.usage.input_tokens} in / {response.usage.output_tokens} out, "
                f"cache_read={cache_leido} cache_write={cache_escrito}, stop={response.stop_reason})"
            )

            if response.stop_reason != "tool_use":
                if not marco_sin_intencion and chat_id:
                    await reiniciar_contador_sin_intencion(chat_id)
                texto = _extraer_texto(response) or obtener_mensaje_fallback()
                folios_previos = folios_conocidos_en_historial(historial)
                return validar_folios_en_respuesta(texto, folios_reales_turno, folios_previos)

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
                resultados.append({
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })
            mensajes.append({"role": "user", "content": resultados})

        logger.warning("Se alcanzó el tope de iteraciones de tool-use sin respuesta final (Claude)")
        return obtener_mensaje_error()

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
