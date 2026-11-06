"""Testy zpracování zpráv (bez Discordu a bez skutečného volání Claude – model je simulovaný)."""

from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.chat import ChatService, InboundMessage
from app.config import Settings
from app.db.enums import MessageRole, RunStatus
from app.db.models import AgentRun, AuditLog, Conversation, Message, SystemState
from app.llm.base import LLMError
from app.seed import seed
from tests.fakes import FakeLLM


def settings(**kw: object) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)  # type: ignore[arg-type]


_counter = 0


def inbound(
    text: str = "Ahoj",
    channel_id: str = "100",
    channel_name: str = "chief-of-staff",
    msg_id: str | None = None,
) -> InboundMessage:
    global _counter
    _counter += 1
    return InboundMessage("discord", channel_id, channel_name, msg_id or f"m{_counter}", "42", text)


async def test_reply_is_stored_with_run_tokens_and_cost(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM()
    reply = await ChatService(sessions, llm, settings()).handle(inbound("Jak se máš?"))

    assert reply is not None and reply.processed
    assert reply.text == "Dobrý den, jsem připraven."

    msgs = (
        await session.execute(select(Message.role, Message.content).order_by(Message.seq))
    ).all()
    assert [(m.role, m.content) for m in msgs] == [
        (MessageRole.USER, "Jak se máš?"),
        (MessageRole.ASSISTANT, "Dobrý den, jsem připraven."),
    ]
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.SUCCESS
    assert (run.input_tokens, run.output_tokens) == (1000, 200)
    assert run.cost_usd == Decimal("0.0060")  # 1000×3/1M + 200×15/1M
    assert run.finished_at is not None
    actions = set((await session.scalars(select(AuditLog.action))).all())
    assert {"message.received", "agent_run.success"} <= actions


async def test_conversation_context_is_sent_to_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLLM()
    svc = ChatService(sessions, llm, settings())
    await svc.handle(inbound("Jmenuji se Radek."))
    await svc.handle(inbound("Jak se jmenuji?"))

    sent = llm.calls[-1]["messages"]
    assert [m.role for m in sent] == ["user", "assistant", "user"]  # type: ignore[attr-defined]
    assert sent[0].content == "Jmenuji se Radek."  # type: ignore[index]


async def test_channel_is_linked_to_project_pomnenice(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    project = await seed(session)
    await session.commit()
    llm = FakeLLM()
    await ChatService(sessions, llm, settings()).handle(
        inbound("Co víme o vodě?", channel_id="200", channel_name="pomnenice")
    )
    conv = await session.scalar(select(Conversation).where(Conversation.external_id == "200"))
    assert conv is not None and conv.project_id == project.id
    assert "Osada Pomněnice" in str(llm.calls[0]["system"])


async def test_duplicate_message_processed_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    llm = FakeLLM()
    svc = ChatService(sessions, llm, settings())
    first = await svc.handle(inbound(msg_id="dup-1"))
    second = await svc.handle(inbound(msg_id="dup-1"))
    assert first is not None and second is None
    assert len(llm.calls) == 1


async def test_kill_switch_blocks_ai(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await session.execute(
        update(SystemState).where(SystemState.key == "agents_enabled").values(value=False)
    )
    await session.commit()
    llm = FakeLLM()
    reply = await ChatService(sessions, llm, settings()).handle(inbound())
    assert reply is not None and not reply.processed and "zastavení" in reply.text
    assert llm.calls == []
    # zpráva se přesto uložila
    assert await session.scalar(select(Message.content)) == "Ahoj"


async def test_budget_exhausted_blocks_ai(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    session.add(AgentRun(trigger="manual", cost_usd=Decimal("50.00"), status="success"))
    await session.commit()
    llm = FakeLLM()
    reply = await ChatService(sessions, llm, settings()).handle(inbound())
    assert reply is not None and not reply.processed and "rozpočet" in reply.text
    assert llm.calls == []


async def test_llm_error_is_recorded(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM(fail=LLMError("RateLimitError: příliš mnoho požadavků"))
    reply = await ChatService(sessions, llm, settings()).handle(inbound())
    assert reply is not None and "chyba" in reply.text
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.ERROR
    assert run.error is not None and "RateLimitError" in run.error


async def test_timeout_stops_run(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    llm = FakeLLM(delay=5)
    reply = await ChatService(sessions, llm, settings(max_run_seconds=1)).handle(inbound())
    assert reply is not None and "přerušena" in reply.text
    run = await session.get_one(AgentRun, reply.run_id)
    assert run.status == RunStatus.LIMIT_EXCEEDED


@pytest.mark.parametrize("model", ["claude-unknown-9"])
def test_unknown_model_priced_at_highest_rate(model: str) -> None:
    from app.llm.base import estimate_cost

    s = settings()
    assert estimate_cost(s.llm_prices_per_mtok, model, 1_000_000, 0) == Decimal("15")
