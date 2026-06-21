# agent/providers/openwa.py — Adaptador para OpenWA (whatsapp-web.js self-hosted)
# Generado por AgentKit

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorOpenWA(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando OpenWA (servidor local basado en whatsapp-web.js)."""

    def __init__(self):
        self.base_url = os.getenv("OPENWA_BASE_URL", "http://localhost:2785").rstrip("/")
        self.api_key = os.getenv("OPENWA_API_KEY")
        self.session_id = os.getenv("OPENWA_SESSION_ID", "default")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload del evento message.received de OpenWA."""
        body = await request.json()

        evento = body.get("event")
        data = body.get("data", {}) or {}
        mensaje_id = data.get("id", "")
        logger.info(f"[OpenWA webhook] event={evento} id={mensaje_id} from={data.get('from')} body={data.get('body')!r}")

        # OpenWA emite varios eventos; solo procesamos mensajes entrantes
        if evento != "message.received":
            return []

        # Ignorar mensajes de grupo (el agente es 1:1)
        if data.get("isGroup"):
            return []

        # En OpenWA el id arranca con "true_" cuando el mensaje lo envio el propio numero
        es_propio = mensaje_id.startswith("true_")

        return [MensajeEntrante(
            telefono=data.get("from", ""),
            texto=data.get("body", ""),
            mensaje_id=mensaje_id,
            es_propio=es_propio,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envia un mensaje de texto via la API REST de OpenWA."""
        if not self.api_key:
            logger.warning("OPENWA_API_KEY no configurado — mensaje no enviado")
            return False

        url = f"{self.base_url}/api/sessions/{self.session_id}/messages/send-text"
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"chatId": telefono, "text": mensaje}

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code not in (200, 201):
                logger.error(f"Error OpenWA: {r.status_code} — {r.text}")
                return False
            return True
