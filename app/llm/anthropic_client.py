"""Adaptér pro Claude API (oficiální SDK Anthropic)."""

from typing import Any

import anthropic

from app.llm.base import LLMError, LLMMessage, LLMResult


class AnthropicClient:
    def __init__(self, api_key: str, timeout_seconds: float = 60.0, sdk: Any | None = None):
        # max_retries: SDK samo zopakuje přechodné chyby (429/5xx) s čekáním
        self._sdk = sdk or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=2
        )

    async def complete(
        self, *, system: str, messages: list[LLMMessage], model: str, max_tokens: int
    ) -> LLMResult:
        payload: list[Any] = [{"role": m.role, "content": m.content} for m in messages]
        try:
            resp = await self._sdk.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=payload,
            )
        except anthropic.APIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e

        text = "".join(
            str(getattr(b, "text", "")) for b in resp.content if getattr(b, "type", None) == "text"
        )
        return LLMResult(
            text=text.strip(),
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
