# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

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


class NumeroBlockeado(Base):
    """Números vetados por mal uso del servicio."""
    __tablename__ = "numeros_bloqueados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    motivo: Mapped[str] = mapped_column(Text)
    bloqueado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [
            {"role": msg.role, "content": msg.content}
            for msg in mensajes
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


async def es_numero_bloqueado(telefono: str) -> bool:
    """Retorna True si el número está en la lista de bloqueados."""
    async with async_session() as session:
        query = select(NumeroBlockeado).where(NumeroBlockeado.telefono == telefono)
        result = await session.execute(query)
        return result.scalar_one_or_none() is not None


async def bloquear_numero(telefono: str, motivo: str):
    """Agrega un número a la lista de bloqueados."""
    async with async_session() as session:
        bloqueado = NumeroBlockeado(
            telefono=telefono,
            motivo=motivo,
            bloqueado_en=datetime.utcnow()
        )
        session.add(bloqueado)
        await session.commit()


async def desbloquear_numero(telefono: str):
    """Elimina un número de la lista de bloqueados."""
    async with async_session() as session:
        query = select(NumeroBlockeado).where(NumeroBlockeado.telefono == telefono)
        result = await session.execute(query)
        registro = result.scalar_one_or_none()
        if registro:
            await session.delete(registro)
            await session.commit()
            return True
        return False


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


async def listar_bloqueados() -> list[dict]:
    """Retorna la lista completa de números bloqueados."""
    async with async_session() as session:
        result = await session.execute(select(NumeroBlockeado).order_by(NumeroBlockeado.bloqueado_en.desc()))
        registros = result.scalars().all()
        return [
            {"telefono": r.telefono, "motivo": r.motivo, "bloqueado_en": str(r.bloqueado_en)}
            for r in registros
        ]
