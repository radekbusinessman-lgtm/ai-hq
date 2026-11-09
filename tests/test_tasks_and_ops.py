"""Testy úkolů, briefingu, plánovače, upozornění a hlídače."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.briefing import briefing_message
from app.agent.chat import ChatService, InboundMessage
from app.agent.state import set_agents_enabled
from app.config import Settings
from app.db.enums import RunStatus, RunTrigger, TaskStatus
from app.db.models import AgentRun, AuditLog, SystemState, Task
from app.llm.base import LLMError, ToolResultBlock
from app.scheduler.jobs import build_scheduler, daily_briefing_job, daily_checks_job
from app.seed import seed
from tests.fakes import FakeLLM, text, tool

_n = 0


def msg(t: str, channel: str = "pomnenice") -> InboundMessage:
    global _n
    _n += 1
    return InboundMessage("discord", f"ch-{channel}", channel, f"tt{_n}", "42", t)


def cfg(**kw: Any) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)


def tool_result(llm: FakeLLM, call: int = -1) -> dict[str, Any]:
    blocks = llm.calls[call]["messages"][-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    return json.loads(blocks[0].content)  # type: ignore[no-any-return]


@pytest.fixture
async def project(session: AsyncSession) -> Any:
    p = await seed(session)
    await session.commit()
    return p


# ------------------------------------------------------------------- úkoly


async def test_create_list_and_complete_task(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession, project: Any
) -> None:
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    llm = FakeLLM(
        script=[
            tool("create_task", title="Objednat geologický průzkum", due_date=tomorrow, priority=1),
            text("Úkol vytvořen."),
        ]
    )
    await ChatService(sessions, llm, cfg()).handle(msg("Přidej úkol: geologie, zítra, priorita 1"))
    created = tool_result(llm)
    assert created["vytvoreno"] and created["ref"].startswith("T-")
    assert created["projekt"] == "Osada Pomněnice" and created["priorita"] == "vysoká"

    task = await session.scalar(select(Task))
    assert task is not None and task.company_id == project.company_id
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "task.created"))

    llm2 = FakeLLM(script=[tool("list_tasks"), text("…")])
    await ChatService(sessions, llm2, cfg()).handle(msg("Jaké mám úkoly?"))
    listed = tool_result(llm2)
    assert listed["pocet"] == 1 and listed["ukoly"][0]["ref"] == created["ref"]

    llm3 = FakeLLM(script=[tool("update_task", ref=created["ref"], status="done"), text("ok")])
    await ChatService(sessions, llm3, cfg()).handle(msg("Hotovo"))
    assert tool_result(llm3)["stav"] == "done"
    await session.refresh(task)
    assert task.status == TaskStatus.DONE and task.completed_at is not None

    llm4 = FakeLLM(script=[tool("list_tasks"), text("…")])
    await ChatService(sessions, llm4, cfg()).handle(msg("A teď?"))
    assert tool_result(llm4)["pocet"] == 0  # hotové se ve výchozím výpisu nezobrazují


async def test_overdue_flag_and_validation(
    sessions: async_sessionmaker[AsyncSession], project: Any
) -> None:
    llm = FakeLLM(script=[tool("create_task", title="Starý", due_date="2020-01-01"), text("ok")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    assert tool_result(llm)["po_terminu"] is True

    for bad in ({"title": ""}, {"title": "X", "due_date": "zítra"}, {"title": "X", "priority": 9}):
        llm = FakeLLM(script=[tool("create_task", **bad), text("ok")])
        await ChatService(sessions, llm, cfg()).handle(msg("…"))
        assert "error" in tool_result(llm)

    llm = FakeLLM(script=[tool("update_task", ref="T-zzzz", status="done"), text("ok")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    assert "Neplatná reference" in tool_result(llm)["error"]


async def test_list_tasks_all_projects_from_general_channel(
    sessions: async_sessionmaker[AsyncSession], project: Any
) -> None:
    llm = FakeLLM(script=[tool("create_task", title="A"), text("ok")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    llm = FakeLLM(script=[tool("list_tasks"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("…", channel="chief-of-staff"))
    assert tool_result(llm)["projekt"] == "všechny projekty"
    assert tool_result(llm)["pocet"] == 1


# --------------------------------------------------------------- briefing


def briefing(day: date, manual: str | None = None) -> InboundMessage:
    return briefing_message(
        day=day,
        channel_id="ch-chief",
        channel_name="chief-of-staff",
        model="claude-opus-5-5",
        manual_key=manual,
    )


async def test_scheduled_briefing_runs_once_per_day(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession, project: Any
) -> None:
    llm = FakeLLM(reply="Briefing: nic po termínu.")
    svc = ChatService(sessions, llm, cfg())
    day = date(2026, 11, 7)
    first = await svc.handle(briefing(day))
    second = await svc.handle(briefing(day))  # např. po restartu tentýž den
    assert first is not None and first.processed and second is None
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "claude-opus-5-5"
    run = await session.get_one(AgentRun, first.run_id)
    assert run.trigger == RunTrigger.SCHEDULE and run.source == "system:scheduler"

    manual = await svc.handle(briefing(day, manual="briefing-manual:x1"))
    assert manual is not None and manual.processed  # ruční briefing jde kdykoli


class FakeBot:
    def __init__(self, connected: bool = True):
        self.connected = connected
        self.briefings = 0
        self.events: list[str] = []

    async def run_briefing(self, *, manual: bool = False) -> str:
        self.briefings += 1
        return "ok"

    async def post_event(self, text: str) -> None:
        self.events.append(text)


async def test_briefing_job_respects_kill_switch_and_connection(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    bot = FakeBot()
    await daily_briefing_job(bot, sessions)
    assert bot.briefings == 1

    offline = FakeBot(connected=False)
    await daily_briefing_job(offline, sessions)
    assert offline.briefings == 0

    await set_agents_enabled(session, False, "test")
    await session.commit()
    await daily_briefing_job(bot, sessions)
    assert bot.briefings == 1  # vypínač zastaví i plánované úlohy
    assert await session.scalar(
        select(AuditLog.id).where(AuditLog.action == "system.agents_stopped")
    )


def test_scheduler_jobs_configured() -> None:
    sched = build_scheduler(FakeBot(), None, cfg(briefing_time="07:30"))  # type: ignore[arg-type]
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"daily_briefing", "daily_checks"}
    assert "hour='7'" in str(jobs["daily_briefing"].trigger)
    assert "minute='30'" in str(jobs["daily_briefing"].trigger)
    assert sched._job_defaults["max_instances"] == 1 and sched._job_defaults["coalesce"]

    off = build_scheduler(FakeBot(), None, cfg(briefing_time=""))  # type: ignore[arg-type]
    assert {j.id for j in off.get_jobs()} == {"daily_checks"}


# ------------------------------------------------------------- upozornění


async def test_key_expiry_warning_once_per_day(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    today = date(2026, 11, 7)
    bot = FakeBot()
    far = cfg(anthropic_key_expires_on=today + timedelta(days=60))
    await daily_checks_job(bot, sessions, far, today)
    assert bot.events == []

    near = cfg(anthropic_key_expires_on=today + timedelta(days=10))
    await daily_checks_job(bot, sessions, near, today)
    await daily_checks_job(bot, sessions, near, today)
    assert len(bot.events) == 1 and "za 10 dní" in bot.events[0]


async def test_budget_warning_sent_once_per_month(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    session.add(AgentRun(trigger="manual", cost_usd=Decimal("40.5"), status="success"))
    await session.commit()
    events: list[str] = []

    async def notify(t: str) -> None:
        events.append(t)

    svc = ChatService(sessions, FakeLLM(), cfg(), notify=notify)
    await svc.handle(msg("a", channel="chief"))
    await svc.handle(msg("b", channel="chief"))
    assert len(events) == 1 and "80 %" in events[0]
    key = f"budget_warned:{datetime.now(UTC).strftime('%Y-%m')}"
    assert await session.get(SystemState, key) is not None


async def test_error_is_reported_to_events_channel(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []

    async def notify(t: str) -> None:
        events.append(t)

    svc = ChatService(sessions, FakeLLM(fail=LLMError("x")), cfg(), notify=notify)
    reply = await svc.handle(msg("a", channel="chief"))
    assert reply is not None and len(events) == 1 and "LLMError" in events[0]


async def test_notifier_failure_does_not_break_reply(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def broken(t: str) -> None:
        raise RuntimeError("Discord nedostupný")

    svc = ChatService(sessions, FakeLLM(fail=LLMError("x")), cfg(), notify=broken)
    reply = await svc.handle(msg("a", channel="chief"))
    assert reply is not None and "chyba" in reply.text


# ------------------------------------------------------------------ hlídač


async def test_watchdog_exits_when_bot_task_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    exits: list[str] = []
    monkeypatch.setattr(main, "WATCHDOG_INTERVAL_S", 0.01)
    monkeypatch.setattr(main, "_exit_for_restart", lambda reason: exits.append(reason))

    async def crash() -> None:
        raise RuntimeError("token neplatný")

    task = asyncio.create_task(crash())
    await asyncio.sleep(0.01)
    stopping = asyncio.Event()
    wd = asyncio.create_task(main.watchdog(FakeBot(), task, stopping))
    await asyncio.sleep(0.05)
    stopping.set()
    await wd
    assert exits and "token neplatný" in exits[0]


async def test_watchdog_exits_after_long_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    exits: list[str] = []
    monkeypatch.setattr(main, "WATCHDOG_INTERVAL_S", 0.01)
    monkeypatch.setattr(main, "BOT_OFFLINE_LIMIT_S", 0.03)
    monkeypatch.setattr(main, "_exit_for_restart", lambda reason: exits.append(reason))
    never = asyncio.create_task(asyncio.sleep(10))
    stopping = asyncio.Event()
    wd = asyncio.create_task(main.watchdog(FakeBot(connected=False), never, stopping))
    await asyncio.sleep(0.12)
    stopping.set()
    await wd
    never.cancel()
    assert exits and "nepřipojen" in exits[0]


async def test_run_status_unchanged_for_normal_flow(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    reply = await ChatService(sessions, FakeLLM(), cfg()).handle(msg("ahoj", channel="chief"))
    assert reply is not None
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.SUCCESS
    await session.execute(update(SystemState).where(SystemState.key == "x").values(value=1))


# ------------------------------------------------------------------ zálohy


async def test_backup_check_warns_on_old_or_missing_backup(tmp_path: Any) -> None:
    import os

    from app.scheduler.jobs import backup_check_job

    now = datetime(2026, 11, 9, 8, 5, tzinfo=UTC)
    settings = cfg(backups_dir=tmp_path)

    bot = FakeBot()
    await backup_check_job(bot, settings, now)
    assert len(bot.events) == 1 and "žádná záloha" in bot.events[0]

    fresh = tmp_path / "aihq-2026-11-09.dump"
    fresh.write_bytes(b"x")
    os.utime(fresh, (now.timestamp() - 5 * 3600,) * 2)
    bot = FakeBot()
    await backup_check_job(bot, settings, now)
    assert bot.events == []

    os.utime(fresh, (now.timestamp() - 30 * 3600,) * 2)
    bot = FakeBot()
    await backup_check_job(bot, settings, now)
    assert len(bot.events) == 1 and "30 h" in bot.events[0]

    bot = FakeBot()
    await backup_check_job(bot, cfg(backups_dir=tmp_path / "neexistuje"), now)
    assert bot.events == []
