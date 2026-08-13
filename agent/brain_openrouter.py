# agent/brain_openrouter.py — Conexión con modelos vía OpenRouter (SDK openai)
# Generado por AgentKit

"""
Implementación de generar_respuesta() usando el SDK `openai` apuntado a la
API de OpenRouter (compatible con Chat Completions + function-calling).
Se activa cuando PROVIDER_CHAT=openrouter (ver agent/brain.py).
"""

import os
import json
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
from agent.tools import obtener_contador_sin_intencion, reiniciar_contador_sin_intencion

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
MAX_TOKENS = int(os.getenv("OPENROUTER_MAX_TOKENS", "1024"))


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


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    cliente: dict | None = None,
    telefono: str | None = None,
    chat_id: str | None = None,
) -> str:
    """
    Genera una respuesta usando un modelo servido por OpenRouter.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        cliente: Datos del cliente registrado en Wings Kings (None si no está registrado)
        telefono: Número real del remitente (se inyecta del lado servidor)
        chat_id: Identificador de la conversación de WhatsApp (para bloquear_cliente)

    Returns:
        La respuesta generada por el modelo
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    contador_sin_intencion = await obtener_contador_sin_intencion(chat_id) if chat_id else 0
    system_prompt, _telefono_sucursal = await construir_system_prompt(historial, cliente, contador_sin_intencion)

    # Prompt caching: OpenRouter pasa el bloque 'cache_control' al proveedor subyacente
    # cuando el modelo es de Anthropic (Claude vía OpenRouter). Mismo breakpoint que en
    # brain_claude.py, mismo objetivo (~13K tokens fijos de menu/catalogos/reglas por
    # llamada). PENDIENTE DE VALIDAR: a diferencia de brain_claude.py (SDK oficial,
    # documentado), aquí no hay garantía de que OpenRouter realmente honre el cache_control
    # para todos los modelos/proveedores que enruta — usar el log de cache_read/cache_write
    # de abajo para confirmarlo en pruebas reales antes de asumir el ahorro.
    mensajes = [{
        "role": "system",
        "content": [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
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

    try:
        # Loop de tool-use: el modelo puede invocar registrar_pedido antes de su respuesta final
        for _ in range(5):  # tope de seguridad de iteraciones
            response = await client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=mensajes,
                tools=TOOLS_OPENAI,
            )
            choice = response.choices[0]
            mensaje_respuesta = choice.message
            usage = response.usage
            # Campos de cache aun sin confirmar en OpenRouter (ver comentario donde se arma
            # 'mensajes' mas arriba) — se registran todos los nombres plausibles para poder
            # detectar en logs reales cual usa cada proveedor/modelo, si alguno.
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

            if choice.finish_reason != "tool_calls" or not mensaje_respuesta.tool_calls:
                if not marco_sin_intencion and chat_id:
                    await reiniciar_contador_sin_intencion(chat_id)
                texto = (mensaje_respuesta.content or "").strip() or obtener_mensaje_fallback()
                folios_previos = folios_conocidos_en_historial(historial)
                return validar_folios_en_respuesta(texto, folios_reales_turno, folios_previos)

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
                mensajes.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })

        logger.warning("Se alcanzó el tope de iteraciones de tool-use sin respuesta final (OpenRouter)")
        return obtener_mensaje_error()

    except Exception as e:
        logger.error(f"Error OpenRouter API: {e}")
        return obtener_mensaje_error()
