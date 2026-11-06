"""Zablokování veřejného Data API Supabase (role anon / authenticated).

Aplikace se připojuje přímo k Postgresu jako vlastník tabulek, takže RLS ji neomezuje
(vlastník RLS obchází, pokud není FORCE). Role anon/authenticated, přes které jde veřejné
REST/GraphQL API Supabase, ztratí veškerá práva a RLS bez politik jim navíc nevrátí žádný řádek.

Na čistém Postgresu (testy, lokální vývoj) role anon/authenticated neexistují – revoke se přeskočí,
RLS se zapne stejně.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    # 1) RLS na všech tabulkách schématu public (bez politik = nic pro cizí role)
    op.execute(
        """
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
                EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
            END LOOP;
        END $$;
        """
    )
    # 2) Odebrat práva rolím veřejného API (jen pokud existují – Supabase)
    for role in API_ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};
                    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};
                    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role};
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role};
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role};
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM {role};
                END IF;
            END $$;
            """
        )
    # 3) Funkce auditu nesmí volat nikdo zvenčí
    op.execute("REVOKE ALL ON FUNCTION audit_logs_append_only() FROM PUBLIC")


def downgrade() -> None:
    # Práva rolím API se záměrně NEVRACEJÍ (bezpečnější výchozí stav). Vypne se jen RLS.
    op.execute(
        """
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
                EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t.tablename);
            END LOOP;
        END $$;
        """
    )
