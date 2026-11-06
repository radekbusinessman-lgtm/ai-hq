# Obnova databáze ze zálohy

Zálohy vytváří služba `backup` každou noc ve 3:00 do složky `~/ai-hq/backups/`
(`aihq-RRRR-MM-DD.dump`, uchovává se 30 dní). Průběh: `~/ai-hq/backups/backup.log`.

Okamžitá záloha (např. před větší změnou):
```sh
cd ~/ai-hq && docker compose run --rm backup /scripts/backup.sh --now
```

## Obnova (ověřeno 6. 11. 2026 na kopii databáze – počty záznamů souhlasily)

1. Zastavit aplikaci, aby během obnovy nic nezapisovala:
   `docker compose stop app`
2. Obnovit do **prázdné** databáze (nový projekt Supabase nebo lokální Postgres):
   ```sh
   docker compose run --rm -v "$PWD/backups:/backups" backup \
     pg_restore --no-owner --no-privileges -d "postgresql://…cílová-databáze…" /backups/aihq-RRRR-MM-DD.dump
   ```
   Hlášky o schématu `public` („already exists“) jsou neškodné.
3. V `.env` přepnout `DATABASE_URL` na obnovenou databázi a spustit
   `scripts/2-spustit-ai-hq.command` (doběhnou chybějící migrace, včetně zablokování Data API).
