#!/bin/bash
# AI HQ – řízené ukončení provozu (24. 11. 2026). Bezpečné spustit opakovaně.
# Neměnné: data, zálohy ani soubory se nemažou.
cd "$(dirname "$0")/.." || exit 1
export PATH="/Volumes/Docker/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$HOME/.docker/bin:$HOME/.orbstack/bin:$PATH"
mkdir -p logs
LOG="logs/zastaveni-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== AI HQ – ukončení provozu $(date '+%d.%m.%Y %H:%M') ==="

echo; echo "## 1. Docker"
DOCKER=$(command -v docker || true)
echo "docker: ${DOCKER:-NENALEZEN}"
ls -d /Volumes/Docker/Docker.app /Applications/Docker.app "$HOME/Applications/Docker.app" /Applications/OrbStack.app 2>/dev/null
command -v colima podman orbctl 2>/dev/null
echo "Procesy Dockeru:"; pgrep -lf "Docker.app|com.docker" | head -5
docker context ls 2>&1
if [ -n "$DOCKER" ] && ! docker info >/dev/null 2>&1; then
  echo "Docker neodpovídá – spouštím Docker Desktop, aby šly kontejnery trvale odstranit…"
  open -a /Volumes/Docker/Docker.app 2>/dev/null || open -a Docker
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
if [ -n "$DOCKER" ] && docker info >/dev/null 2>&1; then
  echo "Docker běží. Kontejnery před zastavením:"; docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}'

  echo; echo "## 2. Závěrečná záloha databáze"
  TS=$(date +%Y%m%d-%H%M)
  if docker compose run --rm --entrypoint sh backup -c \
     'pg_dump --format=custom --no-owner --no-privileges --schema=public --file=/backups/aihq-ZAVERECNA-'"$TS"'.dump "$(printf %s "$DATABASE_URL" | sed "s#^postgresql+psycopg://#postgresql://#")"'; then
    echo "OK: backups/aihq-ZAVERECNA-$TS.dump"
  else
    echo "CHYBA: závěrečná záloha selhala (poslední noční záloha zůstává)"
  fi
  mkdir -p "$HOME/ai-hq-zaloha/databaze"
  cp -p backups/*.dump backups/backup.log "$HOME/ai-hq-zaloha/databaze/" && echo "OK: zálohy zkopírovány do ~/ai-hq-zaloha/databaze"

  echo; echo "## 3. Zastavení AI HQ (docker compose down)"
  docker compose --profile local down --remove-orphans
  echo "Kontejnery po zastavení:"; docker ps -a --format '{{.Names}}\t{{.Status}}'
  echo "Ostatní projekty v Dockeru:"; docker compose ls -a 2>/dev/null
else
  echo "Docker neběží nebo není nainstalovaný – kontejnery AI HQ teď neběží."
  mkdir -p "$HOME/ai-hq-zaloha/databaze"
  cp -p backups/*.dump backups/backup.log "$HOME/ai-hq-zaloha/databaze/" && echo "OK: zálohy zkopírovány do ~/ai-hq-zaloha/databaze"
fi

echo; echo "## 4. Automatický start po přihlášení"
SET="$HOME/Library/Group Containers/group.com.docker/settings-store.json"
[ -f "$SET" ] || SET="$HOME/Library/Group Containers/group.com.docker/settings.json"
if [ -f "$SET" ]; then
  cp -p "$SET" "$SET.pred-ukoncenim"
  /usr/bin/python3 - "$SET" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p))
for k in ("AutoStart","autoStart"):
    if k in d or k=="AutoStart": d[k]=False
json.dump(d,open(p,"w"),indent=2)
print("Docker Desktop: start po přihlášení VYPNUT")
PY
else
  echo "Nastavení Docker Desktop nenalezeno"
fi
osascript -e 'tell application "System Events" to get the name of every login item' 2>&1 | sed 's/^/Položky po přihlášení: /'
osascript -e 'tell application "System Events" to if exists login item "Docker" then delete login item "Docker"' 2>&1
echo "LaunchAgents (uživatel):"; ls ~/Library/LaunchAgents 2>/dev/null
grep -lis "ai-hq\|aihq\|docker" ~/Library/LaunchAgents/* 2>/dev/null | sed 's/^/  souvisí: /'
echo "crontab:"; crontab -l 2>&1

echo; echo "## 5. Ukončení Docker Desktop"
osascript -e 'quit app "Docker"' 2>/dev/null && echo "Docker Desktop ukončen" || echo "Docker Desktop neběžel"

echo; echo "## 6. Bezpečnostní nastavení Macu (jen čtení)"
fdesetup status 2>&1 | sed 's/^/FileVault: /'
echo "Automatické přihlášení: $(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || echo vypnuto)"
echo "Automatická instalace macOS: $(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null || echo výchozí)"
pmset -g | grep -E " sleep |disksleep|displaysleep|autorestart|womp"
echo; echo "=== HOTOVO. Log: $LOG ==="
