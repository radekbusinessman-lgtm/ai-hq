"""Zakládání projektů (úroveň 2 – vyžaduje schválení).

Po schválení vznikne projekt (a případně nová firma). Discord vrstva pak založí
soukromý kanál pojmenovaný podle zkratky projektu (#slug), který se k projektu
automaticky přiřadí.
"""

import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db.enums import PermissionLevel
from app.db.models import Company, Project
from app.tools.registry import Prepared, Tool, ToolContext, ToolInputError

COMPANY_SUFFIXES = re.compile(r"\b(s\.?\s?r\.?\s?o\.?|a\.?\s?s\.?|spol\.?|z\.?\s?s\.?)\s*$")
MAX_ALIASES = 8


def slugify(text: str, max_len: int = 60) -> str:
    """„Bytový dům Brankovice“ → „bytovy-dum-brankovice“ (vhodné i pro název kanálu)."""
    plain = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    ).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return slug[:max_len].rstrip("-")


def company_key(name: str) -> str:
    """Klíč pro porovnání názvů firem: bez diakritiky, velikosti písmen a „s.r.o.“."""
    plain = (
        "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
        .lower()
        .strip()
    )
    plain = plain.rstrip(", ")
    plain = COMPANY_SUFFIXES.sub("", plain).strip(" ,.")
    return re.sub(r"\s+", " ", plain)


async def find_company(s: AsyncSession, name: str) -> Company | None:
    key = company_key(name)
    for c in (await s.scalars(select(Company))).all():
        if company_key(c.name) == key or c.slug == slugify(name):
            return c
    return None


async def _clean_args(s: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if len(name) < 2:
        raise ToolInputError("Chybí název projektu (name).")
    company_name = str(args.get("company") or "").strip()
    if not company_name:
        raise ToolInputError(
            "Uveď firmu, pod kterou projekt patří (company). Pokud ji nevíš, zeptej se."
        )
    slug = slugify(str(args.get("slug") or "") or name)
    if len(slug) < 2:
        raise ToolInputError("Z názvu nejde vytvořit zkratku – uveď slug (např. brankovice).")
    raw_aliases = args.get("aliases") or []
    if not isinstance(raw_aliases, list):
        raise ToolInputError("aliases musí být seznam.")
    aliases = []
    for a in [str(x).strip() for x in raw_aliases][:MAX_ALIASES]:
        if a and a != name and a not in aliases:
            aliases.append(a[:120])

    existing = (await s.scalars(select(Project))).all()
    taken = {slugify(n) for p in existing for n in (p.slug, p.name, *p.aliases)}
    if slug in taken or slugify(name) in taken:
        raise ToolInputError(f"Projekt „{name}“ (nebo zkratka {slug}) už existuje.")

    company = await find_company(s, company_name)
    return {
        "name": name[:200],
        "slug": slug,
        "aliases": aliases,
        "description": (str(args.get("description") or "").strip()[:2000] or None),
        "company_id": str(company.id) if company else None,
        "company_name": company.name if company else company_name[:200],
        "create_company": company is None,
    }


async def prepare_create_project(ctx: ToolContext, args: dict[str, Any]) -> Prepared:
    async with ctx.sessions() as s:
        clean = await _clean_args(s, args)
    company_line = (
        f"Firma: **{clean['company_name']}** _(nová – bude založena)_"
        if clean["create_company"]
        else f"Firma: {clean['company_name']}"
    )
    lines = [
        f"🆕 **Založit projekt {clean['name']}**",
        company_line,
        f"Kanál v Discordu: #{clean['slug']}",
    ]
    if clean["aliases"]:
        lines.append("Další názvy: " + ", ".join(clean["aliases"]))
    if clean["description"]:
        lines.append(f"Popis: {clean['description'][:300]}")
    return Prepared(summary="\n".join(lines), args=clean)


async def create_project(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Provede schválené založení. Kontroluje znovu, že projekt mezitím nevznikl."""
    async with ctx.sessions() as s, s.begin():
        recheck = await _clean_args(
            s,
            {
                "name": args["name"],
                "slug": args["slug"],
                "company": args["company_name"],
                "aliases": args["aliases"],
            },
        )
        if args["create_company"]:
            company = await find_company(s, args["company_name"])
            if company is None:
                company = Company(
                    name=args["company_name"],
                    slug=slugify(args["company_name"]),
                    source=f"{ctx.actor} via agent_run:{ctx.run_id}"[:200],
                )
                s.add(company)
                await s.flush()
                audit(
                    s,
                    actor=ctx.actor,
                    action="company.created",
                    entity_type="company",
                    entity_id=company.id,
                    company_id=company.id,
                )
        else:
            company = await s.get(Company, recheck["company_id"] or args["company_id"])
            if company is None:
                raise ToolInputError("Firma mezitím zanikla – navrhněte projekt znovu.")
        project = Project(
            company_id=company.id,
            name=args["name"],
            slug=args["slug"],
            aliases=args["aliases"],
            description=args["description"],
            source=f"{ctx.actor} via agent_run:{ctx.run_id}"[:200],
        )
        s.add(project)
        await s.flush()
        audit(
            s,
            actor=ctx.actor,
            action="project.created",
            entity_type="project",
            entity_id=project.id,
            company_id=company.id,
            slug=project.slug,
        )
        return {
            "zalozeno": True,
            "projekt": project.name,
            "slug": project.slug,
            "firma": company.name,
            "discord_channel": project.slug,  # Discord vrstva založí kanál #slug
        }


PROJECT_TOOLS = [
    Tool(
        name="create_project",
        description=(
            "Navrhne založení nového projektu (a firmy, pokud ještě neexistuje). VYŽADUJE "
            "SCHVÁLENÍ tlačítkem. Po schválení vznikne i soukromý kanál #zkratka v Discordu. "
            "Firmu (company) nikdy nehádej – když ji uživatel neřekl, zeptej se. Nejdřív ověř "
            "list_projects, že projekt už neexistuje."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Název, např. „Bytový dům Brankovice“."},
                "company": {"type": "string", "description": "Firma, pod kterou projekt patří."},
                "slug": {
                    "type": "string",
                    "description": "Krátká zkratka pro kanál (např. brankovice). Volitelné.",
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Jiné názvy, pod kterými se o projektu mluví.",
                },
                "description": {"type": "string", "description": "Stručný popis (volitelné)."},
            },
            "required": ["name", "company"],
        },
        level=PermissionLevel.APPROVAL,
        handler=create_project,
        prepare=prepare_create_project,
    ),
]
