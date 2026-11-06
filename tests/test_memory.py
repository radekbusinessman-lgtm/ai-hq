"""Testy projektové paměti a smyčky nástrojů."""

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.chat import ChatService, InboundMessage
from app.config import Settings
from app.db.enums import FactStatus, PermissionLevel, RunStatus
from app.db.models import AgentRun, AuditLog, Fact, Message
from app.llm.base import ToolResultBlock
from app.seed import seed
from app.tools.memory import MEMORY_TOOLS, search_terms
from app.tools.registry import Tool, ToolContext, ToolRegistry
from app.tools.tasks import TASK_TOOLS
from tests.fakes import FakeLLM, text, tool

_n = 0


def msg(t: str, channel: str = "pomnenice") -> InboundMessage:
    global _n
    _n += 1
    return InboundMessage("discord", f"ch-{channel}", channel, f"mm{_n}", "42", t)


def cfg(**kw: Any) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)


def last_tool_result(llm: FakeLLM) -> dict[str, Any]:
    """Výsledek nástroje, který agent poslal zpět modelu v posledním volání."""
    blocks = llm.calls[-1]["messages"][-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    return json.loads(blocks[0].content)  # type: ignore[no-any-return]


async def test_save_fact_then_search_and_cite(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    project = await seed(session)
    await session.commit()

    llm = FakeLLM(
        script=[
            tool(
                "save_fact",
                statement="Kapacita ČOV Pomněnice je 300 EO.",
                status="verified",
                topic="kanalizace",
                source_note="sdělil uživatel",
            ),
            text("Uloženo."),
        ]
    )
    svc = ChatService(sessions, llm, cfg())
    reply = await svc.handle(msg("Zapamatuj si: kapacita ČOV je 300 EO, potvrzeno."))
    assert reply is not None and reply.text == "Uloženo."

    saved = last_tool_result(llm)
    assert saved["ulozeno"] is True and saved["ref"].startswith("F-")

    fact = await session.scalar(select(Fact).where(Fact.topic == "kanalizace"))
    assert fact is not None
    assert fact.status == FactStatus.VERIFIED
    assert fact.project_id == project.id and fact.company_id == project.company_id
    # zdroj: odkaz na konkrétní zprávu uživatele + popis
    user_msg = await session.scalar(select(Message).where(Message.id == fact.source_message_id))
    assert user_msg is not None and "300 EO" in user_msg.content
    assert "sdělil uživatel" in fact.source

    # běh má záznam o použitém nástroji, audit zapsán
    run = await session.get_one(AgentRun, reply.run_id)
    assert [c["tool"] for c in run.tool_calls] == ["save_fact"]
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "fact.created"))

    # vyhledání – i s jiným tvarem slova („kanalizací“) a bez diakritiky v názvu projektu
    llm2 = FakeLLM(
        script=[
            tool("search_facts", query="kanalizací", project="Pomnenice"),
            text("Kapacita je 300 EO [F-…]"),
        ]
    )
    await ChatService(sessions, llm2, cfg()).handle(msg("Jaká je kapacita ČOV?", "chief"))
    found = last_tool_result(llm2)
    assert found["pocet"] == 1
    assert found["fakta"][0]["stav"] == "verified"
    assert found["fakta"][0]["ref"] == saved["ref"]


