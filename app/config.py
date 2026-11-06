"""Konfigurace aplikace – vše se načítá z proměnných prostředí (.env).

Žádné tajemství se nesmí objevit ve zdrojovém kódu.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "production", "test"] = "development"

    database_url: str = "postgresql+psycopg://aihq:aihq_dev@localhost:5432/aihq"

    discord_bot_token: SecretStr | None = None
    discord_guild_id: int | None = None
    discord_owner_user_id: int | None = None

    anthropic_api_key: SecretStr | None = None
    llm_model_default: str = "claude-sonnet-5-5"
    llm_model_briefing: str = "claude-opus-5-5"
    voyage_api_key: SecretStr | None = None

    monthly_budget_usd: Decimal = Decimal("50")
    budget_warning_pct: int = 80
    max_tool_steps_per_run: int = 10
    max_run_seconds: int = 120
    max_run_cost_usd: Decimal = Decimal("1.0")

    documents_dir: Path = Path("./data/documents")


@lru_cache
def get_settings() -> Settings:
    return Settings()
