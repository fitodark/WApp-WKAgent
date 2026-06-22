# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

import os
import json
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.tools import (
    obtener_horario,
    obtener_horario_semanal_texto,
    obtener_menu_desde_db,
    registrar_pedido,
    registrar_cliente,
    obtener_telefono_sucursal,
    obtener_catalogo_config,
)

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Herramientas que el agente puede invocar (tool-use de Claude)
HERRAMIENTAS = [
    {
        "name": "registrar_pedido",
        "description": (
            "Registra en el sistema un pedido YA CONFIRMADO por el cliente y devuelve el "
            "folio de seguimiento. Úsala SOLO cuando el cliente haya confirmado explícitamente: "
            "los productos y cantidades, el total, el tipo de entrega, y (si paga en efectivo) "
            "con cuánto paga. NO la uses para cotizar ni para pedidos tentativos. "
            "Una vez generado el folio el pedido NO se puede modificar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo_entrega": {
                    "type": "string",
                    "enum": ["domicilio", "recoger"],
                    "description": "domicilio = se envía a la dirección; recoger = el cliente pasa al local por él",
                },
                "items": {
                    "type": "array",
                    "description": "Líneas del pedido. Usa el código [#N] del menú como producto_id.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "producto_id": {"type": "integer", "description": "El número N del código [#N] en el menú"},
                            "cantidad": {"type": "integer"},
                            "descripcion": {"type": "string", "description": "Notas de esta línea (opcional). NO usar para alitas/boneless; ahí usa 'desglose'."},
                            "desglose": {
                                "type": "array",
                                "description": (
                                    "OBLIGATORIO para productos de la sección 'Alitas y Boneless'. "
                                    "Desglose por sabor; cada entrada es una porción del pedido."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "piezas": {"type": "string", "description": "Una de las presentaciones disponibles (ej: '5 Piezas', '10 Piezas', 'Mitad')"},
                                        "sabor": {"type": "string", "description": "Uno de los sabores disponibles"},
                                    },
                                    "required": ["piezas", "sabor"],
                                },
                            },
                        },
                        "required": ["producto_id", "cantidad"],
                    },
                },
                "cantidad_recibida": {
                    "type": "number",
                    "description": "Monto con el que paga el cliente en efectivo (para calcular el cambio). Si paga exacto, es igual al total.",
                },
                "direccion": {
                    "type": "string",
                    "description": "Dirección de entrega indicada por el cliente (obligatoria si es a domicilio)",
                },
            },
            "required": ["tipo_entrega", "items", "cantidad_recibida"],
        },
    },
    {
        "name": "registrar_cliente",
        "description": (
            "Da de alta a un cliente NUEVO con su nombre completo y dirección. Úsala SOLO cuando "
            "en 'Cliente actual' diga que NO está registrado y el cliente vaya a hacer un pedido. "
            "Si el número ya existiera, no duplica. No pidas estos datos si el cliente ya está registrado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre completo del cliente"},
                "direccion": {"type": "string", "description": "Dirección completa (para domicilio)"},
                "referencia": {"type": "string", "description": "Referencias o indicaciones de ubicación (opcional)"},
            },
            "required": ["nombre"],
        },
    },
]


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Responde en español.")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


def _extraer_texto(response) -> str:
    """Concatena los bloques de texto de una respuesta de Claude."""
    partes = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in partes if p).strip()


async def _ejecutar_herramienta(nombre: str, args: dict, client_id: int | None, telefono: str | None) -> dict:
    """Ejecuta la herramienta pedida por Claude. client_id y telefono se inyectan del lado servidor."""
    if nombre == "registrar_pedido":
        return await registrar_pedido(client_id=client_id, telefono=telefono, **args)
    if nombre == "registrar_cliente":
        return await registrar_cliente(telefono=telefono, **args)
    logger.warning(f"Herramienta desconocida solicitada: {nombre}")
    return {"ok": False, "error": f"Herramienta no soportada: {nombre}"}


