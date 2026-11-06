"""Základ databázového modelu.

Každý záznam má:
- id (UUID),
- source – původ informace (např. "user:discord", "document:<uuid>", "agent_run:<uuid>"),
- created_at / updated_at.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SourceMixin:
    source: Mapped[str] = mapped_column(String(200), server_default="system", nullable=False)


class RecordMixin(IdMixin, TimestampMixin, SourceMixin):
    """Standardní záznam: id + původ + časy."""
