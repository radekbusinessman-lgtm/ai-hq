# AI HQ – archiv projektu a návod na obnovu

**Stav:** projekt pozastaven 24. 11. 2026 (rozhodnutí Radoslava Bodečka).
Důvod: architektura je na současné potřeby příliš složitá (mnoho externích služeb,
databáze, účtů a předplatných). Cílem bylo šetřit čas, ne spravovat infrastrukturu.

## Co je kde archivováno

| Co | Kde | Poznámka |
|---|---|---|
| Zdrojový kód (v0.9.0) | tento repozitář, větev `main` | Shodný s nasazenou kopií na Mac mini (ověřeno 24. 11. 2026 kontrolními součty) |
| Rozhodnutí a architektura | `docs/decisions/0001–0004` | Orchestrátor, hosting, oddělení firem, iCloud pošta |
| Konfigurace bez klíčů | `.env.example`, `docker-compose.yml`, `Dockerfile` | Žádné klíče ani hesla nejsou v Gitu |
| Databáze (záloha) | Mac mini: `~/ai-hq/backups/aihq-RRRR-MM-DD.dump` a `~/ai-hq-zaloha/` | **Záloha není v Gitu** (obsahuje firemní data). Formát `pg_dump -Fc`, Postgres 17 |
| Poslední ověřená záloha | `aihq-2026-11-24.dump` (88 kB) | Ověřeno 24. 11. 2026: čitelná přes `pg_restore` 17, 17 tabulek (66 zpráv, 33 běhů agenta, 8 faktů, 73 auditních záznamů, 1 projekt, 1 úkol) |
| Git bundly (starší) | Mac mini: `~/ai-hq-zaloha/*.bundle` | Kopie repozitáře k 6. 11. 2026 |

## Proměnné v `.env` (hodnoty nejsou archivovány)

| Proměnná | Služba | Při obnově |
|---|---|---|
| `DATABASE_URL` | Supabase (Postgres 17, region eu-west-2) | Nový projekt Supabase nebo lokální Postgres s pgvector |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_OWNER_USER_ID` | Discord | Nová aplikace bota v Developer Portalu (původní bot odstraněn), pozvat na existující server |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_KEY_EXPIRES_ON` | Claude API | Nový klíč v platform.claude.com |
| `VOYAGE_API_KEY` | Voyage AI (embeddingy, zatím nepoužito) | Jen pokud se dokončí vyhledávání v dokumentech |
| `ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD` | iCloud Mail (jen čtení) | Nové heslo pro aplikaci na account.apple.com; vyžaduje zapnutý FileVault |
| `LLM_MODEL_DEFAULT`, `LLM_MODEL_BRIEFING`, `MONTHLY_BUDGET_USD` … | nastavení | Výchozí hodnoty v `.env.example` |

Skutečné náklady provozu (z tabulky `agent_runs`): ~9 USD za 7.–21. 11. 2026,
tj. přibližně 15 USD/měsíc, hlavně denní briefingy.

## Obnova krok za krokem

1. **Kód:** `git clone https://github.com/radekbusinessman-lgtm/ai-hq ~/ai-hq`
2. **Konfigurace:** `cp .env.example .env`, `chmod 600 .env`, vyplnit hodnoty z tabulky výše
   (nové klíče – původní byly při ukončení zneplatněny).
3. **Databáze:** založit prázdnou databázi (Supabase nebo `docker compose --profile local up -d db`)
   a obnovit poslední zálohu podle `docs/obnova-zalohy.md`
   (`pg_restore --no-owner --no-privileges -d "<DATABASE_URL>" aihq-RRRR-MM-DD.dump`).
   Pozor: `DATABASE_URL` v `.env` musí začínat `postgresql+psycopg://`, pro `pg_restore` použít `postgresql://`.
4. **Spuštění:** Docker Desktop, pak dvojklik na `scripts/2-spustit-ai-hq.command`
   (doběhnou migrace, ověří se `/health`, bot se přihlásí do Discordu).
5. **Mac mini jako server (volitelné):** `scripts/1-nastavit-mac-mini.command`
   (neusínat, autorestart). Nevypínat FileVault – pro uložení hesla k iCloudu je nutný.

## Ukončení provozu – postup

1. Poslední záloha: `docker compose run --rm backup /scripts/backup.sh --now`
2. Zastavení: `cd ~/ai-hq && docker compose down` (odstraní kontejnery `app` a `backup`,
   tím končí bot, plánovač – briefing 7:30, kontroly 8:05 – i noční zálohy; data na disku zůstanou).
3. Docker Desktop → Settings → General → vypnout „Start Docker Desktop when you sign in“.
4. Restart Macu a kontrola, že `docker ps` nic neukazuje / Docker neběží.

Přehled zrušených služeb a klíčů je v předávacím protokolu projektu AI HQ.