async def generar_respuesta(mensaje: str, historial: list[dict], cliente: dict | None = None, telefono: str | None = None) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        cliente: Datos del cliente registrado en Wings Kings (None si no está registrado)

    Returns:
        La respuesta generada por Claude
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Inyectar menú dinámico desde la base de datos
    menu = await obtener_menu_desde_db()
    system_prompt += f"\n\n## Menú actual (directo de base de datos — precios y productos vigentes)\n{menu}"

    # Inyectar catálogos de sabores y presentaciones (para pedidos de alitas/boneless, type 2)
    sabores = await obtener_catalogo_config("flavors")
    piezas = await obtener_catalogo_config("pieces")
    if sabores:
        system_prompt += (
            "\n\n## Sabores disponibles (alitas y boneless)\n"
            + ", ".join(sabores)
            + "\nCuando un cliente pida alitas o boneless, pregunta el/los sabor(es) y cómo reparte las piezas."
            "\nAl registrar el pedido, los productos de la sección 'Alitas y Boneless' DEBEN incluir el campo"
            " 'desglose' con cada porción {piezas, sabor}, usando EXACTAMENTE estos nombres de sabor y presentación."
        )
    if piezas:
        system_prompt += "\n\n## Presentaciones de piezas\n" + ", ".join(piezas)

    # Reemplazar el placeholder del teléfono de sucursal por el valor real (tabla configs)
    telefono = await obtener_telefono_sucursal()
    system_prompt = system_prompt.replace("[TELÉFONO_SUCURSAL]", telefono)

    # Inyectar el horario semanal vigente (desde configs) para que Claude no use horarios de memoria
    horario_semanal = await obtener_horario_semanal_texto()
    system_prompt += f"\n\n## Horario de atención (vigente)\n{horario_semanal}"

    # Inyectar hora actual y estado abierto/cerrado para que Claude no adivine
    horario = await obtener_horario()
    estado = "ABIERTO" if horario["esta_abierto"] else "CERRADO"
    system_prompt += (
        f"\n\n## Estado actual del negocio"
        f"\n- Día: {horario['dia_actual']}"
        f"\n- Hora: {horario['hora_actual']}"
        f"\n- Estado: **{estado}**"
        f"\n- Horario hoy: {horario['horario']}"
        f"\nUsa esta información para determinar si debes atender el pedido o informar que estamos cerrados."
    )

    # Detectar si es el primer mensaje de la conversación (historial vacío)
    # para que el agente incluya la recomendación de políticas de uso en el saludo
    if not historial:
        system_prompt += (
            "\n\n## PRIMER_MENSAJE_CONVERSACION"
            "\nEste es el PRIMER mensaje de esta conversación. Sigue estrictamente las reglas"
            " de la sección 'Saludo inicial' e incluye al final de tu respuesta la recomendación"
            " sobre las políticas de uso con su enlace."
        )

    # Agregar contexto del cliente al system prompt si está registrado
    if cliente:
        nombre = cliente.get("name", "")
        direccion = cliente.get("address", "")
        referencia = cliente.get("reference", "")
        contexto_cliente = f"\n\n## Cliente actual\nEste cliente SÍ está registrado en Wings Kings."
        if nombre:
            contexto_cliente += f"\n- Nombre: {nombre}"
        if direccion:
            contexto_cliente += f"\n- Dirección registrada: {direccion}"
        if referencia:
            contexto_cliente += f"\n- Referencia: {referencia}"
        contexto_cliente += "\nTratalo por su nombre, salúdalo de vuelta si es la primera vez que escribe en la conversación."
        system_prompt += contexto_cliente
    else:
        system_prompt += "\n\n## Cliente actual\nEste cliente NO está registrado en Wings Kings. Atiéndelo con amabilidad, pero no asumas datos de dirección o preferencias previas."

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

    try:
        # Loop de tool-use: el modelo puede invocar registrar_pedido antes de su respuesta final
        for _ in range(5):  # tope de seguridad de iteraciones
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                messages=mensajes,
                tools=HERRAMIENTAS,
            )
            logger.info(
                f"Claude ({response.usage.input_tokens} in / {response.usage.output_tokens} out, "
                f"stop={response.stop_reason})"
            )

            if response.stop_reason != "tool_use":
                return _extraer_texto(response) or obtener_mensaje_fallback()

            # Ejecutar las herramientas solicitadas y devolver sus resultados al modelo
            mensajes.append({"role": "assistant", "content": response.content})
            resultados = []
            for bloque in response.content:
                if getattr(bloque, "type", None) != "tool_use":
                    continue
                resultado = await _ejecutar_herramienta(bloque.name, bloque.input, client_id, telefono)
                resultados.append({
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })
            mensajes.append({"role": "user", "content": resultados})

        logger.warning("Se alcanzó el tope de iteraciones de tool-use sin respuesta final")
        return obtener_mensaje_error()

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
