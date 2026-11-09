"""Zpracování příchozí zprávy: kontroly → uložení → AI → uložení odpovědi → evidence běhu.

Služba je nezávislá na Discordu (Discord je jen komunikační vrstva), takže ji lze
otestovat i bez připojení k Discordu a později použít i z webového dashboardu.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import run_agent
from app.agent.prompts import build_system_prompt
from app.agent.state import agents_enabled, claim_once, month_start, spent_this_month
from app.audit import audit
from app.config import Settings
from app.db.enums import MessageRole, RunStatus, RunTrigger
from app.db.models import AgentRun, Conversation, Message, Project
from app.llm.base import LLMClient, LLMMessage, normalize_history
from app.tools.memory import MEMORY_TOOLS
from app.tools.projects import PROJECT_TOOLS
from app.tools.registry import ApprovalRequest, Tool, ToolContext, ToolRegistry
from app.tools.tasks import TASK_TOOLS

log = logging.getLogger(__name__)


def default_tools(settings: Settings) -> list[Tool]:
    """Nástroje agenta. Pošta jen tehdy, je-li nastavená (iCloud e-mail + heslo pro aplikaci)."""
    tools = MEMORY_TOOLS + TASK_TOOLS + PROJECT_TOOLS
    if settings.icloud_email and settings.icloud_app_password:
        from app.integrations.mail import ImapMailBackend
        from app.tools.mail import make_mail_tools

        backend = ImapMailBackend(
            settings.icloud_email, settings.icloud_app_password.get_secret_value()
        )
        owners = [settings.icloud_email] + [
            a.strip() for a in settings.icloud_extra_addresses.split(",") if a.strip()
        ]
        tools = tools + make_mail_tools(backend, owner_emails=owners, timezone=settings.timezone)
    return tools


Notifier = Callable[[str], Awaitable[None]]

MSG_STOPPED = "⏸️ Agenti jsou zastavení (vypínač). Zprávu jsem uložil, ale nezpracoval."
MSG_BUDGET = (
    "💸 Měsíční rozpočet na AI ({spent} USD z {budget} USD) je vyčerpaný. "
    "Zprávu jsem uložil, ale nezpracoval."
)
MSG_ERROR = "⚠️ Při zpracování nastala chyba (zaznamenáno, běh {run_id}). Zkuste to prosím znovu."
MSG_EMPTY = (
    "⚠️ Model nevrátil žádný text (důvod: {reason}, běh {run_id}). Nic nebylo provedeno "
    "ani odesláno – zkuste to prosím znovu, případně zúžit zadání."
)
MSG_CANCELLED = "⏹️ Zpracování bylo zastaveno příkazem /stop (běh {run_id})."
MSG_TIMEOUT = "⏱️ Odpověď trvala déle než {seconds} s a byla přerušena (běh {run_id})."


@dataclass(frozen=True)
class InboundMessage:
    channel: str  # "discord"
    channel_external_id: str  # ID kanálu
    channel_name: str | None  # název kanálu (pro rozpoznání projektu)
    message_external_id: str  # ID zprávy – ochrana proti dvojímu zpracování
    author_external_id: str
    content: str
    trigger: RunTrigger = RunTrigger.DISCORD_MESSAGE
    model: str | None = None  # None = výchozí model
    actor: str | None = None  # None = user:<channel>:<author>


@dataclass(frozen=True)
class ChatReply:
    text: str
    run_id: uuid.UUID | None
    processed: bool  # False = nezpracováno AI (vypínač, rozpočet)
    approvals: tuple[ApprovalRequest, ...] = ()  # akce čekající na schválení tlačítkem


class ChatService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        settings: Settings,
        registry: ToolRegistry | None = None,
        notify: Notifier | None = None,
    ):
        self.sessions = sessions
        self.llm = llm
        self.settings = settings
        self.registry = registry or ToolRegistry(default_tools(settings))
        self.notify = notify  # hlášení do #systemove-udalosti (chyby, rozpočet)
        self._running: dict[uuid.UUID, asyncio.Task[Any]] = {}  # běžící běhy agenta
        self._cancelled: set[uuid.UUID] = set()

    @property
    def running_count(self) -> int:
        return len(self._running)

    async def cancel_running(self) -> int:
        """Okamžitě přeruší všechny běžící běhy agenta (používá /stop). Vrací jejich počet.
        Běhy se zaznamenají jako zrušené; rozpracované databázové transakce se vrátí zpět."""
        tasks = list(self._running.items())
        for run_id, task in tasks:
            self._cancelled.add(run_id)
            task.cancel()
        if tasks:
            await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        return len(tasks)

    async def _notify(self, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001 – hlášení nesmí shodit zpracování
            log.exception("Nepodařilo se odeslat systémové hlášení")

    async def handle(self, msg: InboundMessage) -> ChatReply | None:
        """Vrátí odpověď, nebo None, pokud už byla tato zpráva zpracována (duplicita)."""
        try:
            return await self._handle(msg)
        except IntegrityError:
            # souběžné doručení téže zprávy – druhé zpracování se zahodí
            log.info("Duplicitní zpráva %s zahozena", msg.message_external_id)
            return None

    async def _handle(self, msg: InboundMessage) -> ChatReply | None:
        actor = msg.actor or f"user:{msg.channel}:{msg.author_external_id}"
        model = msg.model or self.settings.llm_model_default

        # --- 1. transakce: uložit zprávu, kontroly, založit běh -------------------
        async with self.sessions() as s, s.begin():
            duplicate = await s.scalar(
                select(Message.id).where(Message.external_id == msg.message_external_id)
            )
            if duplicate is not None:
                return None

            conv = await self._get_or_create_conversation(s, msg)
            user_msg = Message(
                conversation_id=conv.id,
                role=MessageRole.USER,
                content=msg.content,
                external_id=msg.message_external_id,
                source=actor,
            )
            s.add(user_msg)
            await s.flush()
            user_msg_id = user_msg.id
            audit(
                s,
                actor=actor,
                action="message.received",
                entity_type="conversation",
                entity_id=conv.id,
                company_id=conv.company_id,
            )

            if not await agents_enabled(s):
                return ChatReply(MSG_STOPPED, None, processed=False)

            spent = await spent_this_month(s)
            if spent >= self.settings.monthly_budget_usd:
                audit(s, actor="system", action="budget.blocked", spent_usd=str(spent))
                return ChatReply(
                    MSG_BUDGET.format(
                        spent=f"{spent:.2f}", budget=self.settings.monthly_budget_usd
                    ),
                    None,
                    processed=False,
                )

            run = AgentRun(
                company_id=conv.company_id,
                trigger=msg.trigger,
                idempotency_key=f"{msg.channel}:{msg.message_external_id}",
                model=model,
                source=actor,
            )
            s.add(run)
            await s.flush()
            run_id = run.id
            conv_id = conv.id
            ctx = ToolContext(
                sessions=self.sessions,
                run_id=run.id,
                company_id=conv.company_id,
                project_id=conv.project_id,
                user_message_id=user_msg_id,
                actor=actor,
                # akce ke schválení smí navrhovat jen běh vyvolaný přímo uživatelem
                allow_approval_requests=msg.trigger == RunTrigger.DISCORD_MESSAGE,
            )
            project = await s.get(Project, conv.project_id) if conv.project_id else None
            history = await self._history(s, conv_id)

        # --- 2. agent: model + nástroje (mimo transakci) -----------------------------
        system = build_system_prompt(datetime.now(UTC).date(), project)
        agent_task = asyncio.ensure_future(
            asyncio.wait_for(
                run_agent(
                    llm=self.llm,
                    registry=self.registry,
                    ctx=ctx,
                    system=system,
                    history=history,
                    model=model,
                    settings=self.settings,
                ),
                timeout=self.settings.max_run_seconds,
            )
        )
        self._running[run_id] = agent_task
        try:
            result = await agent_task
        except asyncio.CancelledError:
            if run_id not in self._cancelled:
                raise  # zrušení zvenčí (vypnutí aplikace) – nepohlcovat
            await self._finish_failed(run_id, RunStatus.CANCELLED, "zastaveno příkazem /stop")
            return ChatReply(MSG_CANCELLED.format(run_id=run_id), run_id, processed=True)
        except TimeoutError:
            await self._finish_failed(run_id, RunStatus.LIMIT_EXCEEDED, "timeout")
            await self._notify(f"⏱️ Běh `{run_id}` překročil časový limit.")
            return ChatReply(
                MSG_TIMEOUT.format(seconds=self.settings.max_run_seconds, run_id=run_id),
                run_id,
                processed=True,
            )
        except Exception as e:  # noqa: BLE001 – každou chybu chceme zaznamenat
            await self._finish_failed(run_id, RunStatus.ERROR, f"{type(e).__name__}: {e}")
            await self._notify(f"⚠️ Běh `{run_id}` skončil chybou: `{type(e).__name__}`")
            return ChatReply(MSG_ERROR.format(run_id=run_id), run_id, processed=True)
        finally:
            self._running.pop(run_id, None)
            self._cancelled.discard(run_id)

        if not result.text.strip() and not result.limit_hit:
            reason = result.stop_reason or "neznámý"
            await self._finish_failed(run_id, RunStatus.ERROR, f"prázdná odpověď modelu ({reason})")
            await self._notify(f"⚠️ Běh `{run_id}`: model nevrátil text (důvod: {reason}).")
            return ChatReply(MSG_EMPTY.format(reason=reason, run_id=run_id), run_id, processed=True)

        # --- 3. transakce: uložit odpověď a výsledek běhu ---------------------------
        status = RunStatus.LIMIT_EXCEEDED if result.limit_hit else RunStatus.SUCCESS
        async with self.sessions() as s, s.begin():
            run = await s.get_one(AgentRun, run_id)
            run.status = status
            run.model = result.model
            run.input_tokens = result.usage.input_tokens
            run.output_tokens = result.usage.output_tokens
            run.cost_usd = result.usage.cost_usd
            run.tool_calls = result.tool_calls
            run.error = f"limit: {result.limit_hit}" if result.limit_hit else None
            run.finished_at = datetime.now(UTC)
            s.add(
                Message(
                    conversation_id=conv_id,
                    role=MessageRole.ASSISTANT,
                    content=result.text,
                    agent_run_id=run_id,
                    source=f"agent_run:{run_id}",
                )
            )
            audit(
                s,
                actor=f"agent:{run_id}",
                action=f"agent_run.{status.value}",
                entity_type="agent_run",
                entity_id=run_id,
                company_id=run.company_id,
                cost_usd=str(result.usage.cost_usd),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                tools=[c["tool"] for c in result.tool_calls],
            )

        await self._check_budget_warning()
        return ChatReply(
            result.text or "(prázdná odpověď)",
            run_id,
            processed=True,
            approvals=tuple(result.approvals),
        )

    async def _check_budget_warning(self) -> None:
        """Jednou za měsíc upozorní, když útrata překročí BUDGET_WARNING_PCT rozpočtu."""
        budget = self.settings.monthly_budget_usd
        async with self.sessions() as s, s.begin():
            spent = await spent_this_month(s)
            if budget <= 0 or spent * 100 < budget * self.settings.budget_warning_pct:
                return
            key = f"budget_warned:{month_start().strftime('%Y-%m')}"
            if not await claim_once(s, key, "system"):
                return
        await self._notify(
            f"💸 Útrata za AI tento měsíc: {spent:.2f} USD z {budget} USD "
            f"({self.settings.budget_warning_pct} % rozpočtu překročeno)."
        )

    # ------------------------------------------------------------------ pomocné

    async def _get_or_create_conversation(
        self, s: AsyncSession, msg: InboundMessage
    ) -> Conversation:
        conv = await s.scalar(
            select(Conversation).where(
                Conversation.channel == msg.channel,
                Conversation.external_id == msg.channel_external_id,
            )
        )
        if conv is not None:
            return conv
        project = await self._project_for_channel(s, msg.channel_name)
        conv = Conversation(
            channel=msg.channel,
            external_id=msg.channel_external_id,
            project_id=project.id if project else None,
            company_id=project.company_id if project else None,
            source=f"{msg.channel}:channel:{msg.channel_external_id}",
        )
        s.add(conv)
        await s.flush()
        return conv

    async def _project_for_channel(self, s: AsyncSession, name: str | None) -> Project | None:
        """Kanál pojmenovaný podle projektu (např. #pomnenice) se k projektu přiřadí."""
        if not name:
            return None
        return await s.scalar(select(Project).where(Project.slug == name.lower()).limit(1))

    async def _history(self, s: AsyncSession, conv_id: uuid.UUID) -> list[LLMMessage]:
        rows = (
            await s.execute(
                select(Message.role, Message.content)
                .where(Message.conversation_id == conv_id, Message.role != MessageRole.SYSTEM)
                .order_by(Message.seq.desc())
                .limit(self.settings.conversation_history_messages)
            )
        ).all()
        msgs = [
            LLMMessage("user" if r.role == MessageRole.USER else "assistant", r.content)
            for r in reversed(rows)
        ]
        return normalize_history(msgs)

    async def _finish_failed(self, run_id: uuid.UUID, status: RunStatus, error: str) -> None:
        async with self.sessions() as s, s.begin():
            run = await s.get_one(AgentRun, run_id)
            run.status = status
            run.error = error[:2000]
            run.finished_at = datetime.now(UTC)
            audit(
                s,
                actor=f"agent:{run_id}",
                action=f"agent_run.{status.value}",
                entity_type="agent_run",
                entity_id=run_id,
                company_id=run.company_id,
                error=error[:500],
            )
