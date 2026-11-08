"""Schvalování akcí: návrh → žádost → schválení/zamítnutí → provedení.

Hlavně ověřuje, že se akce NEPROVEDE bez schválení, po zamítnutí, po vypršení,
při změně obsahu, dvakrát, ani z automatického běhu.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent.approvals import ApprovalService
from app.agent.briefing import briefing_message
from app.agent.chat import ChatReply, ChatService, InboundMessage
from app.config import Settings
from app.db.enums import ApprovalStatus, FactStatus, PermissionLevel
from app.db.models import Approval, AuditLog, Fact
from app.discord_bot.bot import approval_card, approval_view, parse_approval_custom_id
from app.llm.base import ToolResultBlock
from app.seed import seed
from app.tools.memory import MEMORY_TOOLS, fact_ref
from app.tools.registry import Tool, ToolRegistry
from app.tools.tasks import TASK_TOOLS
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import FakeLLM, text, tool

_n = 0
OWNER = "user:discord:111"


def msg(t: str = "…", channel: str = "pomnenice") -> InboundMessage:
    global _n
    _n += 1
    return InboundMessage("discord", f"ch-{channel}", channel, f"ap{_n}", "42", t)


def cfg(**kw: Any) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)


def tool_result(llm: FakeLLM) -> dict[str, Any]:
    blocks = llm.calls[-1]["messages"][-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    return json.loads(blocks[0].content)  # type: ignore[no-any-return]


def registry() -> ToolRegistry:
    return ToolRegistry(MEMORY_TOOLS + TASK_TOOLS)


async def assumption_fact(session: AsyncSession, needle: str = "urbanistická studie") -> Fact:
    await seed(session)
    await session.commit()
    f = await session.scalar(select(Fact).where(Fact.statement.ilike(f"%{needle}%")))
    assert f is not None and f.status == FactStatus.ASSUMPTION
    return f


async def propose(
    sessions: async_sessionmaker[AsyncSession], fact: Fact, **overrides: Any
) -> tuple[ChatReply, FakeLLM]:
    args = {
        "ref": fact_ref(fact.id),
        "new_status": "verified",
        "source_note": "potvrdil uživatel",
        "reason": "uživatel potvrdil existenci studie",
    }
    args.update(overrides)
    llm = FakeLLM(script=[tool("supersede_fact", **args), text("Čeká na schválení.")])
    reply = await ChatService(sessions, llm, cfg()).handle(msg())
    assert reply is not None
    return reply, llm


# ------------------------------------------------------------------- návrh


async def test_proposal_creates_pending_approval_and_changes_nothing(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, llm = await propose(sessions, fact)

    out = tool_result(llm)
    assert out["ceka_na_schvaleni"] is True and out["ref"].startswith("A-")
    assert len(reply.approvals) == 1
    req = reply.approvals[0]
    assert "Nahradit fakt" in req.summary and "předpoklad" in req.summary
    assert "ověřeno" in req.summary and "potvrdil uživatel" in req.summary

    a = await session.get_one(Approval, req.id)
    assert a.status == ApprovalStatus.PENDING
    assert a.permission_level == PermissionLevel.APPROVAL
    assert a.action_type == "supersede_fact" and len(a.payload_sha256) == 64
    assert a.payload["args"]["fact_id"] == str(fact.id)
    assert a.expires_at > datetime.now(UTC) + timedelta(hours=23)

    await session.refresh(fact)
    assert fact.status == FactStatus.ASSUMPTION  # NIC se nezměnilo
    assert await session.scalar(select(func.count()).select_from(Fact)) == 6


async def test_invalid_proposals_are_refused_before_approval(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    cases = [
        ({"new_status": "verified", "source_note": ""}, "zdroj"),
        ({"new_status": "outdated"}, "Neplatný stav"),
        ({"reason": ""}, "důvod"),
        ({"ref": "F-zzzz"}, "Neplatná reference"),
        ({"new_status": "assumption", "source_note": ""}, "nic nemění"),
    ]
    for override, expected in cases:
        _, llm = await propose(sessions, fact, **override)
        assert expected in tool_result(llm)["error"], override
    assert await session.scalar(select(func.count()).select_from(Approval)) == 0


async def test_scheduled_run_cannot_request_approval(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    llm = FakeLLM(
        script=[
            tool(
                "supersede_fact",
                ref=fact_ref(fact.id),
                new_status="verified",
                source_note="x",
                reason="y",
            ),
            text("ok"),
        ]
    )
    m = briefing_message(
        day=date(2026, 11, 9),
        channel_id="c",
        channel_name="chief-of-staff",
        model="claude-opus-5-5",
    )
    await ChatService(sessions, llm, cfg()).handle(m)
    assert "automatickém běhu" in tool_result(llm)["error"]
    assert await session.scalar(select(func.count()).select_from(Approval)) == 0


def test_approval_tool_without_prepare_is_rejected_at_startup() -> None:
    async def h(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match="prepare"):
        ToolRegistry([Tool("x", "x", {"type": "object"}, PermissionLevel.APPROVAL, h)])


# --------------------------------------------------------------- rozhodnutí


async def test_approve_executes_exactly_what_was_shown(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    svc = ApprovalService(sessions, registry())

    d = await svc.decide(reply.approvals[0].id, approve=True, actor=OWNER)
    assert d.status == ApprovalStatus.EXECUTED, d.message

    await session.refresh(fact)
    assert fact.status == FactStatus.OUTDATED and fact.superseded_by_id is not None
    new = await session.get_one(Fact, fact.superseded_by_id)
    assert new.status == FactStatus.VERIFIED and new.statement == fact.statement
    assert "schváleno" in new.source and "potvrdil uživatel" in new.source
    assert d.result is not None and d.result["novy_ref"] == fact_ref(new.id)

    a = await session.get_one(Approval, reply.approvals[0].id)
    assert a.status == ApprovalStatus.EXECUTED and a.decided_by == OWNER
    assert a.executed_at is not None
    actions = set((await session.scalars(select(AuditLog.action))).all())
    assert {
        "approval.requested",
        "approval.approved",
        "approval.executed",
        "fact.superseded",
    } <= actions

    # historie zůstává: původní fakt je dohledatelný jako neaktuální
    llm = FakeLLM(
        script=[tool("search_facts", query="urbanistická", include_outdated=True), text("…")]
    )
    await ChatService(sessions, llm, cfg()).handle(msg())
    states = sorted(f["stav"] for f in tool_result(llm)["fakta"])
    assert states == ["outdated", "verified"]


async def test_reject_changes_nothing(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    d = await ApprovalService(sessions, registry()).decide(
        reply.approvals[0].id, approve=False, actor=OWNER
    )
    assert d.status == ApprovalStatus.REJECTED
    await session.refresh(fact)
    assert fact.status == FactStatus.ASSUMPTION and fact.superseded_by_id is None


async def test_cannot_decide_twice(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    svc = ApprovalService(sessions, registry())
    aid = reply.approvals[0].id
    assert (await svc.decide(aid, approve=True, actor=OWNER)).status == ApprovalStatus.EXECUTED
    again = await svc.decide(aid, approve=True, actor=OWNER)
    assert "už bylo rozhodnuto" in again.message
    assert await session.scalar(select(func.count()).select_from(Fact)) == 7  # 6 + 1, ne + 2


async def test_expired_approval_is_not_executed(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    aid = reply.approvals[0].id
    await session.execute(
        update(Approval)
        .where(Approval.id == aid)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()
    d = await ApprovalService(sessions, registry()).decide(aid, approve=True, actor=OWNER)
    assert d.status == ApprovalStatus.EXPIRED
    await session.refresh(fact)
    assert fact.status == FactStatus.ASSUMPTION


async def test_tampered_payload_is_not_executed(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    aid = reply.approvals[0].id
    a = await session.get_one(Approval, aid)
    payload = dict(a.payload)
    payload["args"] = {**payload["args"], "new_statement": "Studie neexistuje."}
    await session.execute(update(Approval).where(Approval.id == aid).values(payload=payload))
    await session.commit()

    d = await ApprovalService(sessions, registry()).decide(aid, approve=True, actor=OWNER)
    assert d.status == ApprovalStatus.FAILED and "změněn" in d.message
    await session.refresh(fact)
    assert fact.status == FactStatus.ASSUMPTION
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "approval.tampered"))


async def test_fact_changed_meanwhile_is_not_overwritten(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fact = await assumption_fact(session)
    reply, _ = await propose(sessions, fact)
    await session.execute(
        update(Fact).where(Fact.id == fact.id).values(statement="Studie je ve fázi konceptu.")
    )
    await session.commit()
    d = await ApprovalService(sessions, registry()).decide(
        reply.approvals[0].id, approve=True, actor=OWNER
    )
    assert d.status == ApprovalStatus.FAILED and "mezitím změnil" in d.message
    a = await session.get_one(Approval, reply.approvals[0].id)
    assert a.status == ApprovalStatus.FAILED and a.result is not None


async def test_unknown_approval() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    svc = ApprovalService(async_sessionmaker(engine), registry())
    d = await svc.decide(uuid.uuid4(), approve=True, actor=OWNER)
    await engine.dispose()
    assert d.status == ApprovalStatus.FAILED and "neexistuje" in d.message


# ------------------------------------------------------------------ Discord


def test_button_ids_roundtrip_and_reject_garbage() -> None:
    aid = uuid.uuid4()
    view = approval_view(aid)
    ids = [item.custom_id for item in view.children]  # type: ignore[attr-defined]
    assert [parse_approval_custom_id(i) for i in ids] == [(True, aid), (False, aid)]
    for bad in (
        "",
        "aihq:approval:delete:" + str(aid),
        "aihq:approval:approve:nope",
        "jiny:approve:" + str(aid),
    ):
        assert parse_approval_custom_id(bad) is None
    assert all(len(i) <= 100 for i in ids)  # limit Discordu pro custom_id


def test_approval_card_shows_ref_and_expiry() -> None:
    from app.tools.registry import ApprovalRequest

    req = ApprovalRequest(
        uuid.uuid4(), "A-12345678", "🔁 Nahradit fakt…", datetime(2026, 11, 9, 19, 0, tzinfo=UTC)
    )
    card = approval_card(req, "Europe/Prague")
    assert "[A-12345678]" in card and "9. 11. 20:00" in card and "Nahradit fakt" in card
