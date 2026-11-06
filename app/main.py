"""Vstupní bod aplikace.

Den 1: pouze FastAPI s kontrolou stavu. Discord bot a plánovač se přidají v dalších dnech.
"""

from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, text

from app.db.models import SystemState
from app.db.session import session_scope

app = FastAPI(title="AI HQ", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Ověří, že aplikace běží a databáze odpovídá."""
    async with session_scope() as s:
        await s.execute(text("SELECT 1"))
        enabled = await s.scalar(
            select(SystemState.value).where(SystemState.key == "agents_enabled")
        )
    return {"status": "ok", "database": "ok", "agents_enabled": bool(enabled)}
