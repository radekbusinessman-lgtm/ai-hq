"""Nástroje projektové paměti: projekty a fakta se zdrojem."""

import re
import unicodedata
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db.enums import FactStatus, PermissionLevel
from app.db.models import Company, Fact, Project
from app.tools.registry import Tool, ToolContext, ToolInputError

# Model smí ukládat jen tyto stavy. "outdated" vzniká pouze nahrazením faktu (se schválením).
SAVABLE_STATUSES = [
    FactStatus.VERIFIED.value,
    FactStatus.ASSUMPTION.value,
    FactStatus.AI_ESTIMATE.value,
    FactStatus.USER_DECISION.value,
]
MAX_RESULTS = 30


def fact_ref(fact_id: Any) -> str:
    """Krátké ID pro citaci v odpovědi, např. F-1a2b3c4d."""
    return "F-" + str(fact_id).replace("-", "")[:8]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def search_terms(query: str) -> list[str]:
    """Kořeny slov pro hledání: 'vodovodu' → 'vodov' (zachytí i vodovod, vodovodní…)."""
    words = re.findall(r"[^\W_]+", query.lower())
    return [w[:5] if len(w) > 5 else w for w in words if len(w) >= 3 or w.isdigit()]


async def resolve_project(s: AsyncSession, ctx: ToolContext, name: str | None) -> Project:
    if not name:
        if ctx.project_id is None:
            raise ToolInputError("Konverzace není přiřazena k projektu – uveď název projektu.")
        return await s.get_one(Project, ctx.project_id)
    needle = _strip_accents(name.strip().lower())
    for p in (await s.scalars(select(Project))).all():
        names = [p.slug, p.name, *p.aliases]
        if any(_strip_accents(n.lower()) == needle for n in names):
            return p
    raise ToolInputError(f"Projekt '{name}' neznám. Použij list_projects.")


def _fact_dict(f: Fact, project_name: str) -> dict[str, Any]:
    return {
        "ref": fact_ref(f.id),
        "projekt": project_name,
        "tema": f.topic,
        "tvrzeni": f.statement,
        "stav": f.status.value,
        "zdroj": f.source,
        "zalozeno": f.created_at.date().isoformat(),
    }


# ------------------------------------------------------------------ handlery


async def list_projects(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with ctx.sessions() as s:
        rows = (
            await s.execute(
                select(Project, Company.name)
                .join(Company, Company.id == Project.company_id)
                .order_by(Project.name)
            )
        ).all()
    return {
        "projekty": [
            {
                "nazev": p.name,
                "slug": p.slug,
                "firma": company,
                "stav": p.status,
                "aliasy": p.aliases,
            }
            for p, company in rows
        ]
    }


async def search_facts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    include_outdated = bool(args.get("include_outdated", False))
    async with ctx.sessions() as s:
        project = await resolve_project(s, ctx, args.get("project"))
        stmt = select(Fact).where(Fact.project_id == project.id)
        if not include_outdated:
            stmt = stmt.where(Fact.status != FactStatus.OUTDATED)
        if args.get("topic"):
            stmt = stmt.where(func.lower(Fact.topic) == str(args["topic"]).lower())
        terms = search_terms(query)
        if terms:
            stmt = stmt.where(
                or_(
                    *[Fact.statement.ilike(f"%{t}%") for t in terms],
                    *[Fact.topic.ilike(f"%{t}%") for t in terms],
                )
            )
        facts = (await s.scalars(stmt.order_by(Fact.created_at.desc()).limit(MAX_RESULTS))).all()
    return {
        "projekt": project.name,
        "pocet": len(facts),
        "fakta": [_fact_dict(f, project.name) for f in facts],
        "poznamka": "Žádná odpovídající fakta – paměť o tomto zatím nic neví."
        if not facts
        else None,
    }


async def save_fact(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    statement = str(args.get("statement") or "").strip()
    status = str(args.get("status") or "")
    if not statement:
        raise ToolInputError("Chybí tvrzení (statement).")
    if status not in SAVABLE_STATUSES:
        raise ToolInputError(f"Neplatný stav '{status}'. Povolené: {', '.join(SAVABLE_STATUSES)}.")
    source_note = str(args.get("source_note") or "").strip()
    if status == FactStatus.VERIFIED.value and not source_note:
        raise ToolInputError("Ověřený fakt musí mít uvedený zdroj (source_note).")

    async with ctx.sessions() as s, s.begin():
        project = await resolve_project(s, ctx, args.get("project"))
        source = f"{ctx.actor} via agent_run:{ctx.run_id}"
        if source_note:
            source += f" | {source_note[:120]}"
        fact = Fact(
            company_id=project.company_id,
            project_id=project.id,
            topic=(str(args["topic"]).strip().lower() if args.get("topic") else None),
            statement=statement,
            status=FactStatus(status),
            source_message_id=ctx.user_message_id,
            source=source[:200],
        )
        s.add(fact)
        await s.flush()
        audit(
            s,
            actor=f"agent:{ctx.run_id}",
            action="fact.created",
            entity_type="fact",
            entity_id=fact.id,
            company_id=project.company_id,
            status=status,
        )
        ref = fact_ref(fact.id)
    return {"ulozeno": True, "ref": ref, "projekt": project.name, "stav": status}


# ---------------------------------------------------------------- definice

PROJECT_PARAM = {
    "type": "string",
    "description": "Název nebo zkratka projektu. Vynech, pokud se konverzace týká projektu kanálu.",
}

MEMORY_TOOLS = [
    Tool(
        name="list_projects",
        description="Vypíše projekty evidované v paměti (název, firma, aliasy).",
        input_schema={"type": "object", "properties": {}},
        level=PermissionLevel.AUTO,
        handler=list_projects,
    ),
    Tool(
        name="search_facts",
        description=(
            "Vyhledá uložená fakta o projektu. Vrací tvrzení, stav spolehlivosti, zdroj a ref "
            "pro citaci. Prázdný dotaz vrátí nejnovější fakta. Použij VŽDY před odpovědí na "
            "otázku o projektu."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Co hledáš, česky (např. 'kanalizace').",
                },
                "project": PROJECT_PARAM,
                "topic": {"type": "string", "description": "Volitelně jen jedno téma."},
                "include_outdated": {"type": "boolean", "description": "Zahrnout i neaktuální."},
            },
        },
        level=PermissionLevel.AUTO,
        handler=search_facts,
    ),
    Tool(
        name="save_fact",
        description=(
            "Uloží nové tvrzení o projektu do paměti. Stav zvol pravdivě: verified = potvrzeno "
            "zdrojem (dokument, úřad, uživatel výslovně potvrdil); assumption = pracovní "
            "předpoklad; ai_estimate = tvůj odhad; user_decision = rozhodnutí uživatele. "
            "Ukládej jen to, co uživatel sdělil nebo požádal uložit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "Jedno konkrétní tvrzení."},
                "status": {"type": "string", "enum": SAVABLE_STATUSES},
                "topic": {
                    "type": "string",
                    "description": "Téma jedním slovem: voda, kanalizace, energie, komunikace, "
                    "povoleni, rozpocet, harmonogram, financovani, kontakty, jine…",
                },
                "source_note": {
                    "type": "string",
                    "description": "Odkud informace je (např. 'sdělil uživatel', "
                    "'vyjádření VaK z 3/2026'). Povinné pro verified.",
                },
                "project": PROJECT_PARAM,
            },
            "required": ["statement", "status"],
        },
        level=PermissionLevel.AUTO,
        handler=save_fact,
    ),
]
