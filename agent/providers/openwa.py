# agent/providers/openwa.py — Adaptador para OpenWA (whatsapp-web.js self-hosted)
# Generado por AgentKit

import os
import logging
from urllib.parse import quote
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
        chat_id = data.get("from", "")
        logger.info(f"[OpenWA webhook] event={evento} id={mensaje_id} from={chat_id} body={data.get('body')!r}")

        # OpenWA emite varios eventos; solo procesamos mensajes entrantes
        if evento != "message.received":
            return []

        # Ignorar mensajes de grupo (el agente es 1:1)
        if data.get("isGroup"):
            return []

        # En OpenWA el id arranca con "true_" cuando el mensaje lo envio el propio numero
        es_propio = mensaje_id.startswith("true_")

        # Número real para buscar en clients. Si el chat viene como @lid (WhatsApp
        # multi-device oculta el teléfono), lo resolvemos; si ya es @c.us, el chat_id
        # mismo es el número. 'senderPhone' aparece si OpenWA corre con RESOLVE_LID_TO_PHONE=true.
        if "@lid" in chat_id:
            numero = data.get("senderPhone") or await self._resolver_numero(chat_id)
        else:
            numero = chat_id
        logger.debug(f"[OpenWA] chat={chat_id} numero_real={numero or '(no resuelto)'}")

        return [MensajeEntrante(
            telefono=chat_id,
            texto=data.get("body", ""),
            mensaje_id=mensaje_id,
            es_propio=es_propio,
            numero=numero or "",
        )]

    async def _resolver_numero(self, contact_id: str) -> str:
        """Resuelve un chatId @lid a su número real via la API de contactos de OpenWA."""
        if not self.api_key:
            logger.warning("resolver_numero: OPENWA_API_KEY no configurado")
            return ""
        url = f"{self.base_url}/api/sessions/{self.session_id}/contacts/{quote(contact_id, safe='')}"
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, headers={"X-API-Key": self.api_key})
            logger.debug(f"[OpenWA resolver] GET {url} -> {r.status_code} {r.text[:500]!r}")
            if r.status_code == 200:
                return self._numero_de_contacto(r.json())
        except Exception as e:
            logger.error(f"resolver_numero error para {contact_id}: {e}")
        return ""

    @staticmethod
    def _numero_de_contacto(contacto) -> str:
        """Extrae el número (solo dígitos) de la respuesta del contacto de OpenWA."""
        # La respuesta puede venir envuelta (ej. {"data": {...}})
        if isinstance(contacto, dict) and isinstance(contacto.get("data"), dict):
            contacto = contacto["data"]
        if not isinstance(contacto, dict):
            return ""
        # 1) 'id' con formato @c.us trae el número REAL. OJO: en multi-device el campo
        #    'number' puede traer el @lid (no el teléfono), por eso 'id' tiene prioridad.
        idv = contacto.get("id")
        serial = ""
        if isinstance(idv, dict):
            serial = idv.get("_serialized") or idv.get("user") or ""
        elif isinstance(idv, str):
            serial = idv
        if "@c.us" in serial or "@s.whatsapp.net" in serial:
            return "".join(c for c in serial.split("@")[0] if c.isdigit())
        # 2) Campos directos, solo si 'id' no resolvió a un @c.us
        for clave in ("phone", "phoneNumber", "number", "pn"):
            valor = contacto.get(clave)
            if valor and any(c.isdigit() for c in str(valor)):
                return "".join(c for c in str(valor) if c.isdigit())
        return ""

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
