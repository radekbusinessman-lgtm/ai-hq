"""Registr nástrojů agenta.

Agent smí používat POUZE nástroje registrované zde. Každý nástroj má pevně danou úroveň
oprávnění v kódu – model ji nemůže změnit:

- AUTO (úroveň 1): provede se hned.
- APPROVAL (úroveň 2): neprovede se. Vytvoří se žádost o schválení (approvals) s přesným
  obsahem akce a jeho otiskem (SHA-256). Provede se až po schválení vlastníkem v Discordu
  (app.agent.approvals). V automatických bězích (plánovač) se žádosti nevytvářejí vůbec.
- Úroveň 3 (platby, podpisy…) v systému jako nástroj neexistuje.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import audit
from app.db.enums import PermissionLevel
from app.db.models import Approval
from app.llm.base import ToolSpec

APPROVAL_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class ToolContext:
    sessions: async_sessionmaker[AsyncSession]
    run_id: uuid.UUID
    company_id: uuid.UUID | None  # firma konverzace (pokud je kanál přiřazen projektu)
    project_id: uuid.UUID | None  # projekt konverzace
    user_message_id: uuid.UUID | None  # zpráva, která běh vyvolala (zdroj pro fakta)
    actor: str  # kdo běh vyvolal, např. user:discord:123
    allow_approval_requests: bool = True  # False v automatických bězích


class ToolInputError(Exception):
    """Chybný vstup od modelu – vrátí se modelu jako chyba nástroje, ne jako pád běhu."""


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Prepared:
    """Výsledek přípravy akce ke schválení: čitelný souhrn + přesné parametry provedení."""

    summary: str
    args: dict[str, Any]
    company_id: uuid.UUID | None = None


Preparer = Callable[[ToolContext, dict[str, Any]], Awaitable[Prepared]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    level: PermissionLevel
    handler: Handler
    prepare: Preparer | None = None  # povinné pro úroveň APPROVAL

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)


@dataclass(frozen=True)
class ApprovalRequest:
    id: uuid.UUID
    ref: str
    summary: str
    expires_at: datetime


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # JSON pro model
    is_error: bool
    approval: ApprovalRequest | None = field(default=None)


def approval_ref(approval_id: Any) -> str:
    return "A-" + str(approval_id).replace("-", "")[:8]


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        for t in tools:
            if t.level == PermissionLevel.APPROVAL and t.prepare is None:
                raise ValueError(f"Nástroj {t.name} úrovně APPROVAL musí mít prepare()")
        self._tools = {t.name: t for t in tools}

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return _err(f"Nástroj '{name}' neexistuje nebo není povolen.")
        try:
            if tool.level == PermissionLevel.APPROVAL:
                return await self._request_approval(ctx, tool, args)
            if tool.level != PermissionLevel.AUTO:
                return _err(f"Nástroj '{name}' není povolen.")
            result = await tool.handler(ctx, args)
        except ToolInputError as e:
            return _err(str(e))
        return ToolOutcome(json.dumps(result, ensure_ascii=False, default=str), is_error=False)

    async def _request_approval(
        self, ctx: ToolContext, tool: Tool, args: dict[str, Any]
    ) -> ToolOutcome:
        if not ctx.allow_approval_requests:
            return _err("V automatickém běhu nelze navrhovat akce ke schválení.")
        assert tool.prepare is not None
        prepared = await tool.prepare(ctx, args)
        payload: dict[str, Any] = {
            "tool": tool.name,
            "args": prepared.args,
            "context": {
                "company_id": str(ctx.company_id) if ctx.company_id else None,
                "project_id": str(ctx.project_id) if ctx.project_id else None,
                "user_message_id": str(ctx.user_message_id) if ctx.user_message_id else None,
                "requested_by": ctx.actor,
            },
        }
        expires = datetime.now(UTC) + APPROVAL_TTL
        async with ctx.sessions() as s, s.begin():
            approval = Approval(
                company_id=prepared.company_id or ctx.company_id,
                agent_run_id=ctx.run_id,
                action_type=tool.name,
                permission_level=PermissionLevel.APPROVAL,
                summary=prepared.summary,
                payload=payload,
                payload_sha256=payload_hash(payload),
                expires_at=expires,
                source=f"agent_run:{ctx.run_id}",
            )
            s.add(approval)
            await s.flush()
            audit(
                s,
                actor=f"agent:{ctx.run_id}",
                action="approval.requested",
                entity_type="approval",
                entity_id=approval.id,
                company_id=approval.company_id,
                tool=tool.name,
            )
            request = ApprovalRequest(
                approval.id, approval_ref(approval.id), prepared.summary, expires
            )
        content = {
            "ceka_na_schvaleni": True,
            "ref": request.ref,
            "souhrn": prepared.summary,
            "poznamka": "Akce NENÍ provedena. Uživatel ji schválí nebo zamítne tlačítkem.",
        }
        return ToolOutcome(
            json.dumps(content, ensure_ascii=False), is_error=False, approval=request
        )


def _err(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}, ensure_ascii=False), is_error=True)
