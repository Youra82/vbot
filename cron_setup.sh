#!/bin/bash
# vbot — Cron-Job einrichten (VPS)
# Laeuft jede Stunde (passend zum 1h Timeframe)
# Fuer andere Timeframes anpassen:
#   15m → */15 * * * *  (alle 15 Minuten)
#   1h  → 0 * * * *    (jede Stunde)
#   4h  → 0 */4 * * *  (alle 4 Stunden)
#   1d  → 0 0 * * *    (taeglich Mitternacht)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_CMD="0 * * * * cd $SCRIPT_DIR && .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1"

echo "Fuege Cron-Job hinzu:"
echo "  $CRON_CMD"
(crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
echo "Cron-Job eingerichtet."
crontab -l
