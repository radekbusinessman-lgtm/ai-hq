"""Simulovaný jazykový model pro testy – nevolá žádné API."""

import asyncio
from typing import Any

from app.llm.base import Block, LLMMessage, LLMResult, TextBlock, ToolSpec, ToolUseBlock


def text(t: str) -> list[Block]:
    return [TextBlock(t)]


def tool(tool_name: str, /, call_id: str = "t1", **args: Any) -> list[Block]:
    return [ToolUseBlock(call_id, tool_name, args)]


class FakeLLM:
    """Vrací odpovědi podle scénáře (seznam odpovědí); poslední se opakuje."""

    def __init__(
        self,
        reply: str = "Dobrý den, jsem připraven.",
        *,
        script: list[list[Block]] | None = None,
        fail: Exception | None = None,
        delay: float = 0.0,
        tokens: tuple[int, int] = (1000, 200),
    ):
        self.script = script or [text(reply)]
        self.fail, self.delay, self.tokens = fail, delay, tokens
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int,
        tools: list[ToolSpec] | None = None,
    ) -> LLMResult:
        self.calls.append(
            {"system": system, "messages": list(messages), "model": model, "tools": tools}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        blocks = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return LLMResult(blocks, model, input_tokens=self.tokens[0], output_tokens=self.tokens[1])
