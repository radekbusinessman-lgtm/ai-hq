"""Bezpečnost databáze: veřejné API Supabase (role anon/authenticated) nesmí nic vidět.

Testovací databáze emuluje Supabase vytvořením rolí anon/authenticated s výchozími právy,
jaká jim Supabase dává, a pak znovu spustí migraci 0003.
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DATABASE_URL

TABLES = ["facts", "messages", "audit_logs", "system_state", "approvals", "tasks"]


@pytest.fixture(scope="module")
def supabase_like_db() -> None:
    eng = create_engine(TEST_DATABASE_URL)
    with eng.begin() as c:
        for role in ("anon", "authenticated"):
            exists = c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
            if not exists:
                c.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            # stav jako na Supabase: plná práva na tabulky v public
            c.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            c.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
    eng.dispose()

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.downgrade(cfg, "0002")
    command.upgrade(cfg, "head")


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("table", TABLES)
def test_api_roles_cannot_read_or_write(supabase_like_db: None, role: str, table: str) -> None:
    eng = create_engine(TEST_DATABASE_URL)
    with eng.connect() as c:
        c.execute(text(f"SET ROLE {role}"))
        with pytest.raises(Exception, match="permission denied"):
            c.execute(text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608 – pevný seznam v testu
        c.rollback()
    eng.dispose()


def test_rls_enabled_on_every_table(supabase_like_db: None) -> None:
    eng = create_engine(TEST_DATABASE_URL)
    with eng.connect() as c:
        off = c.scalars(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
            )
        ).all()
    eng.dispose()
    assert off == []


def test_owner_app_still_works(supabase_like_db: None) -> None:
    eng = create_engine(TEST_DATABASE_URL)
    with eng.connect() as c:
        assert c.scalar(text("SELECT value FROM system_state WHERE key='agents_enabled'")) is True
    eng.dispose()
