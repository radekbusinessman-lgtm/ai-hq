"""Smyčka agenta: model → (nástroje → model)* → finální odpověď.

Limity: maximální počet kroků s nástroji a maximální cena jednoho běhu.
Časový limit celého běhu hlídá volající (ChatService).
"""

import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.llm.base import (
    Block,
    LLMClient,
    LLMMessage,
    TextBlock,
    ToolResultBlock,
    Usage,
    result_cost,
)
from app.tools.registry import ApprovalRequest, ToolContext, ToolRegistry

MSG_STEP_LIMIT = (
    "⚠️ Úloha vyžadovala víc než {n} kroků a byla zastavena kvůli limitu. "
    "Zkuste ji prosím rozdělit na menší části."
)
CONTINUE_PROMPT = "Pokračuj přesně tam, kde jsi skončil. Nic neopakuj."
MSG_COST_LIMIT = "⚠️ Běh překročil nákladový limit {limit} USD a byl zastaven."


@dataclass
class LoopResult:
    text: str
    model: str
    usage: Usage
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    limit_hit: str | None = None  # "steps" | "cost" | None
    approvals: list[ApprovalRequest] = field(default_factory=list)
    stop_reason: str | None = None


async def run_agent(
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    system: str,
    history: list[LLMMessage],
    model: str,
    settings: Settings,
) -> LoopResult:
    messages = list(history)
    usage = Usage()
    tool_log: list[dict[str, Any]] = []
    approvals: list[ApprovalRequest] = []
    used_model = model
    max_steps = settings.max_tool_steps_per_run

    for step in range(max_steps + 1):
        result = await llm.complete(
            system=system,
            messages=messages,
            model=model,
            max_tokens=settings.llm_max_output_tokens,
            tools=registry.specs(),
        )
        used_model = result.model
        usage.input_tokens += result.input_tokens
        usage.output_tokens += result.output_tokens
        usage.cost_usd += result_cost(settings.llm_prices_per_mtok, result)

        if not result.tool_uses:
            text = result.raw_text
            # Odpověď uťatá limitem délky → model pokračuje tam, kde skončil (max N×).
            for _ in range(settings.llm_max_continuations):
                if result.stop_reason != "max_tokens":
                    break
                if text:
                    messages.append(LLMMessage("assistant", [TextBlock(text)]))
                    messages.append(LLMMessage("user", CONTINUE_PROMPT))
                    max_tokens = settings.llm_max_output_tokens
                else:  # limit spotřeboval vnitřní rozmysl – zkusit znovu s větším limitem
                    max_tokens = settings.llm_max_output_tokens * 2
                result = await llm.complete(
                    system=system,
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    tools=registry.specs(),
                )
                usage.input_tokens += result.input_tokens
                usage.output_tokens += result.output_tokens
                usage.cost_usd += result_cost(settings.llm_prices_per_mtok, result)
                text = (text + result.raw_text) if text else result.raw_text
                if messages[-1].content == CONTINUE_PROMPT:
                    messages = messages[:-2]
            return LoopResult(
                text.strip(),
                used_model,
                usage,
                tool_log,
                approvals=approvals,
                stop_reason=result.stop_reason,
            )

        if usage.cost_usd >= settings.max_run_cost_usd:
            return LoopResult(
                MSG_COST_LIMIT.format(limit=settings.max_run_cost_usd),
                used_model,
                usage,
                tool_log,
                limit_hit="cost",
                approvals=approvals,
            )
        if step == max_steps:
            break

        messages.append(LLMMessage("assistant", list(result.blocks)))
        results: list[Block] = []
        for call in result.tool_uses:
            started = time.monotonic()
            outcome = await registry.execute(ctx, call.name, call.input)
            tool_log.append(
                {
                    "step": step + 1,
                    "tool": call.name,
                    "input": call.input,
                    "is_error": outcome.is_error,
                    "result_preview": (
                        "[obsah se neukládá]"
                        if (t := registry.get(call.name)) is not None and t.sensitive_output
                        else outcome.content[:500]
                    ),
                    "ms": int((time.monotonic() - started) * 1000),
                }
            )
            # náklady modelů volaných uvnitř nástroje (např. předtřídění pošty Haiku)
            while ctx.extra_usage:
                sub = ctx.extra_usage.pop()
                usage.input_tokens += sub.input_tokens
                usage.output_tokens += sub.output_tokens
                usage.cost_usd += result_cost(settings.llm_prices_per_mtok, sub)
            if outcome.approval is not None:
                approvals.append(outcome.approval)
            results.append(ToolResultBlock(call.id, outcome.content, outcome.is_error))
        messages.append(LLMMessage("user", results))

    return LoopResult(
        MSG_STEP_LIMIT.format(n=max_steps),
        used_model,
        usage,
        tool_log,
        limit_hit="steps",
        approvals=approvals,
    )
