"""DEN 3: oddělení firem, historie úkolů, /stop s přerušením běžících úloh, náklady,
a akceptační scénáře ze zadání (se simulovaným modelem – ostré ověření je v Discordu)."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.chat import ChatService, InboundMessage
from app.agent.state import cost_summary, set_agents_enabled
from app.config import Settings
from app.db.enums import FactStatus, RunStatus
from app.db.models import AgentRun, Company, Fact, Project, Task
from app.llm.base import ToolResultBlock
from app.scheduler.jobs import daily_briefing_job
from app.seed import seed
from app.tools.memory import fact_ref
from tests.fakes import FakeLLM, text, tool

_n = 0


def msg(t: str = "…", channel: str = "pomnenice") -> InboundMessage:
    global _n
    _n += 1
    return InboundMessage("discord", f"ch-{channel}", channel, f"d3-{_n}", "42", t)


def cfg(**kw: Any) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)


def result(llm: FakeLLM, call: int = -1) -> dict[str, Any]:
    blocks = llm.calls[call]["messages"][-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    return json.loads(blocks[0].content)  # type: ignore[no-any-return]


async def two_companies(session: AsyncSession) -> tuple[Project, Project]:
    pomnenice = await seed(session)  # Benet, s.r.o.
    other = Company(name="Búda s.r.o.", slug="buda")
    session.add(other)
    await session.flush()
    buda = Project(company_id=other.id, name="Pekárna", slug="pekarna", aliases=["Búda"])
    session.add(buda)
    await session.flush()
    session.add_all(
        [
            Fact(
                company_id=other.id,
                project_id=buda.id,
                statement="Tržby pekárny 2025: tajné.",
                status=FactStatus.VERIFIED,
                topic="finance",
            ),
            Task(company_id=other.id, project_id=buda.id, title="Úkol pekárny"),
            Task(company_id=pomnenice.company_id, project_id=pomnenice.id, title="Úkol Pomněnic"),
        ]
    )
    await session.commit()
    return pomnenice, buda


# --------------------------------------------------------- oddělení firem


async def test_project_channel_cannot_reach_other_company(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await two_companies(session)
    svc = ChatService

    llm = FakeLLM(script=[tool("search_facts", query="tržby", project="Pekárna"), text("…")])
    await svc(sessions, llm, cfg()).handle(msg())
    assert "jiné firmě" in result(llm)["error"]

    llm = FakeLLM(script=[tool("list_projects"), text("…")])
    await svc(sessions, llm, cfg()).handle(msg())
    names = [p["nazev"] for p in result(llm)["projekty"]]
    assert names == ["Osada Pomněnice"]

    llm = FakeLLM(script=[tool("list_tasks", all_projects=True), text("…")])
    await svc(sessions, llm, cfg()).handle(msg())
    assert [t["nazev"] for t in result(llm)["ukoly"]] == ["Úkol Pomněnic"]

    other_task = await session.scalar(select(Task).where(Task.title == "Úkol pekárny"))
    assert other_task is not None
    ref = "T-" + str(other_task.id).replace("-", "")[:8]
    llm = FakeLLM(script=[tool("update_task", ref=ref, status="done"), text("…")])
    await svc(sessions, llm, cfg()).handle(msg())
    assert "neexistuje" in result(llm)["error"]
    await session.refresh(other_task)
    assert other_task.status.value == "open"

    other_fact = await session.scalar(select(Fact).where(Fact.topic == "finance"))
    assert other_fact is not None
    llm = FakeLLM(
        script=[
            tool(
                "supersede_fact", ref=fact_ref(other_fact.id), new_status="assumption", reason="x"
            ),
            text("…"),
        ]
    )
    await svc(sessions, llm, cfg()).handle(msg())
    assert "neexistuje" in result(llm)["error"]


async def test_general_channel_sees_all_companies(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await two_companies(session)
    llm = FakeLLM(script=[tool("list_projects"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg(channel="chief-of-staff"))
    out = result(llm)
    assert out["rozsah"] == "všechny firmy"
    assert sorted(p["nazev"] for p in out["projekty"]) == ["Osada Pomněnice", "Pekárna"]


# --------------------------------------------------------- historie úkolů


async def test_task_history_records_old_and_new_values(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    svc = ChatService
    llm = FakeLLM(
        script=[tool("create_task", title="Prověřit ČEZ", due_date="2026-11-20"), text("ok")]
    )
    await svc(sessions, llm, cfg()).handle(msg())
    ref = result(llm)["ref"]
    for change in ({"priority": 1}, {"due_date": "2026-11-25"}, {"status": "done"}):
        llm = FakeLLM(script=[tool("update_task", ref=ref, **change), text("ok")])
        await svc(sessions, llm, cfg()).handle(msg())

    llm = FakeLLM(script=[tool("task_history", ref=ref), text("…")])
    await svc(sessions, llm, cfg()).handle(msg())
    hist = result(llm)["historie"]
    assert [h["akce"] for h in hist] == ["založen", "změněn", "změněn", "změněn"]
    assert hist[0]["hodnoty"]["termin"] == "2026-11-20"
    assert hist[0]["kdo"] == "user:discord:42"
    assert hist[1]["zmeny"] == {"priorita": ["střední", "vysoká"]}
    assert hist[2]["zmeny"] == {"termin": ["2026-11-20", "2026-11-25"]}
    assert hist[3]["zmeny"] == {"stav": ["open", "done"]}


# ----------------------------------------------------------------- /stop


async def test_stop_cancels_running_run_and_blocks_new_ones(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM(delay=5)  # „dlouhá“ úloha
    svc = ChatService(sessions, llm, cfg())
    running = asyncio.create_task(svc.handle(msg("dlouhá analýza", channel="chief")))
    for _ in range(100):
        if svc.running_count:
            break
        await asyncio.sleep(0.01)
    assert svc.running_count == 1

    # /stop: nejdřív vypínač, pak přerušení běžících
    await set_agents_enabled(session, False, "user:discord:111")
    await session.commit()
    assert await svc.cancel_running() == 1
    reply = await running
    assert reply is not None and "/stop" in reply.text
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.CANCELLED and run.finished_at is not None
    assert svc.running_count == 0

    blocked = await svc.handle(msg("další", channel="chief"))
    assert blocked is not None and not blocked.processed
    assert len(llm.calls) == 1  # nová zpráva se k modelu nedostala

    class Bot:
        connected = True
        briefings = 0

        async def run_briefing(self, *, manual: bool = False) -> str:
            self.briefings += 1
            return "ok"

        async def post_event(self, text: str) -> None: ...

    bot = Bot()
    await daily_briefing_job(bot, sessions)
    assert bot.briefings == 0  # plánované úlohy stojí

    await set_agents_enabled(session, True, "user:discord:111")  # /start
    await session.commit()
    resumed = await ChatService(sessions, FakeLLM(), cfg()).handle(msg("a teď?", channel="chief"))
    assert resumed is not None and resumed.processed


async def test_cancel_with_nothing_running() -> None:
    from tests.conftest import TEST_DATABASE_URL  # noqa: F401

    svc = ChatService(None, FakeLLM(), cfg())  # type: ignore[arg-type]
    assert await svc.cancel_running() == 0


# --------------------------------------------------------------- náklady


async def test_cost_summary_today_and_by_model(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            AgentRun(
                trigger="manual",
                model="claude-sonnet-5-5",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=Decimal("0.03"),
                status="success",
            ),
            AgentRun(
                trigger="schedule",
                model="claude-opus-5-5",
                input_tokens=5000,
                output_tokens=800,
                cost_usd=Decimal("0.135"),
                status="success",
            ),
            AgentRun(
                trigger="manual",
                model="claude-sonnet-5-5",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("1"),
                status="success",
                started_at=now - timedelta(days=40),
            ),  # minulý měsíc – nepočítá se
        ]
    )
    await session.flush()
    c = await cost_summary(session)
    assert c["by_model"]["claude-sonnet-5-5"]["runs"] == 1
    assert c["by_model"]["claude-opus-5-5"]["in"] == 5000
    assert c["month_usd"] == Decimal("0.165")
    assert c["today_usd"] >= Decimal("0.165") - Decimal("0.0001")


# ---------------------------------------------- akceptační scénáře (simulace)


async def test_acceptance_scenarios_simulated(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    svc = ChatService

    # 1) „Vytvoř úkol pro Pomněnice: prověřit kapacitu elektrické infrastruktury do 20. 11.“
    llm = FakeLLM(
        script=[
            tool(
                "create_task",
                title="Prověřit kapacitu elektrické infrastruktury",
                due_date="2026-11-20",
                project="Pomněnice",
            ),
            text("Úkol vytvořen."),
        ]
    )
    await svc(sessions, llm, cfg()).handle(msg("Vytvoř úkol…", channel="chief-of-staff"))
    created = result(llm)
    assert created["termin"] == "2026-11-20" and created["projekt"] == "Osada Pomněnice"

    # 2) „Jaké máme otevřené úkoly v Pomněnicích?“ – odpověď z databáze
    llm = FakeLLM(script=[tool("list_tasks", project="Pomněnice"), text("…")])
    await svc(sessions, llm, cfg()).handle(msg("Jaké máme…", channel="chief-of-staff"))
    listed = result(llm)
    assert [t["ref"] for t in listed["ukoly"]] == [created["ref"]]

    # 3) „Připrav mi dnešní briefing Pomněnic.“ – agent má k dispozici úkoly i fakta
    llm = FakeLLM(script=[tool("list_tasks", project="Pomněnice"), text("Briefing…")])
    reply = await svc(sessions, llm, cfg()).handle(msg("Připrav…", channel="chief-of-staff"))
    assert reply is not None and reply.processed
    system = str(llm.calls[0]["system"])
    assert "Briefing na požádání" in system and "nevymýšlej" in system
    assert date.fromisoformat(created["termin"]) == date(2026, 11, 20)
