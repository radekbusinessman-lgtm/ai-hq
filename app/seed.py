"""Založí výchozí firmu a pilotní projekt Osada Pomněnice.

Spuštění:  python -m app.seed
Opakované spuštění nic nezdvojí.

Počáteční kontext ze zadání je uložen jako PRACOVNÍ PŘEDPOKLADY (ne ověřená fakta).
Ověřená fakta vzniknou až z dokumentů, e-mailů a potvrzení uživatele.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import FactStatus
from app.db.models import AuditLog, Company, Fact, Project
from app.db.session import session_scope

SEED_SOURCE = "seed:zadani-ai-hq"

COMPANY = {"name": "Benet, s.r.o.", "slug": "benet"}

PROJECT = {
    "name": "Osada Pomněnice",
    "slug": "pomnenice",
    "aliases": ["Pomněnice", "Pomnenice", "Osada Pomnenice"],
    "description": "Rezidenční developerský projekt u Benešova.",
}

INITIAL_ASSUMPTIONS: list[tuple[str, str]] = [
    ("lokalita", "Projekt se nachází u Benešova."),
    ("rozsah", "Rozloha projektu je přibližně 5 hektarů."),
    ("rozsah", "Dlouhodobý záměr je přibližně 80 rodinných domů."),
    ("etapizace", "Výstavba je plánována v několika etapách."),
    ("dokumentace", "Existuje urbanistická studie a související projektová dokumentace."),
    ("infrastruktura", "Řeší se zásobování vodou, odpadní vody, energetika a komunikace."),
]


async def seed(session: AsyncSession) -> Project:
    company = await session.scalar(select(Company).where(Company.slug == COMPANY["slug"]))
    if company is None:
        company = Company(**COMPANY, source=SEED_SOURCE)
        session.add(company)
        await session.flush()

    project = await session.scalar(
        select(Project).where(Project.company_id == company.id, Project.slug == PROJECT["slug"])
    )
    if project is not None:
        return project

    project = Project(company_id=company.id, source=SEED_SOURCE, **PROJECT)
    session.add(project)
    await session.flush()

    for topic, statement in INITIAL_ASSUMPTIONS:
        session.add(
            Fact(
                company_id=company.id,
                project_id=project.id,
                topic=topic,
                statement=statement,
                status=FactStatus.ASSUMPTION,
                source=SEED_SOURCE,
            )
        )
    session.add(
        AuditLog(
            actor="system",
            action="seed.project_created",
            entity_type="project",
            entity_id=str(project.id),
            company_id=company.id,
            details={"assumptions": len(INITIAL_ASSUMPTIONS)},
        )
    )
    return project


async def main() -> None:
    async with session_scope() as s:
        project = await seed(s)
        print(f"Projekt připraven: {project.name} ({project.id})")


if __name__ == "__main__":
    asyncio.run(main())
