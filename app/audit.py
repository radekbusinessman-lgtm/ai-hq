"""Zápis do auditního logu."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    **details: Any,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            company_id=company_id,
            details=details,
        )
    )
