"""Konfigurace aplikace – vše se načítá z proměnných prostředí (.env).

Žádné tajemství se nesmí objevit ve zdrojovém kódu.
"""

from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # prázdná hodnota v .env = nenastaveno (ne chyba)
    )

    app_env: Literal["development", "production", "test"] = "development"

    database_url: str = "postgresql+psycopg://aihq:aihq_dev@localhost:5432/aihq"

    discord_bot_token: SecretStr | None = None
    discord_guild_id: int | None = None
    discord_owner_user_id: int | None = None

    anthropic_api_key: SecretStr | None = None
    llm_model_default: str = "claude-sonnet-5-5"
    llm_model_briefing: str = "claude-opus-5-5"
    voyage_api_key: SecretStr | None = None
    anthropic_key_expires_on: date | None = None  # pro upozornění v briefingu
    llm_max_output_tokens: int = 2000
    conversation_history_messages: int = 20

    # Ceny v USD za 1M tokenů (vstup, výstup). Záměrně opatrné (spíš vyšší) výchozí hodnoty –
    # skutečné ceny ověřte v Anthropic Console a případně přepište v .env (JSON).
    llm_prices_per_mtok: dict[str, tuple[Decimal, Decimal]] = {
        "claude-haiku-5-5": (Decimal("1"), Decimal("5")),
        "claude-sonnet-5-5": (Decimal("3"), Decimal("15")),
        "claude-opus-5-5": (Decimal("15"), Decimal("75")),
    }

    monthly_budget_usd: Decimal = Decimal("50")
    budget_warning_pct: int = 80
    max_tool_steps_per_run: int = 10
    max_run_seconds: int = 120
    max_run_cost_usd: Decimal = Decimal("1.0")

    documents_dir: Path = Path("./data/documents")

    # Discord kanály (podle názvu) a plánované úlohy
    discord_briefing_channel: str = "chief-of-staff"
    discord_events_channel: str = "systemove-udalosti"
    timezone: str = "Europe/Prague"
    briefing_time: str = "07:30"  # denní briefing (HH:MM, místní čas); prázdné = vypnuto
    key_expiry_warning_days: int = 14
    backups_dir: Path = Path("./backups")
    backup_max_age_hours: int = 26


@lru_cache
def get_settings() -> Settings:
    return Settings()
