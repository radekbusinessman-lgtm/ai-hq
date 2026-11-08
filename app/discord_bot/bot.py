"""Discord bot – pouze komunikační vrstva. Veškerá logika je v app.agent.

Bezpečnost: bot reaguje výhradně na zprávy a příkazy od vlastníka (DISCORD_OWNER_USER_ID)
na jeho serveru (DISCORD_GUILD_ID). Soukromé zprávy (DM) a ostatní uživatele ignoruje.

Příkazy (slash): /stop, /start, /stav, /briefing.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from app.agent.approvals import ApprovalService
from app.agent.briefing import briefing_message
from app.agent.chat import ChatService, InboundMessage
from app.agent.state import (
    agents_enabled,
    run_stats_24h,
    set_agents_enabled,
    spent_this_month,
)
from app.audit import audit
from app.config import Settings
from app.db.enums import ApprovalStatus
from app.tools.registry import ApprovalRequest

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
APP_VERSION = "0.5.0"
APPROVAL_PREFIX = "aihq:approval:"  # custom_id tlačítek: aihq:approval:<approve|reject>:<uuid>

DECISION_LABEL = {
    ApprovalStatus.EXECUTED: "✅ **Schváleno a provedeno**",
    ApprovalStatus.REJECTED: "❌ **Zamítnuto**",
    ApprovalStatus.EXPIRED: "⌛ **Vypršelo** – nic se neprovedlo",
    ApprovalStatus.FAILED: "⚠️ **Neprovedeno**",
}


def approval_view(approval_id: uuid.UUID) -> discord.ui.View:
    """Tlačítka bez vlastních callbacků – kliknutí zpracuje on_interaction podle custom_id,
    takže fungují i po restartu serveru."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Schválit",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"{APPROVAL_PREFIX}approve:{approval_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Zamítnout",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"{APPROVAL_PREFIX}reject:{approval_id}",
        )
    )
    return view


def parse_approval_custom_id(custom_id: str) -> tuple[bool, uuid.UUID] | None:
    if not custom_id.startswith(APPROVAL_PREFIX):
        return None
    try:
        action, raw_id = custom_id[len(APPROVAL_PREFIX) :].split(":", 1)
        if action not in ("approve", "reject"):
            return None
        return action == "approve", uuid.UUID(raw_id)
    except ValueError:
        return None


