# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, es_numero_bloqueado, marcar_procesado
from agent.providers import obtener_proveedor
from agent.tools import buscar_cliente_por_telefono

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


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


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.

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

            # Verificar si el número está bloqueado — ignorar sin responder
            if await es_numero_bloqueado(msg.telefono):
                logger.warning(f"Número bloqueado ignorado: {msg.telefono}")
                continue

            # Verificar si el cliente está registrado en Wings Kings.
            # Usamos el número real (msg.numero); si el proveedor no lo resolvió,
            # caemos al chatId (válido cuando ya viene como número, no @lid).
            cliente = await buscar_cliente_por_telefono(msg.numero or msg.telefono)
            if cliente:
                logger.info(f"Cliente registrado: {cliente['name']}")

            # Obtener historial ANTES de guardar el mensaje actual
            historial = await obtener_historial(msg.telefono)

            # Generar respuesta con Claude (pasando datos del cliente si existe)
            respuesta = await generar_respuesta(msg.texto, historial, cliente=cliente)

            # Guardar mensaje del usuario Y respuesta del agente
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp
            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta)
            if enviado:
                logger.info(f"Respuesta a {msg.telefono}: {respuesta}")
            else:
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}")

        except Exception as e:
            # Un fallo en un mensaje no debe abortar el resto del lote ni gatillar reintentos
            logger.error(f"Error procesando mensaje {getattr(msg, 'mensaje_id', '?')}: {e}")
            continue

    return {"status": "ok"}
