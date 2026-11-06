"""Systémový stav: vypínač agentů, útrata za měsíc, jednorázové příznaky."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db.enums import RunStatus
from app.db.models import AgentRun, SystemState


async def get_state(s: AsyncSession, key: str, default: Any = None) -> Any:
    value = await s.scalar(select(SystemState.value).where(SystemState.key == key))
    return default if value is None else value


async def set_state(s: AsyncSession, key: str, value: Any, by: str) -> None:
    row = await s.get(SystemState, key)
    if row is None:
        s.add(SystemState(key=key, value=value, updated_by=by))
    else:
        row.value = value
        row.updated_by = by


async def agents_enabled(s: AsyncSession) -> bool:
    return await get_state(s, "agents_enabled", True) is not False  # chybí = povoleno


async def set_agents_enabled(s: AsyncSession, enabled: bool, by: str) -> None:
    await set_state(s, "agents_enabled", enabled, by)
    audit(s, actor=by, action="system.agents_enabled" if enabled else "system.agents_stopped")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_this_month(s: AsyncSession) -> Decimal:
    total = await s.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
            AgentRun.started_at >= month_start()
        )
    )
    return Decimal(total or 0)


async def run_stats_24h(s: AsyncSession) -> dict[str, int]:
    since = datetime.now(UTC) - timedelta(hours=24)
    rows = (
        await s.execute(
            select(AgentRun.status, func.count())
            .where(AgentRun.started_at >= since)
            .group_by(AgentRun.status)
        )
    ).all()
    return {str(status.value if isinstance(status, RunStatus) else status): n for status, n in rows}


async def claim_once(s: AsyncSession, key: str, by: str) -> bool:
    """Vrátí True jen poprvé pro daný klíč (např. 'budget_warned:2026-11'). Ochrana proti
    opakovanému upozornění."""
    if await s.get(SystemState, key) is not None:
        return False
    s.add(SystemState(key=key, value=True, updated_by=by))
    await s.flush()
    return True
