#!/bin/sh
# Noční záloha databáze (pg_dump) – běží ve vlastním kontejneru "backup" (docker-compose.yml).
# Zálohy: ./backups/aihq-RRRR-MM-DD.dump (formát custom, obnova přes pg_restore).
# Uchovává se BACKUP_KEEP_DAYS dní (výchozí 30). Výsledek se zapisuje do ./backups/backup.log.
set -eu

BACKUP_DIR=/backups
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
HOUR="${BACKUP_HOUR:-3}"          # hodina zálohy (čas Europe/Prague)
# SQLAlchemy URL → libpq URL (bez +psycopg)
PGURL=$(printf '%s' "$DATABASE_URL" | sed 's#^postgresql+psycopg://#postgresql://#')

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$BACKUP_DIR/backup.log"; }

run_backup() {
    day=$(date +%Y-%m-%d)
    target="$BACKUP_DIR/aihq-$day.dump"
    if [ -s "$target" ]; then
        log "záloha $day už existuje – přeskakuji"
        return 0
    fi
    if pg_dump --format=custom --no-owner --no-privileges --schema=public \
            --file="$target.part" "$PGURL" 2>>"$BACKUP_DIR/backup.log"; then
        mv "$target.part" "$target"
        log "OK záloha $day ($(du -h "$target" | cut -f1))"
    else
        rm -f "$target.part"
        log "CHYBA záloha $day selhala"
        return 1
    fi
    find "$BACKUP_DIR" -name 'aihq-*.dump' -mtime +"$KEEP_DAYS" -print -delete \
        | sed 's/^/smazána stará záloha: /' >> "$BACKUP_DIR/backup.log" || true
}

mkdir -p "$BACKUP_DIR"
if [ "${1:-}" = "--now" ]; then
    run_backup
    exit $?
fi

log "plánovač záloh spuštěn (denně v $HOUR:00, uchovat $KEEP_DAYS dní)"
run_backup || true   # první záloha hned po startu (pokud dnes ještě není)
while true; do
    sleep 600
    if [ "$(date +%H)" -eq "$HOUR" ] 2>/dev/null || [ "$(date +%-H)" = "$HOUR" ]; then
        run_backup || true
    fi
done