async def test_seeded_context_is_returned_as_assumptions(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    llm = FakeLLM(script=[tool("search_facts", query="domů"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("Kolik domů plánujeme?"))
    found = last_tool_result(llm)
    assert found["pocet"] == 1
    assert found["fakta"][0]["stav"] == "assumption"


async def test_empty_memory_is_reported_explicitly(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    llm = FakeLLM(script=[tool("search_facts", query="trafostanice"), text("Nevím.")])
    await ChatService(sessions, llm, cfg()).handle(msg("Kde bude trafostanice?"))
    found = last_tool_result(llm)
    assert found["pocet"] == 0 and "nic neví" in found["poznamka"]


async def test_verified_fact_requires_source(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    llm = FakeLLM(script=[tool("save_fact", statement="X", status="verified"), text("ok")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    assert "zdroj" in last_tool_result(llm)["error"]
    assert await session.scalar(select(Fact).where(Fact.statement == "X")) is None


async def test_model_cannot_save_outdated_or_invented_status(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    for status in ("outdated", "certain"):
        llm = FakeLLM(script=[tool("save_fact", statement="Y", status=status), text("ok")])
        await ChatService(sessions, llm, cfg()).handle(msg("…"))
        assert "Neplatný stav" in last_tool_result(llm)["error"]


async def test_unknown_project_and_missing_project(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    llm = FakeLLM(script=[tool("search_facts", query="x", project="Neexistující"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    assert "neznám" in last_tool_result(llm)["error"]

    llm = FakeLLM(script=[tool("search_facts", query="x"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("…", channel="chief-of-staff"))
    assert "není přiřazena" in last_tool_result(llm)["error"]


async def test_unregistered_tool_is_refused(sessions: async_sessionmaker[AsyncSession]) -> None:
    llm = FakeLLM(script=[tool("run_shell", cmd="rm -rf /"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("…"))
    assert "neexistuje" in last_tool_result(llm)["error"]


async def test_approval_level_tool_is_not_executed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    executed: list[bool] = []

    async def send_email(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        executed.append(True)
        return {}

    registry = ToolRegistry(
        [
            Tool(
                "send_email",
                "Odešle e-mail",
                {"type": "object"},
                PermissionLevel.APPROVAL,
                send_email,
            )
        ]
    )
    llm = FakeLLM(script=[tool("send_email", to="x@y.cz"), text("…")])
    await ChatService(sessions, llm, cfg(), registry).handle(msg("…"))
    assert executed == []
    assert "schválení" in last_tool_result(llm)["error"]


async def test_step_limit_stops_endless_tool_use(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM(script=[tool("list_projects")])  # model by volal nástroje donekonečna
    reply = await ChatService(sessions, llm, cfg(max_tool_steps_per_run=3)).handle(msg("…"))
    assert reply is not None and "limit" in reply.text
    assert len(llm.calls) == 4  # 1 + 3 kroky
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.LIMIT_EXCEEDED and len(run.tool_calls) == 3


async def test_cost_limit_stops_run(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM(script=[tool("list_projects")], tokens=(200_000, 0))  # 0,60 USD za volání
    reply = await ChatService(sessions, llm, cfg(max_run_cost_usd=Decimal("1"))).handle(msg("…"))
    assert reply is not None and "nákladový limit" in reply.text
    assert len(llm.calls) == 2
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.LIMIT_EXCEEDED and run.cost_usd == Decimal("1.2")


async def test_tools_are_offered_to_model(sessions: async_sessionmaker[AsyncSession]) -> None:
    llm = FakeLLM()
    await ChatService(sessions, llm, cfg()).handle(msg("ahoj"))
    names = {t.name for t in llm.calls[0]["tools"]}
    assert names == {t.name for t in MEMORY_TOOLS + TASK_TOOLS}


def test_search_terms_use_word_stems() -> None:
    assert search_terms("Kapacita vodovodu pro 80 domů") == ["kapac", "vodov", "pro", "80", "domů"]
    assert search_terms("a_b %") == []


async def test_list_projects_shows_company(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    llm = FakeLLM(script=[tool("list_projects"), text("…")])
    await ChatService(sessions, llm, cfg()).handle(msg("…", channel="chief"))
    projects = last_tool_result(llm)["projekty"]
    assert projects == [
        {
            "nazev": "Osada Pomněnice",
            "slug": "pomnenice",
            "firma": "Benet, s.r.o.",
            "stav": "active",
            "aliasy": ["Pomněnice", "Pomnenice", "Osada Pomnenice"],
        }
    ]
