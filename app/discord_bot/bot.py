"""Discord bot – pouze komunikační vrstva. Veškerá logika je v app.agent.chat.

Bezpečnost: bot reaguje výhradně na zprávy od vlastníka (DISCORD_OWNER_USER_ID)
na jeho serveru (DISCORD_GUILD_ID). Soukromé zprávy (DM) a ostatní uživatele ignoruje.
"""

import logging

import discord

from app.agent.chat import ChatService, InboundMessage
from app.audit import audit
from app.config import Settings

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000


def is_authorized(
    *, author_id: int, guild_id: int | None, is_bot: bool, settings: Settings
) -> bool:
    if is_bot or settings.discord_owner_user_id is None or settings.discord_guild_id is None:
        return False
    return author_id == settings.discord_owner_user_id and guild_id == settings.discord_guild_id


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Rozdělí dlouhou odpověď na části do limitu Discordu, přednostně na koncích odstavců."""
    if not text:
        return ["(prázdná odpověď)"]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


class AIHQBot(discord.Client):
    def __init__(self, chat: ChatService, settings: Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # nutné pro čtení textu zpráv
        super().__init__(intents=intents)
        self.chat = chat
        self.settings = settings

    async def on_ready(self) -> None:
        log.info("Discord bot připojen jako %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        guild_id = message.guild.id if message.guild else None
        if not is_authorized(
            author_id=message.author.id,
            guild_id=guild_id,
            is_bot=message.author.bot,
            settings=self.settings,
        ):
            if not message.author.bot:
                await self._log_rejected(message.author.id, guild_id)
            return
        if not message.content.strip():
            return  # přílohy bez textu přijdou na řadu v dalších dnech

        inbound = InboundMessage(
            channel="discord",
            channel_external_id=str(message.channel.id),
            channel_name=getattr(message.channel, "name", None),
            message_external_id=str(message.id),
            author_external_id=str(message.author.id),
            content=message.content,
        )
        async with message.channel.typing():
            reply = await self.chat.handle(inbound)
        if reply is None:
            return
        for i, part in enumerate(split_message(reply.text)):
            if i == 0:
                await message.reply(part, mention_author=False)
            else:
                await message.channel.send(part)

    async def _log_rejected(self, author_id: int, guild_id: int | None) -> None:
        try:
            async with self.chat.sessions() as s, s.begin():
                audit(
                    s,
                    actor=f"discord:{author_id}",
                    action="auth.rejected",
                    guild_id=str(guild_id) if guild_id else None,
                )
        except Exception:  # noqa: BLE001
            log.exception("Nepodařilo se zapsat odmítnutý přístup do auditu")
