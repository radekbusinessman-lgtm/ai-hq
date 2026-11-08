"""Testy souběhu. Potřebují skutečně oddělená spojení, takže data COMMITUJÍ do testovací
databáze. Proto jsou v souboru, který běží jako poslední (conftest při dalším běhu DB vyčistí),
a pracují s vlastní firmou/projektem, aby neovlivnily ostatní data.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent.approvals import ApprovalService
from app.agent.chat import ChatService, InboundMessage
from app.config import Settings
from app.db.enums import FactStatus
from app.db.models import AgentRun, Company, Fact, Project
from app.tools.memory import MEMORY_TOOLS, fact_ref
from app.tools.registry import ToolRegistry
from app.tools.tasks import TASK_TOOLS
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import FakeLLM, text, tool


def cfg(**kw: Any) -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5", **kw)


@pytest.fixture
async def real_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=5)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_concurrent_duplicate_delivery_processed_once(
    real_sessions: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLLM(delay=0.05)
    svc = ChatService(real_sessions, llm, cfg())
    ext = f"race-{uuid.uuid4()}"
    m = InboundMessage("discord", f"ch-{ext}", f"kanal-{ext}", ext, "42", "ahoj")
    results = await asyncio.gather(svc.handle(m), svc.handle(m), return_exceptions=True)
    errs = [r for r in results if isinstance(r, Exception)]
    assert not errs, repr(errs)
    async with real_sessions() as s:
        runs = await s.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.idempotency_key == f"discord:{ext}")
        )
    assert runs == 1
    assert sum(1 for r in results if r is not None) == 1


async def test_double_click_approval_executes_once(
    real_sessions: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex[:8]
    async with real_sessions() as s:
        company = Company(name=f"Souběh {tag}", slug=f"soubeh-{tag}")
        s.add(company)
        await s.flush()
        project = Project(company_id=company.id, name=f"Souběh {tag}", slug=f"soubeh-{tag}")
        s.add(project)
        await s.flush()
        fact = Fact(
            company_id=company.id,
            project_id=project.id,
            statement=f"Tvrzení {tag}",
            status=FactStatus.ASSUMPTION,
        )
        s.add(fact)
        await s.commit()

    llm = FakeLLM(
        script=[
            tool(
                "supersede_fact",
                ref=fact_ref(fact.id),
                new_status="verified",
                source_note="test",
                reason="test souběhu",
            ),
            text("ok"),
        ]
    )
    m = InboundMessage("discord", f"ch-{tag}", f"kanal-{tag}", f"m-{tag}", "42", "…")
    reply = await ChatService(real_sessions, llm, cfg()).handle(m)
    assert reply is not None and len(reply.approvals) == 1

    svc = ApprovalService(real_sessions, ToolRegistry(MEMORY_TOOLS + TASK_TOOLS))
    aid = reply.approvals[0].id
    results = await asyncio.gather(
        svc.decide(aid, approve=True, actor="user:discord:1"),
        svc.decide(aid, approve=True, actor="user:discord:1"),
    )
    executed_now = [r for r in results if r.message == "Schváleno a provedeno."]
    assert len(executed_now) == 1, [r.message for r in results]
    assert any("už bylo rozhodnuto" in r.message for r in results)
    async with real_sessions() as s:
        successors = await s.scalar(
            select(func.count())
            .select_from(Fact)
            .where(Fact.project_id == project.id, Fact.status == FactStatus.VERIFIED)
        )
        assert successors == 1
