#!/bin/bash
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

VENV_PATH=".venv/bin/activate"
PYTHON=".venv/bin/python3"
CONFIGS_DIR="src/vbot/strategy/configs"

if [ ! -f "$VENV_PATH" ]; then
    echo -e "${RED}Fehler: .venv nicht gefunden. Erst install.sh ausfuehren.${NC}"
    exit 1
fi

source "$VENV_PATH"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║     vbot — Fibonacci Candle Overlap      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Waehle einen Analyse-Modus:${NC}"
echo "  1) Einzel-Analyse              (jede Strategie wird isoliert getestet)"
echo "  2) Manuelle Portfolio-Simulation  (du waehlst das Team)"
echo "  3) Automatische Portfolio-Optimierung  (der Bot waehlt das beste Team)"
echo "  4) Interaktive Charts          (Candlestick + Entry/Exit-Marker)"
echo ""
read -p "Auswahl (1-4) [Standard: 1]: " MODE
MODE="${MODE//[$'\r\n ']/}"

if [[ ! "$MODE" =~ ^[1-4]?$ ]]; then
    echo -e "${RED}Ungueltige Eingabe. Verwende Standard (1).${NC}"
    MODE=1
fi
MODE=${MODE:-1}

# ─────────────────────────────────────────
# Modus 1: Einzel-Analyse — alle Configs isoliert
# ─────────────────────────────────────────
if [ "$MODE" == "1" ]; then
    echo ""
    echo -e "${CYAN}--- Bitte Konfiguration fuer den Backtest festlegen ---${NC}"

    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2024-01-01]: " DATE_FROM
    DATE_FROM="${DATE_FROM//[$'\r\n ']/}"
    [ -z "$DATE_FROM" ] && DATE_FROM="2024-01-01"

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " DATE_TO
    DATE_TO="${DATE_TO//[$'\r\n ']/}"

    read -p "Startkapital in USDT eingeben [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    [[ ! "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] && CAPITAL=1000

    echo "--------------------------------------------------"

    DATE_ARGS=""
    [[ "$DATE_FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="--from $DATE_FROM"
    [[ "$DATE_TO"   =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="$DATE_ARGS --to $DATE_TO"

    $PYTHON src/vbot/analysis/show_results.py \
        --mode 1 \
        --capital "$CAPITAL" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Modus 2: Manuelle Portfolio-Simulation
# ─────────────────────────────────────────
elif [ "$MODE" == "2" ]; then
    echo ""
    echo -e "${CYAN}--- Manuelle Portfolio-Simulation ---${NC}"
    echo -e "${YELLOW}Verfuegbare Configs:${NC}"

    mapfile -t CONFIG_FILES < <(ls "$CONFIGS_DIR"/config_*.json 2>/dev/null | xargs -I{} basename {})

    if [ ${#CONFIG_FILES[@]} -eq 0 ]; then
        echo -e "${RED}Keine Configs gefunden. Erst run_pipeline.sh ausfuehren.${NC}"
        deactivate
        exit 1
    fi

    for i in "${!CONFIG_FILES[@]}"; do
        printf "  %2d) %s\n" "$((i+1))" "${CONFIG_FILES[$i]}"
    done
    echo ""
    read -p "Strategien waehlen (z.B. '1 3 5' oder 'alle') [Standard: alle]: " SELECTION
    SELECTION="${SELECTION//[$'\r\n']/}"
    [ -z "$SELECTION" ] && SELECTION="alle"

    SELECTED_FILES=""
    if [[ "$SELECTION" == "alle" ]]; then
        SELECTED_FILES="${CONFIG_FILES[*]}"
    else
        for num in $SELECTION; do
            idx=$((num - 1))
            if [ "$idx" -ge 0 ] && [ "$idx" -lt ${#CONFIG_FILES[@]} ]; then
                SELECTED_FILES="$SELECTED_FILES ${CONFIG_FILES[$idx]}"
            fi
        done
        SELECTED_FILES="${SELECTED_FILES# }"
    fi

    if [ -z "$SELECTED_FILES" ]; then
        echo -e "${RED}Keine gueltige Auswahl.${NC}"
        deactivate
        exit 1
    fi

    echo ""
    echo -e "${CYAN}--- Bitte Konfiguration fuer den Backtest festlegen ---${NC}"

    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2024-01-01]: " DATE_FROM
    DATE_FROM="${DATE_FROM//[$'\r\n ']/}"
    [ -z "$DATE_FROM" ] && DATE_FROM="2024-01-01"

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " DATE_TO
    DATE_TO="${DATE_TO//[$'\r\n ']/}"

    read -p "Startkapital in USDT eingeben [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    [[ ! "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] && CAPITAL=1000

    echo "--------------------------------------------------"

    DATE_ARGS=""
    [[ "$DATE_FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="--from $DATE_FROM"
    [[ "$DATE_TO"   =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="$DATE_ARGS --to $DATE_TO"

    $PYTHON src/vbot/analysis/show_results.py \
        --mode 2 \
        --capital "$CAPITAL" \
        --configs "$SELECTED_FILES" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Modus 3: Automatische Portfolio-Optimierung
# ─────────────────────────────────────────
elif [ "$MODE" == "3" ]; then
    echo ""
    echo -e "${CYAN}--- Automatische Portfolio-Optimierung ---${NC}"
    echo -e "${YELLOW}Findet die optimale Coin/Timeframe-Kombination aus deinen vorhandenen Configs.${NC}"
    echo ""

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    [[ ! "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] && CAPITAL=1000

    read -p "Max Drawdown % [Standard: 30]: " TARGET_DD
    TARGET_DD="${TARGET_DD//[$'\r\n ']/}"
    [[ ! "$TARGET_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]] && TARGET_DD=30

    read -p "Min Win-Rate % (0 = kein Limit) [Standard: 0]: " MIN_WR
    MIN_WR="${MIN_WR//[$'\r\n ']/}"
    [[ ! "$MIN_WR" =~ ^[0-9]+(\.[0-9]+)?$ ]] && MIN_WR=0

    echo ""
    echo -e "${YELLOW}Zeitraum:${NC}"

    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2024-01-01]: " DATE_FROM
    DATE_FROM="${DATE_FROM//[$'\r\n ']/}"
    [ -z "$DATE_FROM" ] && DATE_FROM="2024-01-01"

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " DATE_TO
    DATE_TO="${DATE_TO//[$'\r\n ']/}"

    echo ""

    DATE_ARGS=""
    [[ "$DATE_FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="--from $DATE_FROM"
    [[ "$DATE_TO"   =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="$DATE_ARGS --to $DATE_TO"

    $PYTHON src/vbot/analysis/show_results.py \
        --mode 3 \
        --capital "$CAPITAL" \
        --target-max-dd "$TARGET_DD" \
        --min-wr "$MIN_WR" \
        $DATE_ARGS

    # --- Angebot: settings.json mit optimalem Portfolio aktualisieren ---
    OPT_FILE="artifacts/results/optimization_results.json"
    if [ -f "$OPT_FILE" ]; then
        echo ""
        echo -e "${YELLOW}Moechtest du settings.json mit dem optimalen Portfolio aktualisieren?${NC}"
        read -p "Dies setzt active_strategies auf die gefundenen Strategien. (j/n) [Standard: n]: " UPDATE_SETTINGS
        UPDATE_SETTINGS="${UPDATE_SETTINGS:-n}"
        if [[ "$UPDATE_SETTINGS" == "j" || "$UPDATE_SETTINGS" == "J" ]]; then
            $PYTHON - <<'PYEOF'
import json, os, sys

PROJECT_ROOT  = os.getcwd()
opt_file      = os.path.join(PROJECT_ROOT, "artifacts", "results", "optimization_results.json")
settings_file = os.path.join(PROJECT_ROOT, "settings.json")
configs_dir   = os.path.join(PROJECT_ROOT, "src", "vbot", "strategy", "configs")

with open(opt_file) as f:
    opt = json.load(f)

portfolio_files = opt.get("optimal_portfolio", [])
if not portfolio_files:
    print("Kein Portfolio in optimization_results.json gefunden.")
    sys.exit(0)

strategies = []
for fname in portfolio_files:
    cfg_path = os.path.join(configs_dir, fname)
    if not os.path.exists(cfg_path):
        continue
    with open(cfg_path) as f:
        cfg = json.load(f)
    market = cfg.get("market", {})
    risk   = cfg.get("risk",   {})
    strategies.append({
        "symbol":             market.get("symbol", ""),
        "timeframe":          market.get("timeframe", ""),
        "leverage":           risk.get("leverage", 10),
        "margin_mode":        risk.get("margin_mode", "isolated"),
        "risk_per_trade_pct": risk.get("risk_per_trade_pct", 1.0),
        "active":             True,
    })

with open(settings_file) as f:
    settings = json.load(f)

settings.setdefault("live_trading_settings", {})["active_strategies"] = strategies

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)

print(f"settings.json aktualisiert mit {len(strategies)} Strategie(n):")
for s in strategies:
    print(f"  {s['symbol']} ({s['timeframe']})  lev={s['leverage']}x  risk={s['risk_per_trade_pct']}%")
PYEOF
        else
            echo -e "${GREEN}settings.json wurde nicht geaendert.${NC}"
        fi
    fi
fi

# ─────────────────────────────────────────
# Modus 4: Interaktive Charts
# ─────────────────────────────────────────
elif [ "$MODE" == "4" ]; then
    $PYTHON src/vbot/analysis/show_results.py --mode 4

fi

deactivate
