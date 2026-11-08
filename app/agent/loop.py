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
    ToolResultBlock,
    Usage,
    estimate_cost,
)
from app.tools.registry import ApprovalRequest, ToolContext, ToolRegistry

MSG_STEP_LIMIT = (
    "⚠️ Úloha vyžadovala víc než {n} kroků a byla zastavena kvůli limitu. "
    "Zkuste ji prosím rozdělit na menší části."
)
MSG_COST_LIMIT = "⚠️ Běh překročil nákladový limit {limit} USD a byl zastaven."


@dataclass
class LoopResult:
    text: str
    model: str
    usage: Usage
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    limit_hit: str | None = None  # "steps" | "cost" | None
    approvals: list[ApprovalRequest] = field(default_factory=list)


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
        usage.cost_usd += estimate_cost(
            settings.llm_prices_per_mtok, result.model, result.input_tokens, result.output_tokens
        )

        if not result.tool_uses:
            return LoopResult(result.text, used_model, usage, tool_log, approvals=approvals)

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
                    "result_preview": outcome.content[:500],
                    "ms": int((time.monotonic() - started) * 1000),
                }
            )
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
