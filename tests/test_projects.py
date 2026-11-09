"""Zakládání projektů: návrh → schválení → projekt (a případně firma) → kanál podle zkratky."""

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.approvals import ApprovalService
from app.agent.chat import ChatReply, ChatService, InboundMessage
from app.config import Settings
from app.db.enums import ApprovalStatus
from app.db.models import AuditLog, Company, Conversation, Project
from app.llm.base import ToolResultBlock
from app.seed import seed
from app.tools.memory import MEMORY_TOOLS
from app.tools.projects import PROJECT_TOOLS, company_key, slugify
from app.tools.registry import ToolRegistry
from app.tools.tasks import TASK_TOOLS
from tests.fakes import FakeLLM, text, tool

OWNER = "user:discord:111"
_n = 0


def msg(t: str = "…", channel: str = "chief-of-staff") -> InboundMessage:
    global _n
    _n += 1
    return InboundMessage("discord", f"ch-{channel}", channel, f"pr{_n}", "42", t)


def cfg() -> Settings:
    return Settings(llm_model_default="claude-sonnet-5-5")


def tool_result(llm: FakeLLM) -> dict[str, Any]:
    blocks = llm.calls[-1]["messages"][-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    return json.loads(blocks[0].content)  # type: ignore[no-any-return]


async def propose(
    sessions: async_sessionmaker[AsyncSession], **args: Any
) -> tuple[ChatReply, FakeLLM]:
    llm = FakeLLM(script=[tool("create_project", **args), text("Čeká na schválení.")])
    reply = await ChatService(sessions, llm, cfg()).handle(msg())
    assert reply is not None
    return reply, llm


def approvals(sessions: async_sessionmaker[AsyncSession]) -> ApprovalService:
    return ApprovalService(sessions, ToolRegistry(MEMORY_TOOLS + TASK_TOOLS + PROJECT_TOOLS))


@pytest.mark.parametrize(
    ("text_in", "slug"),
    [
        ("Brankovice", "brankovice"),
        ("Bytový dům Brankovice", "bytovy-dum-brankovice"),
        ("  Osada Pomněnice – etapa II ", "osada-pomnenice-etapa-ii"),
        ("Klecany 9 BJ!", "klecany-9-bj"),
    ],
)
def test_slugify(text_in: str, slug: str) -> None:
    assert slugify(text_in) == slug


def test_company_key_ignores_legal_form_and_accents() -> None:
    assert company_key("Benet, s.r.o.") == company_key("benet") == company_key("BENET s. r. o.")
    assert company_key("Construction Europe, s.r.o.") == "construction europe"
    assert company_key("Búda a.s.") == "buda"


async def test_new_project_under_existing_company(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)  # firma Benet, s.r.o.
    await session.commit()
    reply, llm = await propose(
        sessions,
        name="Bytový dům Brankovice",
        company="Benet",
        slug="brankovice",
        aliases=["Brankovice", "BD Brankovice"],
    )
    out = tool_result(llm)
    assert out["ceka_na_schvaleni"] is True
    summary = reply.approvals[0].summary
    assert "Založit projekt Bytový dům Brankovice" in summary
    assert "Firma: Benet, s.r.o." in summary and "nová" not in summary
    assert "#brankovice" in summary
    assert await session.scalar(select(Project).where(Project.slug == "brankovice")) is None

    d = await approvals(sessions).decide(reply.approvals[0].id, approve=True, actor=OWNER)
    assert d.status == ApprovalStatus.EXECUTED, d.message
    assert d.result is not None and d.result["discord_channel"] == "brankovice"

    project = await session.scalar(select(Project).where(Project.slug == "brankovice"))
    assert project is not None and project.aliases == ["Brankovice", "BD Brankovice"]
    company = await session.get_one(Company, project.company_id)
    assert company.name == "Benet, s.r.o."
    assert await session.scalar(select(func.count()).select_from(Company)) == 1
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "project.created"))

    # zprávy v kanálu #brankovice se automaticky přiřadí k novému projektu
    llm2 = FakeLLM(script=[tool("search_facts", query=""), text("V paměti nic nemám.")])
    await ChatService(sessions, llm2, cfg()).handle(msg("Co víme?", channel="brankovice"))
    conv = await session.scalar(
        select(Conversation).where(Conversation.external_id == "ch-brankovice")
    )
    assert conv is not None and conv.project_id == project.id
    assert tool_result(llm2)["projekt"] == "Bytový dům Brankovice"
    assert "Bytový dům Brankovice" in str(llm2.calls[0]["system"])


async def test_new_company_is_created_and_flagged(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    reply, _ = await propose(sessions, name="Klecany", company="Klecany Development s.r.o.")
    assert "nová – bude založena" in reply.approvals[0].summary
    d = await approvals(sessions).decide(reply.approvals[0].id, approve=True, actor=OWNER)
    assert d.status == ApprovalStatus.EXECUTED, d.message
    company = await session.scalar(
        select(Company).where(Company.slug == "klecany-development-s-r-o")
    )
    assert company is not None
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "company.created"))


async def test_duplicates_and_missing_company_are_refused(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed(session)
    await session.commit()
    for args, expected in [
        ({"name": "Osada Pomněnice", "company": "Benet"}, "už existuje"),
        ({"name": "Pomnenice", "company": "Benet"}, "už existuje"),  # alias bez diakritiky
        ({"name": "Nový projekt", "company": ""}, "firmu"),
        ({"name": "X", "company": "Benet"}, "název"),
    ]:
        _, llm = await propose(sessions, **args)
        assert expected in tool_result(llm)["error"], args


async def test_rejected_proposal_creates_nothing(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    reply, _ = await propose(sessions, name="Brankovice", company="Benet")
    d = await approvals(sessions).decide(reply.approvals[0].id, approve=False, actor=OWNER)
    assert d.status == ApprovalStatus.REJECTED
    assert await session.scalar(select(func.count()).select_from(Project)) == 0
    assert await session.scalar(select(func.count()).select_from(Company)) == 0


async def test_project_created_meanwhile_is_not_duplicated(
    sessions: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    first, _ = await propose(sessions, name="Brankovice", company="Benet")
    second, _ = await propose(sessions, name="Brankovice", company="Benet")
    svc = approvals(sessions)
    ok = await svc.decide(first.approvals[0].id, approve=True, actor=OWNER)
    dup = await svc.decide(second.approvals[0].id, approve=True, actor=OWNER)
    assert ok.status == ApprovalStatus.EXECUTED
    assert dup.status == ApprovalStatus.FAILED and "už existuje" in dup.message
    assert await session.scalar(select(func.count()).select_from(Project)) == 1
    # druhá žádost nesmí založit ani druhou firmu
    assert await session.scalar(select(func.count()).select_from(Company)) == 1
