# agent/brain_common.py — Lógica compartida entre proveedores de IA (Claude API / OpenRouter)
# Generado por AgentKit

"""
Todo lo que NO depende del SDK de un proveedor específico: definición de
herramientas (formato Anthropic, se convierte a formato OpenAI donde haga
falta), construcción del system prompt con datos de Wings Kings (menú,
sabores, horario, cliente) y ejecución de las herramientas de negocio.

agent/brain_claude.py y agent/brain_openrouter.py importan de aquí para no
duplicar reglas de negocio entre proveedores.
"""

import re
import yaml
import logging
from agent.tools import (
    obtener_horario,
    obtener_horario_semanal_texto,
    obtener_menu_desde_db,
    registrar_pedido,
    registrar_cliente,
    obtener_telefono_sucursal,
    obtener_catalogo_config,
    bloquear_numero,
    incrementar_contador_sin_intencion,
)

logger = logging.getLogger("agentkit")

# Herramientas que el agente puede invocar (formato nativo Anthropic tool-use;
# cada proveedor las adapta a su propio formato si lo requiere — ver
# brain_openrouter.py para la conversión a formato OpenAI function-calling).
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
    {
        "name": "marcar_sin_intencion",
        "description": (
            "Marca que el mensaje ACTUAL del cliente NO mostró ninguna intención de compra ni de "
            "pedir información de productos (charla sin rumbo, temas ajenos al negocio, preguntas "
            "sin relación). Llámala SOLO cuando el prompt te indique evaluar esto (después de 1+ "
            "mensajes seguidos así). El resultado te dice el conteo actual y si ya se bloqueó la "
            "conversación automáticamente (al llegar a 3) — redacta tu respuesta según ese resultado. "
            "NO la llames si el cliente SÍ muestra intención de compra en su mensaje."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "bloquear_cliente",
        "description": (
            "Bloquea la conversación de este número por incumplir las políticas de uso del servicio "
            "(ej. 3 mensajes seguidos sin ninguna intención de compra, spam, mal uso evidente). "
            "Úsala SOLO cuando la regla correspondiente del prompt indique bloquear — no la uses por "
            "iniciativa propia. Después de llamarla, el cliente ya NO va a recibir más respuestas hasta "
            "que un humano lo desbloquee manualmente, así que responde su despedida ANTES o en el mismo "
            "turno en que la invocas, nunca después."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Motivo breve y concreto del bloqueo"},
            },
            "required": ["motivo"],
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


async def construir_system_prompt(
    historial: list[dict], cliente: dict | None, contador_sin_intencion: int = 0
) -> tuple[str, str, str]:
    """
    Arma el system prompt del turno en dos bloques, para aprovechar prompt caching
    (ver agent/brain_claude.py y agent/brain_openrouter.py):

    - system_estable: base (prompts.yaml) + menú vigente + catálogos de sabores/piezas +
      horario semanal. Prácticamente el mismo texto en cada llamada (cambia solo si el
      menú/catálogos cambian en BD) — es el bloque que se marca con cache_control.
    - system_volatil: hora actual (cambia cada minuto), contador de sin-intención y
      contexto del cliente — cambia entre llamadas por diseño, por eso va SIN cache,
      en un bloque aparte al final. Si esto se mezclara con el bloque estable, cualquier
      cambio (ej. cruzar de minuto) invalidaría también la parte cacheable.

    Args:
        contador_sin_intencion: mensajes SEGUIDOS de este cliente, hasta antes de este turno,
            sin intención de compra (llevado en código — ver memory.py). Se inyecta al prompt
            para que el modelo sepa en qué número va, en vez de tener que contarlo él mismo
            leyendo el historial (poco confiable).

    Returns:
        (system_estable, system_volatil, telefono_sucursal) — telefono_sucursal es el
        teléfono configurado del negocio (tabla configs), usado para reemplazar el
        placeholder [TELÉFONO_SUCURSAL].
    """
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
    telefono_sucursal = await obtener_telefono_sucursal()
    system_prompt = system_prompt.replace("[TELÉFONO_SUCURSAL]", telefono_sucursal)

    # Inyectar el horario semanal vigente (desde configs) para que el modelo no use horarios de memoria
    horario_semanal = await obtener_horario_semanal_texto()
    system_prompt += f"\n\n## Horario de atención (vigente)\n{horario_semanal}"

    # ════════════════════════════════════════════════════════════
    # A partir de aquí es la parte VOLÁTIL (sin cache) — ver docstring.
    # ════════════════════════════════════════════════════════════
    system_estable = system_prompt
    system_volatil = ""

    # Inyectar hora actual y estado abierto/cerrado para que el modelo no adivine
    horario = await obtener_horario()
    estado = "ABIERTO" if horario["esta_abierto"] else "CERRADO"
    system_volatil += (
        f"\n\n## Estado actual del negocio"
        f"\n- Día: {horario['dia_actual']}"
        f"\n- Hora: {horario['hora_actual']}"
        f"\n- Estado: **{estado}**"
        f"\n- Horario hoy: {horario['horario']}"
        f"\nUsa esta información para determinar si debes atender el pedido o informar que estamos cerrados."
    )

    # Guía sobre mensajes sin intención de compra — el conteo lo lleva el código (memory.py),
    # el modelo solo decide si ESTE mensaje muestra intención o no. SIEMPRE se inyecta (incluso
    # con contador=0): si esto solo apareciera cuando contador > 0, el contador nunca podría
    # pasar de 0 a 1 — el modelo jamás recibiría instrucción de llamar la tool la primera vez.
    system_volatil += (
        f"\n\n## Mensajes sin intención de compra"
        f"\nEste cliente lleva {contador_sin_intencion} mensaje(s) SEGUIDO(s) sin ninguna intención"
        " de compra ni de pedir información de productos (charla sin rumbo, temas ajenos al negocio)."
        "\nEvalúa ESTE mensaje: si TAMPOCO muestra intención de compra, DEBES llamar a la"
        " herramienta marcar_sin_intencion (incluso si el contador de arriba está en 0 — así se"
        " registra el primero). Su resultado te dice el conteo actualizado y si ya se bloqueó la"
        " conversación (al llegar a 3):"
        "\n- Si el conteo resultante es 1: responde normal, sin mencionar nada de strikes."
        "\n- Si el conteo resultante es 2 y NO se bloqueó: avísale que se le aplicó un strike por"
        " infringir las políticas de uso del servicio (mensajes sin intención de compra) y que al"
        " 3er strike se le niega el servicio."
        "\n- Si SÍ se bloqueó (conteo 3): despídete indicando que se le aplicó el tercer strike, su"
        " conversación queda bloqueada por infringir las políticas de uso, y que para pedir más"
        f" adelante se comunique directamente a la sucursal al {telefono_sucursal}."
        "\nSi en cambio ESTE mensaje SÍ muestra intención de compra (pregunta por un producto,"
        " precio, menú, o quiere pedir), NO llames marcar_sin_intencion y atiéndelo con normalidad."
        "\nEXCEPCIÓN — un saludo simple (ej. 'hola', 'buenas tardes', 'buenas noches', 'qué tal')"
        " NUNCA cuenta como sin intención, aunque sea el único contenido del mensaje: es la forma"
        " normal de abrir la plática en WhatsApp antes de pedir. Ante un saludo, NO llames"
        " marcar_sin_intencion — respóndelo con normalidad y pregúntale en qué le ayudas."
        "\nMisma EXCEPCIÓN para una despedida simple (ej. 'gracias', 'ok gracias', 'hasta luego',"
        " 'nos vemos', 'bye', 'de nada'), sobre todo justo después de un pedido ya confirmado: es"
        " el cierre normal de la conversación, no cuenta como sin intención. NO llames"
        " marcar_sin_intencion — respóndela con algo breve y amable."
    )

    # Detectar si es el primer mensaje de la conversación (historial vacío)
    # para que el agente incluya la recomendación de políticas de uso en el saludo
    if not historial:
        system_volatil += (
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
        system_volatil += contexto_cliente
    else:
        system_volatil += "\n\n## Cliente actual\nEste cliente NO está registrado en Wings Kings. Atiéndelo con amabilidad, pero no asumas datos de dirección o preferencias previas."

    return system_estable, system_volatil, telefono_sucursal


async def ejecutar_herramienta(
    nombre: str, args: dict, client_id: int | None, telefono: str | None, chat_id: str | None = None
) -> dict:
    """
    Ejecuta la herramienta pedida por el modelo. client_id y telefono se inyectan del lado
    servidor. chat_id es el identificador de conversación de WhatsApp (el mismo que usa
    memory.py para historial/bloqueo — puede diferir de 'telefono', que es el número real
    del cliente resuelto para buscar en la BD de clientes).
    """
    if nombre == "registrar_pedido":
        return await registrar_pedido(client_id=client_id, telefono=telefono, **args)
    if nombre == "registrar_cliente":
        return await registrar_cliente(telefono=telefono, **args)
    if nombre == "bloquear_cliente":
        if not chat_id:
            return {"ok": False, "error": "No se pudo determinar la conversación a bloquear."}
        motivo = args.get("motivo") or "Incumplimiento de políticas de uso"
        await bloquear_numero(chat_id, motivo)
        logger.warning(f"Número bloqueado por el agente ({chat_id}): {motivo}")
        return {"ok": True}
    if nombre == "marcar_sin_intencion":
        if not chat_id:
            return {"ok": False, "error": "No se pudo determinar la conversación."}
        conteo = await incrementar_contador_sin_intencion(chat_id)
        if conteo >= 3:
            motivo = "3 mensajes seguidos sin intención de compra"
            await bloquear_numero(chat_id, motivo)
            logger.warning(f"Número bloqueado automáticamente por 3 strikes ({chat_id})")
            return {"ok": True, "conteo": conteo, "bloqueado": True}
        logger.info(f"Strike sin intención de compra ({chat_id}): {conteo}/3")
        return {"ok": True, "conteo": conteo, "bloqueado": False}
    logger.warning(f"Herramienta desconocida solicitada: {nombre}")
    return {"ok": False, "error": f"Herramienta no soportada: {nombre}"}


# ════════════════════════════════════════════════════════════
# Guardrail anti-alucinación de folios
#
# Ningún modelo es 100% confiable en seguir "nunca inventes un folio" solo
# por instrucción de prompt (se confirmó con Haiku 4.5: le dio al cliente
# folios que nunca se insertaron en `ventas`). Esta validación es la última
# línea de defensa, a nivel de código: si el texto final menciona un folio
# que no vino de un INSERT real (`registrar_pedido` con ok=True) en este
# turno, ni de un folio ya confirmado antes en la misma conversación, la
# respuesta se descarta y se reemplaza por un mensaje seguro.
# ════════════════════════════════════════════════════════════

_PATRON_FOLIO = re.compile(r"folio[^\d]{0,20}(\d{4,7})", re.IGNORECASE | re.DOTALL)

MENSAJE_FOLIO_NO_VERIFICADO = (
    "Dame un momento para confirmar los datos exactos de tu pedido antes de darte el folio — "
    "en unos segundos te lo confirmo con el número correcto."
)


def extraer_folios_mencionados(texto: str) -> set[int]:
    """Extrae los números de folio que aparecen en un texto (patrón 'folio #NNNNN')."""
    return {int(m) for m in _PATRON_FOLIO.findall(texto or "")}


def folios_conocidos_en_historial(historial: list[dict]) -> set[int]:
    """
    Folios que el agente ya confirmó en turnos anteriores de esta conversación.
    Son de fiar porque, de aquí en adelante, todo folio que se le manda al
    cliente ya pasó por validar_folios_en_respuesta().
    """
    folios: set[int] = set()
    for msg in historial:
        if msg.get("role") == "assistant":
            folios |= extraer_folios_mencionados(msg.get("content", ""))
    return folios


def validar_folios_en_respuesta(texto: str, folios_reales_turno: set[int], folios_previos: set[int]) -> str:
    """
    Si el texto menciona un folio que no es ni uno recién obtenido de
    registrar_pedido en este turno, ni uno ya confirmado antes en la
    conversación, se considera una alucinación: se bloquea la respuesta.
    """
    mencionados = extraer_folios_mencionados(texto)
    conocidos = folios_reales_turno | folios_previos
    if mencionados and not mencionados.issubset(conocidos):
        logger.error(
            f"Folio no verificado en respuesta del modelo — mencionados={mencionados} "
            f"conocidos={conocidos}. Respuesta bloqueada: {texto!r}"
        )
        return MENSAJE_FOLIO_NO_VERIFICADO
    return texto
