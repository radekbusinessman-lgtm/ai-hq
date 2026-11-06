"""Plánované úlohy.

Každá úloha má: spouštěč (cron), kontrolu vypínače, ochranu proti duplicitnímu běhu
(klíč v databázi), časový a nákladový limit (přes ChatService) a záznam běhu (agent_runs).
APScheduler: max_instances=1 (nikdy dva běhy současně), coalesce (zmeškané běhy se slijí
do jednoho), misfire_grace_time (zmeškaný běh se dožene jen do 1 h).
"""

import logging
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.state import agents_enabled, claim_once
from app.config import Settings

log = logging.getLogger(__name__)


class BotLike(Protocol):
    @property
    def connected(self) -> bool: ...
    async def run_briefing(self, *, manual: bool = False) -> str: ...
    async def post_event(self, text: str) -> None: ...


async def daily_briefing_job(bot: BotLike, sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as s:
        if not await agents_enabled(s):
            log.info("Briefing přeskočen – agenti zastaveni")
            return
    if not bot.connected:
        log.warning("Briefing přeskočen – bot není připojen")
        return
    result = await bot.run_briefing()
    log.info("Denní briefing: %s", result)


async def daily_checks_job(
    bot: BotLike, sessions: async_sessionmaker[AsyncSession], settings: Settings, today: date
) -> None:
    """Upozornění na blížící se vypršení API klíče (jednou denně, jen v posledních N dnech)."""
    expiry = settings.anthropic_key_expires_on
    if expiry is None:
        return
    days_left = (expiry - today).days
    if days_left > settings.key_expiry_warning_days:
        return
    async with sessions() as s, s.begin():
        if not await claim_once(s, f"key_expiry_warned:{today.isoformat()}", "system:scheduler"):
            return
    if days_left < 0:
        text = f"🔑 API klíč Anthropic vypršel {expiry.isoformat()}! Vyměňte ho v .env."
    else:
        text = (
            f"🔑 API klíč Anthropic vyprší za {days_left} dní ({expiry.isoformat()}). "
            "Vytvořte nový v Anthropic Console a vložte ho do .env."
        )
    await bot.post_event(text)


def build_scheduler(
    bot: BotLike, sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(
        timezone=tz,
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600},
    )
    if settings.briefing_time:
        hour, minute = (int(x) for x in settings.briefing_time.split(":"))
        scheduler.add_job(
            daily_briefing_job,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[bot, sessions],
            id="daily_briefing",
            name="Denní briefing",
        )

    async def checks() -> None:
        await daily_checks_job(bot, sessions, settings, datetime.now(tz).date())

    scheduler.add_job(
        checks, CronTrigger(hour=8, minute=5, timezone=tz), id="daily_checks", name="Denní kontroly"
    )
    return scheduler
