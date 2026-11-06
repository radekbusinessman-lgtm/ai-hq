"""Datový model firemní paměti AI HQ.

Oddělení firem: všechny projektové a obchodní záznamy nesou company_id.
Dotazy na data vždy filtrují podle firmy (repository vrstva).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, RecordMixin
from app.db.enums import (
    ApprovalStatus,
    FactStatus,
    MessageRole,
    PermissionLevel,
    RunStatus,
    RunTrigger,
    TaskStatus,
)

EMBEDDING_DIM = 1024  # Voyage AI (voyage-3.x) výchozí dimenze


def enum_col(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Enum uložený jako text s CHECK omezením (snadné migrace)."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=30,
        values_callable=lambda e: [m.value for m in e],
    )


def company_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )


def project_fk() -> Mapped[uuid.UUID | None]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )


# ---------------------------------------------------------------- kmenová data


class Company(RecordMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    projects: Mapped[list["Project"]] = relationship(back_populates="company")


class Project(RecordMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("company_id", "slug", name="company_slug"),)

    company_id: Mapped[uuid.UUID] = company_fk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String(120)), server_default="{}")
    status: Mapped[str] = mapped_column(String(40), server_default="active")
    description: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship(back_populates="projects")


class Contact(RecordMixin, Base):
    __tablename__ = "contacts"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(200))  # např. projektant, úřad, správce sítě
    organization: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------------------- dokumenty


class Document(RecordMixin, Base):
    __tablename__ = "documents"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    title: Mapped[str] = mapped_column(String(300))
    doc_type: Mapped[str | None] = mapped_column(String(80))  # studie, rozpočet, vyjádření…

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_no"
    )


class DocumentVersion(RecordMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="doc_version_no"),
        UniqueConstraint("document_id", "sha256", name="doc_sha256"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(300))
    storage_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))  # chrání proti duplicitnímu nahrání

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentChunk(RecordMixin, Base):
    """Úsek textu dokumentu – jednotka vyhledávání a citace."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index", name="version_chunk"),
        Index("ix_document_chunks_search", "search_vector", postgresql_using="gin"),
    )

    company_id: Mapped[uuid.UUID] = company_fk()
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)  # pro citaci "str. X"
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )


# --------------------------------------------------------------------- paměť


class Fact(RecordMixin, Base):
    """Jedno tvrzení o firmě/projektu s rozlišením spolehlivosti a zdrojem."""

    __tablename__ = "facts"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    topic: Mapped[str | None] = mapped_column(String(120))  # voda, kanalizace, povolení, rozpočet…
    statement: Mapped[str] = mapped_column(Text)
    status: Mapped[FactStatus] = mapped_column(enum_col(FactStatus, "fact_status"))
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="SET NULL")
    )


class Task(RecordMixin, Base):
    __tablename__ = "tasks"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        enum_col(TaskStatus, "task_status"), server_default=TaskStatus.OPEN.value
    )
    priority: Mapped[int] = mapped_column(Integer, server_default="2")  # 1 vysoká – 3 nízká
    due_date: Mapped[date | None] = mapped_column(Date)
    assignee_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Decision(RecordMixin, Base):
    __tablename__ = "decisions"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    title: Mapped[str] = mapped_column(String(300))
    decision: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(120))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(RecordMixin, Base):
    """Termíny, lhůty, schůzky a milníky."""

    __tablename__ = "events"

    company_id: Mapped[uuid.UUID] = company_fk()
    project_id: Mapped[uuid.UUID | None] = project_fk()
    event_type: Mapped[str] = mapped_column(String(60))  # deadline, meeting, milestone…
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    occurs_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# --------------------------------------------------------------- konverzace


class Conversation(RecordMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="channel_external"),)

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = project_fk()
    channel: Mapped[str] = mapped_column(String(40))  # discord
    external_id: Mapped[str] = mapped_column(String(100))  # ID kanálu / vlákna


class Message(RecordMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    # Pořadí zpráv – spolehlivější než čas (více zpráv v jedné transakci má stejný čas)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    role: Mapped[MessageRole] = mapped_column(enum_col(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )


# ------------------------------------------------------- provoz a kontrola


class AgentRun(RecordMixin, Base):
    """Každý běh agenta (odpověď, briefing, automatizace) včetně nákladů a chyb."""

    __tablename__ = "agent_runs"

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )
    trigger: Mapped[RunTrigger] = mapped_column(enum_col(RunTrigger, "run_trigger"))
    job_name: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[RunStatus] = mapped_column(
        enum_col(RunStatus, "run_status"), server_default=RunStatus.RUNNING.value
    )
    model: Mapped[str | None] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), server_default="0")
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )


class Approval(RecordMixin, Base):
    """Žádost o schválení konkrétní akce – schvaluje se přesný obsah (hash payloadu)."""

    __tablename__ = "approvals"

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(String(80))  # např. send_email
    permission_level: Mapped[PermissionLevel] = mapped_column(
        enum_col(PermissionLevel, "permission_level")
    )
    summary: Mapped[str] = mapped_column(Text)  # lidsky čitelný popis pro Discord
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_col(ApprovalStatus, "approval_status"), server_default=ApprovalStatus.PENDING.value
    )
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AuditLog(Base):
    """Auditní stopa. Pouze přidávání – UPDATE a DELETE blokuje databázový trigger."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String(120))  # user:<id> | agent:<run_id> | system
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class SystemState(Base):
    """Globální přepínače: vypínač agentů, rozpočet apod."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(120), server_default="system")
