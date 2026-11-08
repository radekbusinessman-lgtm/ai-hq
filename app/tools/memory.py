"""Nástroje projektové paměti: projekty a fakta se zdrojem."""

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db.enums import FactStatus, PermissionLevel
from app.db.models import Company, Fact, Project
from app.tools.registry import Prepared, Tool, ToolContext, ToolInputError

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


STATUS_CZ = {
    "verified": "ověřeno",
    "assumption": "předpoklad",
    "ai_estimate": "odhad AI",
    "user_decision": "rozhodnutí",
    "outdated": "neaktuální",
}


async def find_fact(s: AsyncSession, ref: str) -> Fact:
    prefix = ref.strip().upper().removeprefix("F-").lower()
    if len(prefix) < 6 or not all(c in "0123456789abcdef" for c in prefix):
        raise ToolInputError(f"Neplatná reference faktu '{ref}' (očekávám např. F-1a2b3c4d).")
    facts = (await s.scalars(select(Fact))).all()
    matches = [f for f in facts if str(f.id).replace("-", "").startswith(prefix)]
    if not matches:
        raise ToolInputError(f"Fakt {ref} neexistuje.")
    if len(matches) > 1:
        raise ToolInputError(f"Reference {ref} není jednoznačná.")
    found: Fact = matches[0]
    return found


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


async def prepare_supersede_fact(ctx: ToolContext, args: dict[str, Any]) -> Prepared:
    """Ověří návrh náhrady faktu a připraví přesné parametry + čitelný souhrn ke schválení."""
    new_status = str(args.get("new_status") or "")
    if new_status not in SAVABLE_STATUSES:
        raise ToolInputError(
            f"Neplatný stav '{new_status}'. Povolené: {', '.join(SAVABLE_STATUSES)}."
        )
    source_note = str(args.get("source_note") or "").strip()
    if new_status == FactStatus.VERIFIED.value and not source_note:
        raise ToolInputError("Ověřený fakt musí mít uvedený zdroj (source_note).")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolInputError("Uveď důvod změny (reason).")
    async with ctx.sessions() as s:
        old = await find_fact(s, str(args.get("ref") or ""))
        if old.status == FactStatus.OUTDATED or old.superseded_by_id is not None:
            raise ToolInputError("Tento fakt už je neaktuální / nahrazený.")
        project = await s.get(Project, old.project_id) if old.project_id else None
    new_statement = str(args.get("new_statement") or "").strip() or old.statement
    if new_statement == old.statement and new_status == old.status.value:
        raise ToolInputError("Návrh nic nemění (stejné tvrzení i stav).")
    summary = (
        f"🔁 **Nahradit fakt {fact_ref(old.id)}**"
        f"{' (' + project.name + ')' if project else ''}\n"
        f"Původně: {old.statement} _({STATUS_CZ[old.status.value]})_\n"
        f"Nově: {new_statement} _({STATUS_CZ[new_status]})_"
        f"{' — zdroj: ' + source_note if source_note else ''}\n"
        f"Důvod: {reason}"
    )
    return Prepared(
        summary=summary,
        args={
            "fact_id": str(old.id),
            "expected_statement": old.statement,
            "expected_status": old.status.value,
            "new_statement": new_statement,
            "new_status": new_status,
            "source_note": source_note,
            "reason": reason,
        },
        company_id=old.company_id,
    )


async def supersede_fact(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Provede schválenou náhradu: starý fakt → neaktuální, nový fakt s odkazem na původní."""
    async with ctx.sessions() as s, s.begin():
        old = await s.get(Fact, uuid.UUID(args["fact_id"]), with_for_update=True)
        if old is None:
            raise ToolInputError("Původní fakt už neexistuje.")
        if (
            old.statement != args["expected_statement"]
            or old.status.value != args["expected_status"]
            or old.superseded_by_id is not None
        ):
            raise ToolInputError("Fakt se mezitím změnil – navrhněte změnu znovu.")
        source = f"{ctx.actor} via agent_run:{ctx.run_id}"
        if args.get("source_note"):
            source += f" | {args['source_note'][:100]}"
        new = Fact(
            company_id=old.company_id,
            project_id=old.project_id,
            topic=old.topic,
            statement=args["new_statement"],
            status=FactStatus(args["new_status"]),
            source_message_id=ctx.user_message_id,
            source=source[:200],
        )
        s.add(new)
        await s.flush()
        old.status = FactStatus.OUTDATED
        old.superseded_by_id = new.id
        audit(
            s,
            actor=ctx.actor,
            action="fact.superseded",
            entity_type="fact",
            entity_id=old.id,
            company_id=old.company_id,
            new_fact=str(new.id),
            reason=args.get("reason"),
        )
        return {
            "nahrazeno": True,
            "stary_ref": fact_ref(old.id),
            "novy_ref": fact_ref(new.id),
            "stav": args["new_status"],
        }


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
    Tool(
        name="supersede_fact",
        description=(
            "Navrhne nahrazení existujícího faktu (oprava údaje nebo změna stavu, např. "
            "předpoklad → ověřeno). VYŽADUJE SCHVÁLENÍ: nic se neprovede, uživatel dostane "
            "tlačítka ✅/❌. Původní fakt se po schválení označí jako neaktuální a zůstane "
            "v historii. Netvrď, že je změna provedena – je jen navržena."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Reference faktu, např. F-1a2b3c4d."},
                "new_statement": {
                    "type": "string",
                    "description": "Nové znění. Vynech, pokud se mění jen stav.",
                },
                "new_status": {"type": "string", "enum": SAVABLE_STATUSES},
                "source_note": {"type": "string", "description": "Zdroj; povinný pro verified."},
                "reason": {"type": "string", "description": "Proč se fakt mění."},
            },
            "required": ["ref", "new_status", "reason"],
        },
        level=PermissionLevel.APPROVAL,
        handler=supersede_fact,
        prepare=prepare_supersede_fact,
    ),
]
