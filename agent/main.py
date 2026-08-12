# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, marcar_procesado
from agent.providers import obtener_proveedor
from agent.tools import buscar_cliente_por_telefono, es_numero_bloqueado, obtener_horario

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Debounce: si el cliente manda varios mensajes seguidos (ej. los va escribiendo en
# burbujas separadas), se espera este tiempo de inactividad antes de contestar, para
# juntarlos en un solo turno en vez de responder cada uno por separado. Cada mensaje
# nuevo reinicia la espera. Solo funciona correctamente con un único proceso/worker
# (el estado vive en memoria); no aplica si se corre con varios workers de uvicorn.
DEBOUNCE_SEGUNDOS = float(os.getenv("MENSAJE_DEBOUNCE_SEGUNDOS", "4"))

_buffer_mensajes: dict[str, list] = {}
_debounce_tasks: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="Wings Kings Agent — WhatsApp AI",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "service": "wings-kings-agent"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


async def _procesar_lote(telefono: str):
    """
    Espera DEBOUNCE_SEGUNDOS de inactividad y, si nadie la canceló (es decir, no llegó
    otro mensaje de este número mientras tanto), procesa TODOS los mensajes que se
    hayan acumulado en el buffer como un solo turno y contesta una sola vez.
    """
    try:
        await asyncio.sleep(DEBOUNCE_SEGUNDOS)
    except asyncio.CancelledError:
        # Llegó un mensaje nuevo antes de tiempo: se reprograma un debounce nuevo
        # (ver webhook_handler), este intento se descarta sin procesar nada.
        return

    lote = _buffer_mensajes.pop(telefono, [])
    _debounce_tasks.pop(telefono, None)
    if not lote:
        return

    try:
        if await es_numero_bloqueado(telefono):
            logger.warning(f"Número bloqueado ignorado: {telefono}")
            return

        # Le da al cliente la sensación de que su mensaje ya está siendo atendido mientras
        # se genera la respuesta (que puede tardar varios segundos, sobre todo con tool-use)
        await proveedor.enviar_estado_escribiendo(telefono)

        # Fuera de horario, no se llama al modelo — cada llamada cuesta tokens reales
        # (~13K de system prompt) aunque solo sea para decir "estamos cerrados". Se
        # responde con un mensaje fijo, sin gastar nada de la API del proveedor de IA.
        horario = await obtener_horario()
        if not horario["esta_abierto"]:
            respuesta = (
                f"Hey! 👋 Por el momento estamos cerrados. Nuestro horario es: {horario['horario']}. "
                "¡Escríbenos cuando estemos abiertos y con gusto te atendemos! 🍗"
            )
            for m in lote:
                await guardar_mensaje(telefono, "user", m.texto)
            await guardar_mensaje(telefono, "assistant", respuesta)
            enviado = await proveedor.enviar_mensaje(telefono, respuesta)
            if enviado:
                logger.info(f"Respuesta de horario (sin llamar al modelo) a {telefono}: {respuesta}")
            else:
                logger.error(f"No se pudo enviar la respuesta de horario a {telefono}")
            return

        # Número real para buscar en clients (el más reciente que se haya resuelto en el lote)
        numero_real = next((m.numero for m in reversed(lote) if m.numero), telefono)

        cliente = await buscar_cliente_por_telefono(numero_real)
        if cliente:
            logger.info(f"Cliente registrado: {cliente['name']}")

        # Obtener historial ANTES de guardar los mensajes de este lote
        historial = await obtener_historial(telefono)

        # Varios mensajes seguidos del cliente se combinan en un solo turno para el modelo
        texto_combinado = "\n".join(m.texto for m in lote)

        respuesta = await generar_respuesta(
            texto_combinado, historial, cliente=cliente, telefono=numero_real, chat_id=telefono
        )

        # Guardar cada mensaje del usuario tal cual llegó (preserva el historial real)
        for m in lote:
            await guardar_mensaje(telefono, "user", m.texto)
        await guardar_mensaje(telefono, "assistant", respuesta)

        enviado = await proveedor.enviar_mensaje(telefono, respuesta)
        if enviado:
            logger.info(f"Respuesta a {telefono} ({len(lote)} mensaje(s) agrupados): {respuesta}")
        else:
            logger.error(f"No se pudo enviar la respuesta a {telefono}")

    except Exception as e:
        # Un fallo en un lote no debe gatillar reintentos del webhook
        logger.error(f"Error procesando lote de {telefono}: {e}")


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.

    No contesta cada mensaje al instante: lo encola por número y reinicia un debounce
    de DEBOUNCE_SEGUNDOS (ver _procesar_lote). Así, si el cliente manda varios mensajes
    seguidos, se juntan en una sola respuesta en vez de contestar cada uno por separado.

    SIEMPRE responde 200: los errores se aíslan por mensaje y se registran en el log.
    Devolver 500 haría que el proveedor (p.ej. OpenWA) reintente y reprocese el mismo
    mensaje, así que nunca propagamos la excepción al webhook.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:
        logger.error(f"No se pudo parsear el webhook: {e}")
        return {"status": "ok"}

    for msg in mensajes:
        try:
            if msg.es_propio or not msg.texto:
                continue

            # Deduplicacion: si el mismo mensaje_id ya fue procesado, ignorar
            if not await marcar_procesado(msg.mensaje_id):
                logger.info(f"Webhook duplicado ignorado: {msg.mensaje_id}")
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Encolar y (re)iniciar el debounce de este número
            _buffer_mensajes.setdefault(msg.telefono, []).append(msg)
            tarea_previa = _debounce_tasks.get(msg.telefono)
            if tarea_previa and not tarea_previa.done():
                tarea_previa.cancel()
            _debounce_tasks[msg.telefono] = asyncio.create_task(_procesar_lote(msg.telefono))

        except Exception as e:
            # Un fallo en un mensaje no debe abortar el resto del lote ni gatillar reintentos
            logger.error(f"Error procesando mensaje {getattr(msg, 'mensaje_id', '?')}: {e}")
            continue

    return {"status": "ok"}
