"""Vstupní bod aplikace: FastAPI (kontrola stavu) + Discord bot v jednom procesu."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, text

from app.agent.chat import ChatService
from app.config import get_settings
from app.db.models import SystemState
from app.db.session import get_sessionmaker, session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ai-hq")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    bot_task: asyncio.Task[None] | None = None
    bot = None

    if settings.discord_bot_token and settings.anthropic_api_key:
        from app.discord_bot.bot import AIHQBot
        from app.llm.anthropic_client import AnthropicClient

        llm = AnthropicClient(settings.anthropic_api_key.get_secret_value())
        bot = AIHQBot(ChatService(get_sessionmaker(), llm, settings), settings)
        bot_task = asyncio.create_task(bot.start(settings.discord_bot_token.get_secret_value()))
        log.info("Discord bot se spouští")
    else:
        log.warning("Discord bot nespuštěn – chybí DISCORD_BOT_TOKEN nebo ANTHROPIC_API_KEY")

    yield

    if bot is not None:
        await bot.close()
    if bot_task is not None:
        bot_task.cancel()


app = FastAPI(title="AI HQ", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Ověří, že aplikace běží a databáze odpovídá."""
    async with session_scope() as s:
        await s.execute(text("SELECT 1"))
        enabled = await s.scalar(
            select(SystemState.value).where(SystemState.key == "agents_enabled")
        )
    return {"status": "ok", "database": "ok", "agents_enabled": bool(enabled)}
