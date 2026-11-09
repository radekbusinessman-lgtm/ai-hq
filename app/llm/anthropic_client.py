"""Adaptér pro Claude API (oficiální SDK Anthropic)."""

from typing import Any

import anthropic

from app.llm.base import (
    Block,
    LLMError,
    LLMMessage,
    LLMResult,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)


def _block_to_api(b: Block) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {
        "type": "tool_result",
        "tool_use_id": b.tool_use_id,
        "content": b.content,
        "is_error": b.is_error,
    }


def _message_to_api(m: LLMMessage) -> dict[str, Any]:
    if isinstance(m.content, str):
        return {"role": m.role, "content": m.content}
    return {"role": m.role, "content": [_block_to_api(b) for b in m.content]}


def _block_from_api(b: Any) -> Block | None:
    kind = getattr(b, "type", None)
    if kind == "text":
        return TextBlock(str(getattr(b, "text", "")))
    if kind == "tool_use":
        return ToolUseBlock(id=b.id, name=b.name, input=dict(b.input or {}))
    return None  # ostatní typy bloků (např. thinking) ignorujeme


class AnthropicClient:
    def __init__(self, api_key: str, timeout_seconds: float = 60.0, sdk: Any | None = None):
        # max_retries: SDK samo zopakuje přechodné chyby (429/5xx) s čekáním
        self._sdk = sdk or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=2
        )

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int,
        tools: list[ToolSpec] | None = None,
    ) -> LLMResult:
        # Prompt caching: instrukce, nástroje a dosavadní konverzace se při dalším kroku
        # téhož běhu čtou z mezipaměti za ~10 % ceny vstupu (max. 4 značky cache_control).
        api_messages = [_message_to_api(m) for m in messages]
        _mark_last_block_for_cache(api_messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": EPHEMERAL}],
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
            kwargs["tools"][-1]["cache_control"] = EPHEMERAL
        try:
            resp = await self._sdk.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e

        blocks = [blk for b in resp.content if (blk := _block_from_api(b)) is not None]
        u = resp.usage
        write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        return LLMResult(
            blocks=blocks,
            model=resp.model,
            input_tokens=int(u.input_tokens) + write + read,  # API uvádí jen nekešovanou část
            output_tokens=int(u.output_tokens),
            stop_reason=getattr(resp, "stop_reason", None),
            cache_write_tokens=write,
            cache_read_tokens=read,
        )


EPHEMERAL = {"type": "ephemeral"}


def _mark_last_block_for_cache(api_messages: list[dict[str, Any]]) -> None:
    """Značka cache na posledním bloku poslední zprávy → celý dosavadní kontext se uloží."""
    if not api_messages:
        return
    last = api_messages[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    if last["content"]:
        last["content"][-1] = {**last["content"][-1], "cache_control": EPHEMERAL}
