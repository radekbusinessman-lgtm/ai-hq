#!/bin/bash
# AI HQ – jednorázové nastavení Mac mini jako serveru.
# Spusťte dvojklikem. Zeptá se na heslo správce (zadáváte vy, nikdo jiný ho nevidí).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOG="logs/nastaveni-$(date +%Y%m%d-%H%M%S).log"

echo "=================================================="
echo " AI HQ – nastavení Mac mini jako serveru"
echo "=================================================="
echo
echo "Tento skript změní tato nastavení macOS:"
echo "  1. Počítač se nebude uspávat (displej se smí vypnout po 10 min)."
echo "  2. Po výpadku proudu se Mac sám znovu zapne."
echo "  3. Aktualizace macOS se nebudou instalovat automaticky"
echo "     (server se nebude sám restartovat; instalujete ručně)."
echo
read -r -p "Pokračovat? (a/n): " ODPOVED
if [ "$ODPOVED" != "a" ]; then
  echo "Zrušeno, nic se nezměnilo." | tee -a "$LOG"
  read -r -p "Stiskněte Enter pro zavření…"
  exit 0
fi

echo
echo "Zadejte heslo správce tohoto Macu (při psaní se nezobrazuje):"
{
  sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1 womp 1 &&
  sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false &&
  echo "OK: nastavení uloženo"
} 2>&1 | tee -a "$LOG"

echo | tee -a "$LOG"
echo "Kontrola:" | tee -a "$LOG"
pmset -g | grep -E " sleep |autorestart|disksleep" | tee -a "$LOG"
echo "AutomaticallyInstallMacOSUpdates = $(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null)" | tee -a "$LOG"
echo
echo "Hotovo. Výsledek je uložen v $LOG"
read -r -p "Stiskněte Enter pro zavření…"
