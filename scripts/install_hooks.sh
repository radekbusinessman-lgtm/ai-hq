#!/bin/sh
# Nainstaluje kontrolu tajemství, která běží před každým commitem.
set -e
cat > .git/hooks/pre-commit <<'EOF'
#!/bin/sh
python3 scripts/check_secrets.py
EOF
chmod +x .git/hooks/pre-commit
echo "Kontrola tajemství před commitem je aktivní."
