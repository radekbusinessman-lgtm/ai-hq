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
    input_tokens: int  # celkový vstup včetně části z mezipaměti
    output_tokens: int
    stop_reason: str | None = None
    cache_write_tokens: int = 0  # vstup zapsaný do mezipaměti (1,25× cena vstupu)
    cache_read_tokens: int = 0  # vstup přečtený z mezipaměti (0,1× cena vstupu)

    @property
    def raw_text(self) -> str:
        """Text bez ořezání mezer – pro spojování pokračujících částí odpovědi."""
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

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


CACHE_WRITE_FACTOR = Decimal("1.25")
CACHE_READ_FACTOR = Decimal("0.1")


def estimate_cost(
    prices: dict[str, tuple[Decimal, Decimal]],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Decimal:
    """Cena volání v USD. Neznámý model se počítá nejdražší známou sazbou (bezpečná strana).
    input_tokens = celý vstup; z něj cache_write/cache_read se účtují upravenou sazbou."""
    if model in prices:
        p_in, p_out = prices[model]
    else:
        p_in = max((p[0] for p in prices.values()), default=Decimal("15"))
        p_out = max((p[1] for p in prices.values()), default=Decimal("75"))
    million = Decimal(1_000_000)
    uncached = max(input_tokens - cache_write_tokens - cache_read_tokens, 0)
    cost_in = (
        Decimal(uncached)
        + Decimal(cache_write_tokens) * CACHE_WRITE_FACTOR
        + Decimal(cache_read_tokens) * CACHE_READ_FACTOR
    ) * p_in
    return (cost_in + Decimal(output_tokens) * p_out) / million


def result_cost(prices: dict[str, tuple[Decimal, Decimal]], r: "LLMResult") -> Decimal:
    return estimate_cost(
        prices,
        r.model,
        r.input_tokens,
        r.output_tokens,
        r.cache_write_tokens,
        r.cache_read_tokens,
    )


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
