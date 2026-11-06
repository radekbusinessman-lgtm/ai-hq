"""Testy datového modelu firemní paměti."""

import hashlib

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import FactStatus
from app.db.models import (
    AuditLog,
    Company,
    Document,
    DocumentChunk,
    DocumentVersion,
    Fact,
    Project,
    SystemState,
    Task,
)
from app.seed import seed


async def make_company(s: AsyncSession, slug: str) -> Company:
    c = Company(name=slug.upper(), slug=slug)
    s.add(c)
    await s.flush()
    return c


async def test_system_defaults_seeded(session: AsyncSession) -> None:
    rows = dict((await session.execute(select(SystemState.key, SystemState.value))).all())
    assert rows["agents_enabled"] is True
    assert rows["monthly_budget_usd"] == 50


async def test_seed_pomnenice_is_idempotent_and_uses_assumptions(session: AsyncSession) -> None:
    p1 = await seed(session)
    p2 = await seed(session)
    assert p1.id == p2.id
    assert "Pomněnice" in p1.aliases

    facts = (await session.scalars(select(Fact).where(Fact.project_id == p1.id))).all()
    assert len(facts) == 6
    # Počáteční kontext NESMÍ být uložen jako ověřený fakt.
    assert {f.status for f in facts} == {FactStatus.ASSUMPTION}
    assert all(f.source == "seed:zadani-ai-hq" for f in facts)


async def test_every_record_has_source_and_timestamps(session: AsyncSession) -> None:
    c = await make_company(session, "acme")
    await session.refresh(c)
    assert c.source == "system"
    assert c.created_at is not None and c.updated_at is not None


async def test_project_slug_unique_per_company_only(session: AsyncSession) -> None:
    a = await make_company(session, "firma-a")
    b = await make_company(session, "firma-b")
    session.add_all(
        [Project(company_id=a.id, name="X", slug="x"), Project(company_id=b.id, name="X", slug="x")]
    )
    await session.flush()  # stejný slug v jiné firmě je v pořádku

    session.add(Project(company_id=a.id, name="X2", slug="x"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_company_data_separated(session: AsyncSession) -> None:
    a = await make_company(session, "sep-a")
    b = await make_company(session, "sep-b")
    session.add_all([Task(company_id=a.id, title="úkol A"), Task(company_id=b.id, title="úkol B")])
    await session.flush()
    titles = (await session.scalars(select(Task.title).where(Task.company_id == a.id))).all()
    assert titles == ["úkol A"]


async def test_invalid_fact_status_rejected(session: AsyncSession) -> None:
    c = await make_company(session, "bad-status")
    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "INSERT INTO facts (company_id, statement, status) "
                "VALUES (:c, 'x', 'just_guessing')"
            ),
            {"c": c.id},
        )


async def test_audit_log_is_append_only(session: AsyncSession) -> None:
    session.add(AuditLog(actor="system", action="test.event"))
    await session.flush()

    for stmt in ("UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs"):
        nested = await session.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(stmt))
        await nested.rollback()

    count = await session.scalar(select(func.count()).select_from(AuditLog))
    assert count == 1


async def test_duplicate_document_version_rejected(session: AsyncSession) -> None:
    c = await make_company(session, "docs")
    d = Document(company_id=c.id, title="Urbanistická studie")
    session.add(d)
    await session.flush()
    sha = hashlib.sha256(b"obsah").hexdigest()
    session.add(
        DocumentVersion(
            document_id=d.id,
            version_no=1,
            file_name="a.pdf",
            storage_path="x",
            size_bytes=5,
            sha256=sha,
        )
    )
    await session.flush()
    session.add(
        DocumentVersion(
            document_id=d.id,
            version_no=2,
            file_name="a.pdf",
            storage_path="y",
            size_bytes=5,
            sha256=sha,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_chunk_fulltext_and_vector_search(session: AsyncSession) -> None:
    c = await make_company(session, "search")
    d = Document(company_id=c.id, title="Vyjádření VaK")
    session.add(d)
    await session.flush()
    v = DocumentVersion(
        document_id=d.id,
        version_no=1,
        file_name="vak.pdf",
        storage_path="z",
        size_bytes=1,
        sha256="0" * 64,
    )
    session.add(v)
    await session.flush()

    vec_water = [1.0] + [0.0] * 1023
    vec_road = [0.0, 1.0] + [0.0] * 1022
    session.add_all(
        [
            DocumentChunk(
                company_id=c.id,
                version_id=v.id,
                chunk_index=0,
                page=3,
                text="Kapacita vodovodu pro 80 domů není zajištěna.",
                embedding=vec_water,
            ),
            DocumentChunk(
                company_id=c.id,
                version_id=v.id,
                chunk_index=1,
                page=7,
                text="Napojení na komunikaci III. třídy.",
                embedding=vec_road,
            ),
        ]
    )
    await session.flush()

    # Fulltext (konkrétní slova, čísla, názvy)
    hit = await session.scalar(
        select(DocumentChunk.page).where(
            DocumentChunk.company_id == c.id,
            DocumentChunk.search_vector.op("@@")(func.plainto_tsquery("simple", "vodovodu")),
        )
    )
    assert hit == 3

    # Sémantické vyhledávání (nejbližší vektor)
    nearest = await session.scalar(
        select(DocumentChunk.page)
        .where(DocumentChunk.company_id == c.id)
        .order_by(DocumentChunk.embedding.cosine_distance(vec_road))
        .limit(1)
    )
    assert nearest == 7
