"""Rozhraní k jazykovému modelu – nezávislé na dodavateli (Claude, později OpenAI)."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    """Chyba při volání modelu (síť, limit, odmítnutí API…)."""


class LLMClient(Protocol):
    async def complete(
        self, *, system: str, messages: list[LLMMessage], model: str, max_tokens: int
    ) -> LLMResult: ...


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
    """Sloučí po sobě jdoucí zprávy stejné role a zajistí, že konverzace začíná uživatelem."""
    merged: list[LLMMessage] = []
    for m in messages:
        if merged and merged[-1].role == m.role:
            merged[-1] = LLMMessage(m.role, merged[-1].content + "\n\n" + m.content)
        else:
            merged.append(m)
    while merged and merged[0].role != "user":
        merged.pop(0)
    return merged
