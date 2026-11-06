"""Rozhraní k jazykovému modelu – nezávislé na dodavateli (Claude, později OpenAI).

Zprávy se skládají z bloků: text, požadavek na nástroj (tool_use) a výsledek nástroje.
Adaptér konkrétního dodavatele je převádí do jeho formátu.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str | list[Block]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LLMResult:
    blocks: list[Block]
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]


class LLMError(Exception):
    """Chyba při volání modelu (síť, limit, odmítnutí API…)."""


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int,
        tools: list[ToolSpec] = ...,
    ) -> LLMResult: ...


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))


def estimate_cost(
    prices: dict[str, tuple[Decimal, Decimal]], model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    """Cena volání v USD. Neznámý model se počítá nejdražší známou sazbou (bezpečná strana)."""
    if model in prices:
        p_in, p_out = prices[model]
    else:
        p_in = max((p[0] for p in prices.values()), default=Decimal("15"))
        p_out = max((p[1] for p in prices.values()), default=Decimal("75"))
    million = Decimal(1_000_000)
    return (Decimal(input_tokens) * p_in + Decimal(output_tokens) * p_out) / million


def normalize_history(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Sloučí po sobě jdoucí textové zprávy stejné role; konverzace začne uživatelem."""
    merged: list[LLMMessage] = []
    for m in messages:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.role == m.role
            and isinstance(prev.content, str)
            and isinstance(m.content, str)
        ):
            merged[-1] = LLMMessage(m.role, prev.content + "\n\n" + m.content)
        else:
            merged.append(m)
    while merged and merged[0].role != "user":
        merged.pop(0)
    return merged
