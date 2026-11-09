"""Vstupní bod: FastAPI (kontrola stavu) + Discord bot + plánovač v jednom procesu.

Hlídač (watchdog): když bot skončí nebo není připojený déle než BOT_OFFLINE_LIMIT_S,
proces se ukončí s chybou a Docker (restart: unless-stopped) ho spustí znovu.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.agent.chat import ChatService
from app.agent.state import agents_enabled
from app.config import get_settings
from app.db.session import get_sessionmaker, session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ai-hq")

BOT_OFFLINE_LIMIT_S = 600  # 10 minut bez spojení s Discordem → restart
WATCHDOG_INTERVAL_S = 30


def _exit_for_restart(reason: str) -> None:
    log.critical("Ukončuji proces kvůli restartu: %s", reason)
    logging.shutdown()
    os._exit(1)


async def watchdog(bot: Any, bot_task: "asyncio.Task[None]", stopping: asyncio.Event) -> None:
    offline_since: float | None = None
    while not stopping.is_set():
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        if stopping.is_set():
            return
        if bot_task.done():
            exc = None if bot_task.cancelled() else bot_task.exception()
            _exit_for_restart(f"Discord bot skončil ({exc!r})")
        if bot.connected:
            offline_since = None
        else:
            offline_since = offline_since or time.monotonic()
            if time.monotonic() - offline_since > BOT_OFFLINE_LIMIT_S:
                _exit_for_restart(f"bot nepřipojen déle než {BOT_OFFLINE_LIMIT_S} s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.bot = None
    stopping = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    scheduler = None

    if settings.discord_bot_token and settings.anthropic_api_key:
        from app.discord_bot.bot import AIHQBot
        from app.llm.anthropic_client import AnthropicClient
        from app.scheduler.jobs import build_scheduler

        llm = AnthropicClient(settings.anthropic_api_key.get_secret_value())
        bot = AIHQBot(ChatService(get_sessionmaker(), llm, settings), settings)
        app.state.bot = bot
        bot_task = asyncio.create_task(bot.start(settings.discord_bot_token.get_secret_value()))
        tasks += [bot_task, asyncio.create_task(watchdog(bot, bot_task, stopping))]
        scheduler = build_scheduler(bot, get_sessionmaker(), settings)
        scheduler.start()
        log.info("Discord bot a plánovač se spouštějí (%s)", [j.name for j in scheduler.get_jobs()])
    else:
        log.warning("Discord bot nespuštěn – chybí DISCORD_BOT_TOKEN nebo ANTHROPIC_API_KEY")

    yield

    stopping.set()
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    if app.state.bot is not None:
        await app.state.bot.close()
    for t in tasks:
        t.cancel()


app = FastAPI(title="AI HQ", version="0.7.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    """Stav aplikace, databáze a Discordu. 503, pokud je bot zapnutý, ale nepřipojený."""
    async with session_scope() as s:
        await s.execute(text("SELECT 1"))
        enabled = await agents_enabled(s)
        schema = await s.scalar(text("SELECT version_num FROM alembic_version"))
        unprotected = await s.scalar(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
            )
        )
    bot = app.state.bot if hasattr(app.state, "bot") else None
    discord_state = "disabled" if bot is None else ("connected" if bot.connected else "offline")
    body: dict[str, Any] = {
        "status": "ok" if discord_state != "offline" else "degraded",
        "database": "ok",
        "discord": discord_state,
        "agents_enabled": enabled,
        "schema": schema,
        "tables_without_rls": unprotected,
    }
    return JSONResponse(body, status_code=503 if discord_state == "offline" else 200)
