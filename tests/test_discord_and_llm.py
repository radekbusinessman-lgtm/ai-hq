"""Testy ověření identity v Discordu, dělení zpráv a adaptéru Claude API."""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from app.config import Settings
from app.discord_bot.bot import is_authorized, split_message
from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMError, LLMMessage, normalize_history

OWNER, GUILD = 111, 222


def cfg() -> Settings:
    return Settings(discord_owner_user_id=OWNER, discord_guild_id=GUILD)


@pytest.mark.parametrize(
    ("author", "guild", "is_bot", "expected"),
    [
        (OWNER, GUILD, False, True),  # vlastník na svém serveru
        (999, GUILD, False, False),  # cizí uživatel
        (OWNER, 333, False, False),  # jiný server
        (OWNER, None, False, False),  # soukromá zpráva (DM)
        (OWNER, GUILD, True, False),  # bot
    ],
)
def test_only_owner_on_own_server_is_authorized(
    author: int, guild: int | None, is_bot: bool, expected: bool
) -> None:
    assert (
        is_authorized(author_id=author, guild_id=guild, is_bot=is_bot, settings=cfg()) is expected
    )


def test_unconfigured_bot_authorizes_nobody() -> None:
    assert not is_authorized(author_id=OWNER, guild_id=GUILD, is_bot=False, settings=Settings())


def test_split_message_respects_discord_limit() -> None:
    text = ("Odstavec. " * 150 + "\n\n") * 3
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace(
        "\n", ""
    )


def test_normalize_history_merges_and_starts_with_user() -> None:
    h = normalize_history(
        [
            LLMMessage("assistant", "a"),
            LLMMessage("user", "u1"),
            LLMMessage("user", "u2"),
            LLMMessage("assistant", "b"),
        ]
    )
    assert [(m.role, m.content) for m in h] == [("user", "u1\n\nu2"), ("assistant", "b")]


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None):
        self.response, self.error = response, error
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


async def test_anthropic_adapter_parses_text_and_usage() -> None:
    resp = SimpleNamespace(
        model="claude-sonnet-5-5",
        content=[
            SimpleNamespace(type="text", text=" Ahoj "),
            SimpleNamespace(type="thinking"),
            SimpleNamespace(
                type="tool_use", id="tu1", name="search_facts", input={"query": "voda"}
            ),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )
    msgs = FakeMessages(resp)
    client = AnthropicClient("test", sdk=SimpleNamespace(messages=msgs))
    r = await client.complete(
        system="S", messages=[LLMMessage("user", "Hi")], model="claude-sonnet-5-5", max_tokens=50
    )
    assert (r.text, r.input_tokens, r.output_tokens) == ("Ahoj", 12, 3)
    assert [(t.name, t.input) for t in r.tool_uses] == [("search_facts", {"query": "voda"})]
    assert msgs.kwargs["system"] == "S" and msgs.kwargs["max_tokens"] == 50
    assert msgs.kwargs["messages"] == [{"role": "user", "content": "Hi"}]


async def test_anthropic_api_error_becomes_llm_error() -> None:
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client = AnthropicClient("test", sdk=SimpleNamespace(messages=FakeMessages(error=err)))
    with pytest.raises(LLMError):
        await client.complete(system="", messages=[], model="m", max_tokens=1)


async def test_anthropic_adapter_serializes_tools_and_blocks() -> None:
    from app.llm.base import TextBlock, ToolResultBlock, ToolSpec, ToolUseBlock

    resp = SimpleNamespace(
        model="m", content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )
    msgs = FakeMessages(resp)
    client = AnthropicClient("test", sdk=SimpleNamespace(messages=msgs))
    await client.complete(
        system="",
        messages=[
            LLMMessage("user", "Q"),
            LLMMessage("assistant", [TextBlock("hledám"), ToolUseBlock("a", "x", {"q": 1})]),
            LLMMessage("user", [ToolResultBlock("a", '{"ok": true}')]),
        ],
        model="m",
        max_tokens=10,
        tools=[ToolSpec("x", "popis", {"type": "object"})],
    )
    sent = msgs.kwargs["messages"]
    assert sent[1]["content"][1] == {"type": "tool_use", "id": "a", "name": "x", "input": {"q": 1}}
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert msgs.kwargs["tools"][0]["name"] == "x"
