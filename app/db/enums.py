"""Výčtové typy používané v databázi."""

from enum import StrEnum


class FactStatus(StrEnum):
    """Spolehlivost informace v paměti."""

    VERIFIED = "verified"  # ověřený fakt (má zdroj v dokumentu / od uživatele)
    ASSUMPTION = "assumption"  # pracovní předpoklad
    AI_ESTIMATE = "ai_estimate"  # odhad AI – nikdy se nevydává za fakt
    USER_DECISION = "user_decision"  # rozhodnutí uživatele
    OUTDATED = "outdated"  # historická / neaktuální informace


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"


class RunTrigger(StrEnum):
    DISCORD_MESSAGE = "discord_message"
    SCHEDULE = "schedule"
    MANUAL = "manual"


class PermissionLevel(StrEnum):
    AUTO = "auto"  # úroveň 1 – automaticky povoleno
    APPROVAL = "approval"  # úroveň 2 – vyžaduje schválení
    # Úroveň 3 (pouze ruční provedení) v systému záměrně jako akce neexistuje.


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
