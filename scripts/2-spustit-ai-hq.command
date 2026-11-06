#!/bin/bash
# AI HQ – spuštění / aktualizace serveru. Spusťte dvojklikem.
# Bezpečné spustit opakovaně: jen znovu sestaví a restartuje aplikaci.
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
mkdir -p logs
LOG="logs/spusteni-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=================================================="
echo " AI HQ – spuštění serveru  ($(date '+%d.%m.%Y %H:%M'))"
echo "=================================================="

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "CHYBA: Docker není nainstalovaný."
  read -r -p "Stiskněte Enter pro zavření…"; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop neběží – spouštím ho a čekám…"
  open -a Docker
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
docker info >/dev/null 2>&1 || { echo "CHYBA: Docker se nespustil."; read -r -p "Enter…"; exit 1; }
echo "OK: Docker běží"

# 2. Konfigurace (.env) – kontroluje se jen, že hodnoty nejsou prázdné; nic se nevypisuje
if [ ! -f .env ]; then
  echo "CHYBA: chybí soubor .env"; read -r -p "Enter…"; exit 1
fi
CHYBI=0
for KLIC in DATABASE_URL DISCORD_BOT_TOKEN DISCORD_GUILD_ID DISCORD_OWNER_USER_ID ANTHROPIC_API_KEY; do
  HODNOTA=$(grep -E "^${KLIC}=" .env | head -1 | cut -d= -f2- | sed -E 's/[[:space:]]*#.*$//; s/[[:space:]]+$//')
  if [ -z "$HODNOTA" ]; then echo "CHYBÍ hodnota: $KLIC"; CHYBI=1; else echo "OK: $KLIC vyplněno"; fi
done
if grep -qE "^DATABASE_URL=postgresql://" .env; then
  echo "OPRAVA: DATABASE_URL – měním začátek na postgresql+psycopg://"
  sed -i '' -E 's#^DATABASE_URL=postgresql://#DATABASE_URL=postgresql+psycopg://#' .env
fi
if grep -q "YOUR-PASSWORD" .env; then echo "CHYBA: v DATABASE_URL zůstalo [YOUR-PASSWORD]"; CHYBI=1; fi
[ $CHYBI -eq 0 ] || { echo "Doplňte prosím .env a spusťte znovu."; read -r -p "Enter…"; exit 1; }

# 3. Sestavení a spuštění (migrace databáze proběhnou automaticky při startu)
echo; echo "Sestavuji a spouštím (první spuštění trvá několik minut)…"
docker compose up -d --build || { echo "CHYBA při spuštění"; read -r -p "Enter…"; exit 1; }

# 4. Čekání na zdravý stav
echo "Čekám na aplikaci…"
for _ in $(seq 1 40); do
  curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 3
done
if curl -fsS http://127.0.0.1:8000/health; then
  echo; echo "OK: aplikace odpovídá"
else
  echo "CHYBA: aplikace neodpovídá – posílám log níže"
fi

# 5. Založení projektu Pomněnice (opakované spuštění nic nezdvojí)
docker compose exec -T app python -m app.seed

# 6. Log aplikace pro kontrolu
sleep 5
docker compose logs --no-color --tail 80 app > logs/app-posledni.log 2>&1
echo; echo "--- posledních 30 řádků logu aplikace ---"
tail -30 logs/app-posledni.log
echo
if grep -q "Discord bot připojen" logs/app-posledni.log; then
  echo "✅ HOTOVO: bot je online. Napište mu do Discordu (#pomnenice)."
else
  echo "⚠️  Bot zatím nehlásí připojení – zkontrolujeme log."
fi
read -r -p "Stiskněte Enter pro zavření…"
