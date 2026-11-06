"""Testy běží proti skutečnému PostgreSQL s pgvector (databáze TEST_DATABASE_URL).

Před testy se databáze vyčistí a aplikují se migrace – testuje se tedy i samotná migrace.
"""

import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://aihq:aihq_dev@localhost:5432/aihq_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_ENV"] = "test"


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    sync = create_engine(TEST_DATABASE_URL)
    with sync.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    sync.dispose()

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Každý test běží v transakci, která se na konci vrátí – testy se navzájem neovlivní."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with maker() as s:
            yield s
        await trans.rollback()
    await engine.dispose()
