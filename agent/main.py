# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

import os
import re
import base64
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, marcar_procesado
from agent.providers import obtener_proveedor
from agent.tools import (
    buscar_cliente_por_telefono,
    es_numero_bloqueado,
    obtener_horario,
    guardar_ubicacion_cliente,
)

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO

# Antes solo se logueaba a consola: cuando algo raro pasaba en producción (ej. el
# incidente del 2026-08-14 con respuestas vacías del modelo) no había forma de
# revisarlo después porque la consola no persiste entre reinicios. LOG_DIR es
# configurable por si en producción se prefiere otra ruta (ej. un volumen montado).
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_formato_log = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_handler_archivo = RotatingFileHandler(
    os.path.join(LOG_DIR, "agent.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
)
_handler_archivo.setFormatter(_formato_log)
_handler_consola = logging.StreamHandler()
_handler_consola.setFormatter(_formato_log)
logging.basicConfig(level=log_level, handlers=[_handler_consola, _handler_archivo])
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

# OpenWA a veces entrega el contenido de una imagen o PDF adjunto como base64 crudo en
# el campo 'body' del mensaje, en vez de un caption o una URL (visto en producción el
# 2026-08-14: fotos y comprobantes de pago llegaban como un blob de texto enorme). Ese
# blob NUNCA debe mandarse al modelo — aparte de ser carísimo en tokens, el modelo no
# puede "ver" la imagen desde ahí, solo confunde la conversación y a veces la revienta.
# Se detecta por firma binaria en vez de confiar en un campo del webhook (no documentado
# para eventos entrantes), así funciona sin importar cómo lo mande el proveedor.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_FIRMAS_BINARIAS = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"%PDF", b"GIF87a", b"GIF89a")

RESPUESTA_ADJUNTO_RECIBIDO = (
    "Recibí tu archivo 📎 (imagen o PDF). Por este medio no puedo revisarlo automáticamente: "
    "si es el comprobante de tu pago por transferencia, nuestro equipo lo va a validar "
    "manualmente y en cuanto se confirme seguimos con tu pedido. Si necesitas algo más, "
    "cuéntamelo por texto 😊"
)

# Ubicación compartida por WhatsApp (pin o lugar guardado): confirmado en vivo el
# 2026-08-16 que OpenWA SOLO entrega latitud/longitud, nunca un texto de dirección — así
# que esto se guarda como dato complementario (para el repartidor) pero SIEMPRE se le
# sigue pidiendo la dirección completa por escrito, igual que a cualquier cliente. Se
# resuelve aquí (mecánico, sin juicio del modelo) en vez de mandarlo al LLM.
RESPUESTA_UBICACION_CON_CLIENTE = (
    "📍 ¡Gracias! Guardé tu ubicación como referencia para el repartidor. Aun así, por "
    "WhatsApp no puedo leer la dirección directo del mapa — ¿me compartes tu dirección "
    "completa (calle, número, colonia) y una referencia para armar tu pedido? 😊"
)
RESPUESTA_UBICACION_SIN_CLIENTE = (
    "📍 ¡Gracias por la ubicación! Por WhatsApp no puedo leer la dirección directo del "
    "mapa, así que para tu pedido necesito que me compartas por escrito tu nombre "
    "completo, tu dirección completa y una referencia 😊"
)


def _es_archivo_adjunto_crudo(texto: str) -> bool:
    """True si 'texto' es en realidad el contenido binario (base64) de una imagen o PDF."""
    compacto = "".join(texto.split())
    if len(compacto) < 200 or not _BASE64_RE.match(compacto):
        return False
    try:
        # Un múltiplo de 4 caracteres siempre decodifica sin necesitar padding extra.
        cabecera = base64.b64decode(compacto[:24], validate=True)
    except Exception:
        return False
    return any(cabecera.startswith(firma) for firma in _FIRMAS_BINARIAS)


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

        respuesta, adjunto = await generar_respuesta(
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

        if adjunto:
            if await proveedor.enviar_imagen(telefono, adjunto):
                logger.info(f"Adjunto enviado a {telefono}: {adjunto}")
            else:
                logger.error(f"No se pudo enviar el adjunto a {telefono}: {adjunto}")

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
            if msg.es_propio or (not msg.texto and not msg.ubicacion):
                continue

            # Deduplicacion: si el mismo mensaje_id ya fue procesado, ignorar
            if not await marcar_procesado(msg.mensaje_id):
                logger.info(f"Webhook duplicado ignorado: {msg.mensaje_id}")
                continue

            # Ubicación compartida (pin o lugar guardado) — mecánico, no pasa por el modelo.
            # WhatsApp nunca manda un texto de dirección aquí, solo coordenadas.
            if msg.ubicacion:
                lat, lng = msg.ubicacion
                logger.info(f"Ubicación recibida de {msg.telefono}: {lat}, {lng}")
                guardada = await guardar_ubicacion_cliente(msg.numero or msg.telefono, lat, lng)
                respuesta_ubicacion = (
                    RESPUESTA_UBICACION_CON_CLIENTE if guardada else RESPUESTA_UBICACION_SIN_CLIENTE
                )
                await guardar_mensaje(msg.telefono, "user", f"[Cliente compartió su ubicación: {lat}, {lng}]")
                await guardar_mensaje(msg.telefono, "assistant", respuesta_ubicacion)
                if not await proveedor.enviar_mensaje(msg.telefono, respuesta_ubicacion):
                    logger.error(f"No se pudo enviar el acuse de ubicación a {msg.telefono}")
                continue

            # Adjunto binario (imagen/PDF) entregado como texto crudo — nunca se manda al
            # modelo (ver _es_archivo_adjunto_crudo). Se responde de inmediato, sin pasar
            # por el debounce ni gastar una llamada al modelo.
            if _es_archivo_adjunto_crudo(msg.texto):
                logger.info(f"Adjunto binario detectado de {msg.telefono} ({len(msg.texto)} chars) — no se envía al modelo")
                await guardar_mensaje(msg.telefono, "user", "[Cliente envió un archivo adjunto: imagen o PDF]")
                await guardar_mensaje(msg.telefono, "assistant", RESPUESTA_ADJUNTO_RECIBIDO)
                if not await proveedor.enviar_mensaje(msg.telefono, RESPUESTA_ADJUNTO_RECIBIDO):
                    logger.error(f"No se pudo enviar el acuse de adjunto a {msg.telefono}")
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