def approval_card(req: ApprovalRequest, tz: str) -> str:
    until = req.expires_at.astimezone(ZoneInfo(tz)).strftime("%-d. %-m. %H:%M")
    return f"🛂 **Ke schválení** [{req.ref}] · platí do {until}\n{req.summary}"


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
        self.approvals = ApprovalService(chat.sessions, chat.registry)
        self.tree = app_commands.CommandTree(self)
        self.started_at = time.monotonic()
        self.last_ready_at: float | None = None
        self._announced = False
        chat.notify = self.post_event  # chyby a upozornění služby chodí do #systemove-udalosti
        self._register_commands()

    # ------------------------------------------------------------- stav a kanály

    @property
    def connected(self) -> bool:
        return self.is_ready() and not self.is_closed()

    def _guild(self) -> discord.Guild | None:
        gid = self.settings.discord_guild_id
        return self.get_guild(gid) if gid else None

    def channel_by_name(self, name: str) -> discord.TextChannel | None:
        guild = self._guild()
        if guild is None:
            return None
        return discord.utils.get(guild.text_channels, name=name)

    async def post(self, channel_name: str, text: str) -> bool:
        channel = self.channel_by_name(channel_name)
        if channel is None:
            log.warning("Kanál #%s nenalezen – zpráva neodeslána", channel_name)
            return False
        try:
            for part in split_message(text):
                await channel.send(part)
        except discord.HTTPException as e:
            # typicky Forbidden: bot nemá v kanálu oprávnění „Zobrazit kanál“ / „Odesílat zprávy“
            log.error(
                "Nelze psát do #%s (%s %s) – zkontrolujte oprávnění bota v kanálu",
                channel_name,
                e.status,
                e.text,
            )
            return False
        return True

    async def post_event(self, text: str) -> None:
        await self.post(self.settings.discord_events_channel, text)

    # ------------------------------------------------------------- události

    async def setup_hook(self) -> None:
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Slash příkazy synchronizovány: %s", [c.name for c in synced])

    async def on_ready(self) -> None:
        self.last_ready_at = time.monotonic()
        log.info("Discord bot připojen jako %s", self.user)
        if not self._announced:
            self._announced = True
            now = datetime.now(ZoneInfo(self.settings.timezone)).strftime("%-d. %-m. %Y %H:%M")
            await self.post_event(f"🟢 AI HQ {APP_VERSION} spuštěn ({now}).")

    async def on_resumed(self) -> None:
        self.last_ready_at = time.monotonic()

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
            return  # přílohy bez textu přijdou na řadu později

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
        for req in reply.approvals:
            view = approval_view(req.id)
            await message.channel.send(approval_card(req, self.settings.timezone), view=view)
            view.stop()  # klik zpracuje on_interaction (i po restartu)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data: dict[str, Any] = dict(interaction.data or {})
        custom_id = str(data.get("custom_id", ""))
        parsed = parse_approval_custom_id(custom_id)
        if parsed is None:
            return
        approve, approval_id = parsed
        if not self._owner_only(interaction):
            await self._log_rejected(interaction.user.id, interaction.guild_id)
            await interaction.response.send_message("Nemáte oprávnění.", ephemeral=True)
            return
        await interaction.response.defer()
        decision = await self.approvals.decide(
            approval_id, approve=approve, actor=f"user:discord:{interaction.user.id}"
        )
        label = DECISION_LABEL.get(decision.status, f"ℹ️ {decision.status.value}")
        original = interaction.message.content if interaction.message else ""
        detail = "" if decision.status == ApprovalStatus.REJECTED else f" – {decision.message}"
        if decision.status == ApprovalStatus.EXECUTED and decision.result:
            new_ref = decision.result.get("novy_ref")
            detail = f" (nový fakt [{new_ref}])" if new_ref else ""
        try:
            await interaction.edit_original_response(
                content=f"{original}\n\n{label}{detail}"[:DISCORD_LIMIT], view=None
            )
        except discord.HTTPException:
            await interaction.followup.send(f"{label}{detail}", ephemeral=True)

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

    # ------------------------------------------------------------- briefing

    async def run_briefing(self, *, manual: bool = False) -> str:
        """Vytvoří briefing a pošle ho do kanálu briefingu. Vrací krátký popis výsledku."""
        channel = self.channel_by_name(self.settings.discord_briefing_channel)
        if channel is None:
            return f"Kanál #{self.settings.discord_briefing_channel} nenalezen."
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        msg = briefing_message(
            day=today,
            channel_id=str(channel.id),
            channel_name=channel.name,
            model=self.settings.llm_model_briefing,
            manual_key=f"briefing-manual:{uuid.uuid4()}" if manual else None,
        )
        reply = await self.chat.handle(msg)
        if reply is None:
            return "Dnešní briefing už proběhl."
        header = "☀️ **Ranní briefing**" if not manual else "📋 **Briefing**"
        await self.post(channel.name, f"{header}\n\n{reply.text}")
        return "Briefing odeslán." if reply.processed else reply.text

    # ------------------------------------------------------------- příkazy

    def _owner_only(self, interaction: discord.Interaction) -> bool:
        return is_authorized(
            author_id=interaction.user.id,
            guild_id=interaction.guild_id,
            is_bot=interaction.user.bot,
            settings=self.settings,
        )

    def _register_commands(self) -> None:
        bot = self

        async def deny(interaction: discord.Interaction) -> None:
            await bot._log_rejected(interaction.user.id, interaction.guild_id)
            await interaction.response.send_message("Nemáte oprávnění.", ephemeral=True)

        @self.tree.command(name="stop", description="Okamžitě zastaví agenty a automatizace")
        async def stop(interaction: discord.Interaction) -> None:
            if not bot._owner_only(interaction):
                return await deny(interaction)
            async with bot.chat.sessions() as s, s.begin():
                await set_agents_enabled(s, False, f"user:discord:{interaction.user.id}")
            await interaction.response.send_message(
                "⏸️ Agenti zastaveni. Zprávy se ukládají, ale AI je nezpracuje. Zapnutí: /start"
            )
            await bot.post_event("⏸️ Agenti zastaveni příkazem /stop.")

        @self.tree.command(name="start", description="Znovu zapne agenty")
        async def start(interaction: discord.Interaction) -> None:
            if not bot._owner_only(interaction):
                return await deny(interaction)
            async with bot.chat.sessions() as s, s.begin():
                await set_agents_enabled(s, True, f"user:discord:{interaction.user.id}")
            await interaction.response.send_message("▶️ Agenti znovu běží.")
            await bot.post_event("▶️ Agenti zapnuti příkazem /start.")

        @self.tree.command(name="stav", description="Stav systému, útrata a běhy za 24 h")
        async def stav(interaction: discord.Interaction) -> None:
            if not bot._owner_only(interaction):
                return await deny(interaction)
            async with bot.chat.sessions() as s:
                enabled = await agents_enabled(s)
                spent = await spent_this_month(s)
                stats = await run_stats_24h(s)
            uptime_h = (time.monotonic() - bot.started_at) / 3600
            budget = bot.settings.monthly_budget_usd
            runs = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())) or "žádné"
            expiry = bot.settings.anthropic_key_expires_on
            expiry_txt = expiry.isoformat() if expiry else "nenastaveno"
            text = (
                f"**AI HQ {APP_VERSION}**\n"
                f"Agenti: {'▶️ běží' if enabled else '⏸️ zastaveni'}\n"
                f"Útrata tento měsíc: {spent:.2f} / {budget} USD\n"
                f"Běhy za 24 h: {runs}\n"
                f"Bot běží: {uptime_h:.1f} h · odezva Discordu: {bot.latency * 1000:.0f} ms\n"
                f"Platnost API klíče do: {expiry_txt}\n"
                f"Briefing: {bot.settings.briefing_time or 'vypnut'} denně"
            )
            await interaction.response.send_message(text, ephemeral=True)

        @self.tree.command(name="briefing", description="Vytvoří projektový briefing hned")
        async def briefing(interaction: discord.Interaction) -> None:
            if not bot._owner_only(interaction):
                return await deny(interaction)
            await interaction.response.send_message("📋 Připravuji briefing…", ephemeral=True)
            result = await bot.run_briefing(manual=True)
            await interaction.followup.send(result, ephemeral=True)


def utcnow() -> datetime:
    return datetime.now(UTC)
