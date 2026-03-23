#!/bin/bash
# vbot — Parameter-Optimierungs-Pipeline
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}======================================================="
echo "       vbot — Fibonacci Candle Overlap Pipeline"
echo -e "=======================================================${NC}"

VENV_PATH=".venv/bin/activate"
PYTHON=".venv/bin/python3"
OPTIMIZER="src/vbot/analysis/optimizer.py"

if [ ! -f "$VENV_PATH" ]; then
    echo -e "${RED}Fehler: Virtuelle Umgebung nicht gefunden. Bitte install.sh ausfuehren.${NC}"
    exit 1
fi
source "$VENV_PATH"
echo -e "${GREEN}Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

# --- Aufraeumen ---
echo ""
echo -e "${YELLOW}Moechtest du alle alten, generierten Configs vor dem Start loeschen?${NC}"
read -p "Dies wird fuer einen kompletten Neustart empfohlen. (j/n) [Standard: n]: " CLEANUP_CHOICE
CLEANUP_CHOICE="${CLEANUP_CHOICE:-n}"
if [[ "$CLEANUP_CHOICE" == "j" || "$CLEANUP_CHOICE" == "J" ]]; then
    echo -e "${YELLOW}Loesche alte Konfigurationen...${NC}"
    rm -f src/vbot/strategy/configs/config_*.json
    echo -e "${GREEN}Aufraeumen abgeschlossen.${NC}"
else
    echo -e "${GREEN}Alte Ergebnisse werden beibehalten.${NC}"
fi

# --- Interaktive Abfrage ---
read -p "Handelspaar(e) eingeben (ohne /USDT, z.B. BTC ETH): " SYMBOLS
read -p "Zeitfenster eingeben (z.B. 1h 4h): " TIMEFRAMES

echo -e "\n${BLUE}--- Empfehlung: Optimaler Rueckblick-Zeitraum ---${NC}"
printf "+-------------+--------------------------------+\n"
printf "| Zeitfenster | Empfohlener Rueckblick (Tage)  |\n"
printf "+-------------+--------------------------------+\n"
printf "| 5m, 15m     | 15 - 90 Tage                   |\n"
printf "| 30m, 1h     | 180 - 365 Tage                 |\n"
printf "| 2h, 4h      | 550 - 730 Tage                 |\n"
printf "| 6h, 1d      | 1095 - 1825 Tage               |\n"
printf "+-------------+--------------------------------+\n"
read -p "Startdatum (JJJJ-MM-TT) oder 'a' fuer Automatik [Standard: a]: " START_DATE_INPUT
START_DATE_INPUT="${START_DATE_INPUT:-a}"
read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
END_DATE="${END_DATE:-$(date +%F)}"
read -p "Startkapital in USDT [Standard: 1000]: " START_CAPITAL
START_CAPITAL="${START_CAPITAL:-1000}"
read -p "Anzahl Trials [Standard: 200]: " N_TRIALS
N_TRIALS="${N_TRIALS:-200}"
read -p "CPU-Kerne [Standard: 1]: " N_JOBS
N_JOBS="${N_JOBS:-1}"

echo -e "\n${YELLOW}Waehle einen Optimierungs-Modus:${NC}"
echo "  1) Strenger Modus (Profitabel & Sicher)"
echo "  2) 'Finde das Beste'-Modus (Max Profit)"
read -p "Auswahl (1-2) [Standard: 1]: " OPTIM_MODE
OPTIM_MODE="${OPTIM_MODE:-1}"

read -p "Max Drawdown % [Standard: 30]: " MAX_DD
MAX_DD="${MAX_DD:-30}"

if [ "$OPTIM_MODE" == "1" ]; then
    read -p "Min Win-Rate % [Standard: 45]: " MIN_WR
    MIN_WR="${MIN_WR:-45}"
else
    MIN_WR=0
fi

for symbol in $SYMBOLS; do
    for timeframe in $TIMEFRAMES; do

        # --- Datumsberechnung ---
        if [ "$START_DATE_INPUT" == "a" ]; then
            lookback_days=365
            case "$timeframe" in
                5m|15m) lookback_days=60 ;;
                30m|1h) lookback_days=365 ;;
                2h|4h)  lookback_days=730 ;;
                6h|1d)  lookback_days=1095 ;;
            esac
            FINAL_START_DATE=$(date -d "$lookback_days days ago" +%F)
            echo -e "${YELLOW}INFO: Automatisches Startdatum fuer $timeframe (${lookback_days} Tage Rueckblick): $FINAL_START_DATE${NC}"
        else
            FINAL_START_DATE="$START_DATE_INPUT"
        fi

        echo -e "\n${BLUE}=======================================================${NC}"
        echo -e "${BLUE}  Bearbeite Pipeline fuer: $symbol ($timeframe)${NC}"
        echo -e "${BLUE}  Datenzeitraum: $FINAL_START_DATE bis $END_DATE${NC}"
        echo -e "${BLUE}=======================================================${NC}"

        echo -e "\n${GREEN}>>> Starte vbot Fibonacci-Optimierung fuer $symbol ($timeframe)...${NC}"
        $PYTHON "$OPTIMIZER" \
            --symbols "$symbol" \
            --timeframes "$timeframe" \
            --from "$FINAL_START_DATE" \
            --to "$END_DATE" \
            --capital "$START_CAPITAL" \
            --trials "$N_TRIALS" \
            --jobs "$N_JOBS" \
            --max-dd "$MAX_DD" \
            --min-wr "$MIN_WR"

        if [ $? -ne 0 ]; then
            echo -e "${RED}Fehler im Optimierer fuer $symbol ($timeframe). Ueberspringe...${NC}"
        fi
    done
done

deactivate
echo -e "\n${BLUE}Alle Pipeline-Aufgaben erfolgreich abgeschlossen!${NC}"
