# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Historial de conversación y deduplicación de webhooks — local al agente (SQLite).

El bloqueo de números y el contador de mensajes sin intención de compra viven
en la MariaDB compartida con el POS (ver agent/tools.py), no aquí, para que el
panel de Laravel pueda administrarlos sin necesitar acceso a este servidor.
"""

import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Ventana de conversación: si el cliente deja de escribir más de estas horas, lo que
# siga se trata como una conversación nueva (el historial anterior ya no se le pasa al
# modelo). Evita que retome pedidos de semanas atrás o invente su estado.
HISTORIAL_VENTANA_HORAS = float(os.getenv("HISTORIAL_VENTANA_HORAS", "6"))

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WebhookProcesado(Base):
    """Registro de IDs de mensajes ya procesados para evitar duplicados."""
    __tablename__ = "webhook_procesado"

    mensaje_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    procesado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los mensajes de la conversación ACTUAL (máximo `limite`).

    La conversación actual es el tramo de mensajes sin huecos mayores a
    HISTORIAL_VENTANA_HORAS, contando hacia atrás desde ahora: si el último mensaje
    guardado es de hace más de esa ventana, el historial viene vacío (conversación
    nueva); si en medio hay un hueco mayor, se corta ahí.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content, en orden cronológico
    """
    ventana = timedelta(hours=HISTORIAL_VENTANA_HORAS)
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc(), Mensaje.id.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

    # Recorrer del más reciente al más viejo y cortar en el primer hueco > ventana
    vigentes = []
    referencia = datetime.utcnow()  # timestamps se guardan en UTC
    for msg in mensajes:
        if referencia - msg.timestamp > ventana:
            break
        vigentes.append(msg)
        referencia = msg.timestamp

    vigentes.reverse()
    return [
        {"role": msg.role, "content": msg.content}
        for msg in vigentes
    ]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()


async def marcar_procesado(mensaje_id: str) -> bool:
    """
    Intenta registrar un mensaje_id como procesado.
    Retorna True si era nuevo (debe procesarse) o False si ya existia (duplicado).
    Si mensaje_id viene vacio, procesa siempre (no se puede deduplicar).
    """
    if not mensaje_id:
        return True
    async with async_session() as session:
        try:
            session.add(WebhookProcesado(mensaje_id=mensaje_id))
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False
