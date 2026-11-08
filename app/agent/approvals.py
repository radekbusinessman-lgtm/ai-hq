"""Rozhodnutí o žádostech o schválení a provedení schválené akce.

Bezpečnostní pravidla:
- rozhodnout smí jen vlastník (ověřuje volající – Discord vrstva),
- žádost musí být ve stavu „pending“ a nevypršelá; o jedné žádosti lze rozhodnout jen jednou
  (řádek se zamyká – dvojklik ani souběh akci neprovede dvakrát),
- před provedením se ověří otisk SHA-256 obsahu – provede se přesně to, co bylo zobrazeno.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import audit
from app.db.enums import ApprovalStatus
from app.db.models import Approval
from app.tools.registry import ToolContext, ToolInputError, ToolRegistry, payload_hash

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    status: ApprovalStatus
    message: str
    result: dict[str, Any] | None = None


class ApprovalService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], registry: ToolRegistry):
        self.sessions = sessions
        self.registry = registry

    async def decide(self, approval_id: uuid.UUID, *, approve: bool, actor: str) -> Decision:
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            a = await s.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
            if a is None:
                return Decision(ApprovalStatus.FAILED, "Žádost neexistuje.")
            if a.status != ApprovalStatus.PENDING:
                return Decision(a.status, f"O žádosti už bylo rozhodnuto ({a.status.value}).")
            if a.expires_at <= now:
                a.status = ApprovalStatus.EXPIRED
                audit(
                    s,
                    actor="system",
                    action="approval.expired",
                    entity_type="approval",
                    entity_id=a.id,
                    company_id=a.company_id,
                )
                return Decision(ApprovalStatus.EXPIRED, "Žádost vypršela – nic se neprovedlo.")
            a.decided_by = actor
            a.decided_at = now
            if not approve:
                a.status = ApprovalStatus.REJECTED
                audit(
                    s,
                    actor=actor,
                    action="approval.rejected",
                    entity_type="approval",
                    entity_id=a.id,
                    company_id=a.company_id,
                )
                return Decision(ApprovalStatus.REJECTED, "Zamítnuto – nic se neprovedlo.")
            if payload_hash(a.payload) != a.payload_sha256:
                a.status = ApprovalStatus.FAILED
                a.result = {"error": "otisk obsahu nesouhlasí"}
                audit(
                    s,
                    actor="system",
                    action="approval.tampered",
                    entity_type="approval",
                    entity_id=a.id,
                    company_id=a.company_id,
                )
                return Decision(
                    ApprovalStatus.FAILED,
                    "Obsah žádosti byl změněn – z bezpečnostních důvodů neprovedeno.",
                )
            a.status = ApprovalStatus.APPROVED
            audit(
                s,
                actor=actor,
                action="approval.approved",
                entity_type="approval",
                entity_id=a.id,
                company_id=a.company_id,
            )
            payload = dict(a.payload)
            run_id = a.agent_run_id or uuid.uuid4()

        return await self._execute(approval_id, payload, run_id, actor)

    async def _execute(
        self, approval_id: uuid.UUID, payload: dict[str, Any], run_id: uuid.UUID, actor: str
    ) -> Decision:
        tool = self.registry.get(str(payload.get("tool")))
        ctxd = payload.get("context") or {}
        status = ApprovalStatus.FAILED
        result: dict[str, Any]
        if tool is None:
            result = {"error": f"Nástroj {payload.get('tool')} už není dostupný."}
        else:
            ctx = ToolContext(
                sessions=self.sessions,
                run_id=run_id,
                company_id=_uuid(ctxd.get("company_id")),
                project_id=_uuid(ctxd.get("project_id")),
                user_message_id=_uuid(ctxd.get("user_message_id")),
                actor=f"{actor} (schváleno)",
                allow_approval_requests=False,
            )
            try:
                result = await tool.handler(ctx, dict(payload.get("args") or {}))
                status = ApprovalStatus.EXECUTED
            except ToolInputError as e:
                result = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 – výsledek musí být vždy zaznamenán
                log.exception("Provedení schválené akce selhalo")
                result = {"error": f"{type(e).__name__}: {e}"}

        async with self.sessions() as s, s.begin():
            a = await s.get_one(Approval, approval_id)
            a.status = status
            a.executed_at = datetime.now(UTC) if status == ApprovalStatus.EXECUTED else None
            a.result = result
            audit(
                s,
                actor=actor,
                action=f"approval.{status.value}",
                entity_type="approval",
                entity_id=a.id,
                company_id=a.company_id,
            )
        if status == ApprovalStatus.EXECUTED:
            return Decision(status, "Schváleno a provedeno.", result)
        return Decision(status, f"Schváleno, ale provedení selhalo: {result.get('error')}", result)


def _uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None
