"""Registr nástrojů agenta.

Agent smí používat POUZE nástroje registrované zde. Každý nástroj má pevně danou úroveň
oprávnění v kódu – model ji nemůže změnit. V této verzi se automaticky provádějí jen
nástroje úrovně AUTO (úroveň 1); nástroje vyžadující schválení přijdou s modulem schvalování.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import PermissionLevel
from app.llm.base import ToolSpec


@dataclass(frozen=True)
class ToolContext:
    sessions: async_sessionmaker[AsyncSession]
    run_id: uuid.UUID
    company_id: uuid.UUID | None  # firma konverzace (pokud je kanál přiřazen projektu)
    project_id: uuid.UUID | None  # projekt konverzace
    user_message_id: uuid.UUID | None  # zpráva, která běh vyvolala (zdroj pro fakta)
    actor: str  # kdo běh vyvolal, např. user:discord:123


class ToolInputError(Exception):
    """Chybný vstup od modelu – vrátí se modelu jako chyba nástroje, ne jako pád běhu."""


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    level: PermissionLevel
    handler: Handler

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # JSON pro model
    is_error: bool


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return _err(f"Nástroj '{name}' neexistuje nebo není povolen.")
        if tool.level != PermissionLevel.AUTO:
            return _err(f"Nástroj '{name}' vyžaduje schválení – zatím není dostupný.")
        try:
            result = await tool.handler(ctx, args)
        except ToolInputError as e:
            return _err(str(e))
        return ToolOutcome(json.dumps(result, ensure_ascii=False, default=str), is_error=False)


def _err(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}, ensure_ascii=False), is_error=True)
