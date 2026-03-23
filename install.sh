#!/bin/bash
# vbot — Installation
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ">>> Erstelle virtuelle Umgebung..."
python3 -m venv .venv

echo ">>> Installiere Abhaengigkeiten..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo ">>> Erstelle notwendige Verzeichnisse..."
mkdir -p artifacts/tracker artifacts/results logs data/cache src/vbot/strategy/configs

echo ">>> Installation abgeschlossen."
echo "Kopiere jetzt secret.json.template -> secret.json und fuege deine API-Keys ein."
