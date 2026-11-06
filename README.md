# AI HQ – Autonomous Business Operating System

Vlastní AI Chief of Staff. Rozhraní: Discord. Paměť: PostgreSQL (Supabase) s pgvector.
AI: Claude API. Pilotní projekt: **Osada Pomněnice**.

## Stav
| Den | Obsah | Stav |
|---|---|---|
| 1 | Kostra, databáze, migrace, testy | ✅ otestováno |
| 2 | Discord bot + Claude + ukládání konverzace | ✅ otestováno se simulovaným modelem; čeká na ostré připojení |
| 3 | Projektová paměť (fakta se zdrojem) | ⏳ |
| 4 | Úkoly | ⏳ |
| 5 | Dokumenty a vyhledávání s citací | ⏳ |
| 6 | Briefing, plánovač, limity, `/stop` | ⏳ |
| 7 | Nasazení, zálohy, test restartu | ⏳ |

## Struktura
```
app/
  config.py      nastavení z .env
  db/            datový model (firemní paměť) a připojení
  seed.py        založení firmy a projektu Pomněnice
  agent/         zpracování zpráv (vypínač, rozpočet, kontext, evidence běhů)
  llm/           adaptér pro Claude (výměnný za jiný model)
  discord_bot/   Discord – ověření identity, odpovědi
  audit.py       zápis do auditního logu
  main.py        aplikace: /health + Discord bot v jednom procesu
migrations/      databázové migrace (Alembic)
tests/           automatické testy (proti skutečnému Postgresu)
docs/decisions/  záznamy architektonických rozhodnutí
scripts/         kontrola tajemství před commitem
```

## Spuštění (vývoj)
```sh
cp .env.example .env              # doplnit hodnoty
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
sh scripts/install_hooks.sh       # kontrola, aby se klíče nedostaly do Gitu
docker compose --profile local up -d db
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed
.venv/bin/pytest
```

## Spuštění (server – Mac mini / VPS)
```sh
docker compose up -d --build      # migrace proběhnou automaticky při startu
curl http://127.0.0.1:8000/health
```

## Bezpečnostní zásady
- Tajemství jen v `.env`, nikdy v Gitu (hlídá `scripts/check_secrets.py`).
- Auditní log je jen pro přidávání – změny a mazání blokuje databáze.
- Počáteční informace o projektech jsou uloženy jako **předpoklady**, ne ověřená fakta.
- Každý záznam nese `source` (původ), `created_at` a `updated_at`.
