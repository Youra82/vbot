# vbot — Fibonacci Candle Overlap Bot

Ein technischer Trading-Bot, der das **Fibonacci Candle Overlap**-Prinzip automatisiert:
Nach jeder vollständig ausgebildeten Kerze wird ein Fibonacci-Retracement innerhalb dieser Kerze berechnet,
das voraussagt, wie weit sich die neue Kerze in die vorherige überlappen (zurücksetzen) wird.
Kein maschinelles Lernen — reine Preisstruktur-Logik.

> **Disclaimer:** Diese Software ist experimentell und dient ausschließlich Forschungszwecken.
> Der Handel mit Kryptowährungen birgt erhebliche finanzielle Risiken. Nutzung auf eigene Gefahr.

---

## Grundidee

```
Schritt 1: Kerze schließt vollständig
   ┌──────────────────────────────────────────┐
   │   Vorherige Kerze (bullish, geschlossen) │
   │                                          │
   │  High ────────────────────── 100%        │
   │      │   ← Körper oben                  │
   │  Close ─────────────────────  78.6%      │
   │      │                                   │
   │      │   61.8% ← TP (Standard)           │
   │      │   50.0% ← TP (konservativ)        │
   │      │   38.2% ← TP (aggressiv)          │
   │      │                                   │
   │  Open ──────────────────────  23.6%      │
   │      │   ← Körper unten                  │
   │  Low ─────────────────────── 0%          │
   └──────────────────────────────────────────┘

Schritt 2: Neue Kerze öffnet — sie wird in die vorherige zurücksetzen
   → Bullische Vorkerze → SHORT-Trade (neue Kerze fällt zurück)
     Entry:  aktueller Open der neuen Kerze
     TP:     high − fibo_level × (high − low)
     SL:     oberhalb des vorherigen High

   → Bearische Vorkerze → LONG-Trade (neue Kerze steigt zurück)
     Entry:  aktueller Open der neuen Kerze
     TP:     low + fibo_level × (high − low)
     SL:     unterhalb des vorherigen Low
```

**Beispiel: BTC/USDT 4H (bullische Vorkerze)**

```
Vorherige Kerze: High = 87.800 | Low = 80.600
Fibonacci-Level: 0.618 (goldener Schnitt)

Entry:   87.200  (Open der neuen Kerze)
TP:      87.800 − 0.618 × (87.800 − 80.600) = 83.351
SL:      88.100  (über dem vorherigen High + Buffer)
R:R:     1:2.3

→ SHORT bis zur 61.8%-Überlagerung der Vorkerze
```

---

## Architektur

```
vbot/
├── master_runner.py                   # Cronjob-Orchestrator für Live-Trading
├── auto_optimizer_scheduler.py        # Auto-Optimierung im Hintergrund (Scheduler)
├── show_results.sh                    # Interaktives Analyse-Menü (3 Modi)
├── run_pipeline.sh                    # Optuna-Optimierung für neue Configs
├── push_configs.sh                    # Optimierte Configs ins Repo pushen
├── install.sh                         # Erstinstallation auf VPS
├── update.sh                          # Git-Update (sichert secret.json)
├── cron_setup.sh                      # Cron-Job einrichten
├── settings.json                      # Aktive Strategien + Auto-Optimizer-Einstellungen
├── secret.json                        # API-Keys (nicht in Git)
│
└── src/vbot/
    ├── strategy/
    │   ├── fibo_logic.py              # KERN: Fibonacci Candle Overlap Signal
    │   ├── run.py                     # Entry Point für eine Strategie
    │   └── configs/
    │       └── config_BTCUSDTUSDT_1h_fibo.json   # Parameter pro Symbol
    │
    ├── analysis/
    │   ├── backtester.py              # Walk-Forward Backtest
    │   ├── optimizer.py               # Optuna-Optimierung: findet beste Parameter
    │   ├── portfolio_simulator.py     # Chronologische Multi-Strategie-Simulation
    │   └── show_results.py            # Portfolio-Analyse & Backtest-Anzeige
    │
    └── utils/
        ├── exchange.py                # Bitget CCXT Wrapper
        ├── trade_manager.py           # Entry / TP / SL / Tracker
        ├── telegram.py                # Telegram-Benachrichtigungen
        └── guardian.py                # Exception-Wrapper
```

---

## Strategie im Detail

### Schritt 1 — Vorkerze analysieren

```
Nach jeder abgeschlossenen Kerze:
  → Berechne Kerzenbereich:  range = high − low
  → Körperanteil:            body  = |close − open| / range
  → Filter:
      min_candle_body_pct   ≥ 0.3  → keine Doji / Spinning Tops
      min_candle_range_pct  ≥ 0.3% → keine winzigen Kerzen
```

### Schritt 2 — Fibonacci Overlap berechnen

```
Bullische Vorkerze (close > open):
  → SHORT-Setup (neue Kerze setzt zurück)
  → TP  = high − fibo_level × range
  → SL  = high + sl_buffer_pct × range

Bearische Vorkerze (close < open):
  → LONG-Setup (neue Kerze setzt zurück)
  → TP  = low  + fibo_level × range
  → SL  = low  − sl_buffer_pct × range
```

### Schritt 3 — Optionaler Trendfilter

```
confirm_overlap_window > 0:
  → Schaut N Kerzen zurück
  → Bullish-Confirmation: letzte N Kerzen mehrheitlich grün
  → Bearish-Confirmation: letzte N Kerzen mehrheitlich rot
  → Signal nur wenn Trendrichtung mit dem erwarteten Overlap übereinstimmt
```

### Schritt 4 — Entry

```
Entry:  Open der aktuellen Kerze (live: Trigger-Limit 0.05%)
SL:     Jenseits des Kerzenextrems + Buffer
TP:     Fibonacci-Überlagerungslevel der Vorkerze
```

---

## Fibonacci-Levels Referenz

| Level | Ratio | Rolle im System |
|---|---|---|
| **23.6%** | **0.236** | **Aggressiver TP (große Überlagerung)** |
| **38.2%** | **0.382** | **TP Zone Anfang** |
| **50.0%** | **0.500** | **TP Zone Mitte** |
| **61.8%** | **0.618** | **Goldener Schnitt TP (Standard)** |
| **78.6%** | **0.786** | **Konservativer TP (tiefe Überlagerung)** |

> **Warum 0.618 als Standard?** Der goldene Schnitt hat historisch die höchste Trefferquote
> beim Candle-Overlap. Die meisten Kerzen retracen zwischen 50–78.6% in die Vorkerze.

---

## Konfiguration

### `settings.json` — Aktive Strategien & Auto-Optimizer

```json
{
  "live_trading_settings": {
    "active_strategies": [
      {
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "leverage": 10,
        "margin_mode": "isolated",
        "risk_per_trade_pct": 1.0,
        "active": true
      }
    ]
  },
  "optimization_settings": {
    "enabled": false,
    "schedule": {
      "day_of_week": 6,
      "hour": 3,
      "minute": 0,
      "interval": {
        "value": 7,
        "unit": "days"
      }
    },
    "start_capital": 1000,
    "max_drawdown_pct": 30,
    "min_win_rate_pct": 0,
    "lookback_days": "auto",
    "send_telegram_on_completion": true
  }
}
```

| Feld | Standard | Erklärung |
|---|---|---|
| `enabled` | `false` | Auto-Optimizer ein/ausschalten |
| `day_of_week` | `6` | Wochentag (0=Montag, 6=Sonntag) |
| `hour` / `minute` | `3` / `0` | Uhrzeit für geplanten Lauf |
| `interval.value/unit` | `7 days` | Mindestabstand zwischen Optimierungen |
| `start_capital` | `1000` | Startkapital für Simulation |
| `max_drawdown_pct` | `30` | Max. erlaubter Drawdown |
| `min_win_rate_pct` | `0` | Min. Win-Rate (0 = kein Limit) |
| `lookback_days` | `"auto"` | Historische Tage: `"auto"` = timeframe-abhängig (1h→365, 4h→730, 1d→1095) |
| `send_telegram_on_completion` | `true` | Telegram-Benachrichtigung nach Optimierung |

### `configs/config_BTCUSDTUSDT_1h_fibo.json` — Strategie-Parameter

```json
{
  "market": {
    "symbol": "BTC/USDT:USDT",
    "timeframe": "1h"
  },
  "signal": {
    "fibo_tp_level": 0.618,
    "min_candle_body_pct": 0.3,
    "min_candle_range_pct": 0.3,
    "sl_buffer_pct": 0.1,
    "confirm_overlap_window": 0
  },
  "risk": {
    "leverage": 10,
    "margin_mode": "isolated",
    "risk_per_trade_pct": 1.0
  }
}
```

| Parameter | Beschreibung |
|---|---|
| `fibo_tp_level` | Fibonacci-Level für TP (0.236 / 0.382 / 0.5 / 0.618 / 0.786) |
| `min_candle_body_pct` | Mindest-Körperanteil der Vorkerze (0.0–1.0) |
| `min_candle_range_pct` | Mindest-Kerzengröße in % des Preises |
| `sl_buffer_pct` | SL-Puffer jenseits des Kerzenextrems (als Anteil des range) |
| `confirm_overlap_window` | Trendfilter-Kerzen (0 = deaktiviert) |
| `leverage` | Hebel (1–125x) |
| `risk_per_trade_pct` | Risikoanteil pro Trade (% des Kapitals) |

### `secret.json` — API-Keys

```json
{
  "telegram": {
    "bot_token": "...",
    "chat_id": "..."
  },
  "vbot": {
    "api_key": "...",
    "api_secret": "...",
    "passphrase": "..."
  }
}
```

> Vorlage: `secret.json.template` — nie in Git committen!

---

## Installation

### Erstinstallation (VPS / lokal)

```bash
git clone https://github.com/Youra82/vbot.git
cd vbot
chmod +x *.sh
./install.sh
cp secret.json.template secret.json
nano secret.json   # API-Keys eintragen
```

### Cron-Job einrichten

```bash
./cron_setup.sh
```

Richtet einen stündlichen Cron-Job ein:
```
0 * * * * cd /pfad/zu/vbot && .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1
```

---

## Pipeline — Neue Strategien optimieren

```bash
./run_pipeline.sh
```

Interaktives Menü:

```
1. Symbol eingeben  (z.B. BTC/USDT:USDT)
2. Timeframe wählen (z.B. 1h, 4h, 1d)
3. Zeitraum festlegen (auto oder manuell)
4. Startkapital und Optuna-Trials
5. Modus: strict (DD ≤ 30%) oder max-profit
```

Der Optimizer testet folgende Parameter mit Optuna (TPE):
- `fibo_tp_level` — welches Fibonacci-Level als TP
- `min_candle_body_pct` — Qualitätsfilter für Vorkerzen
- `min_candle_range_pct` — Mindestgröße der Kerze
- `sl_buffer_pct` — SL-Puffer
- `confirm_overlap_window` — optionaler Trendfilter
- `leverage`, `risk_per_trade_pct` — Risiko-Parameter

Ergebnis: `src/vbot/strategy/configs/config_SYMBOL_TIMEFRAME_fibo.json`

---

## Ergebnisse analysieren

```bash
./show_results.sh
```

### Modus 1 — Einzel-Analyse

Alle Configs werden **isoliert** getestet. Zeigt Tabelle mit:

```
Strategie      Trades    WR %   PnL %   MaxDD %    R:R   Fibo
BTC/1h             42    54.7   +18.3     -12.4   1.92  0.618
ETH/4h             28    50.0   +11.2      -8.7   1.74  0.500
...
```

### Modus 2 — Manuelle Portfolio-Simulation

Du wählst eine Kombination aus Configs — der Bot simuliert sie als gemeinsames Portfolio:

```
Verfügbare Configs:
  1) config_BTCUSDTUSDT_1h_fibo.json
  2) config_ETHUSDTUSDT_4h_fibo.json
  ...
Strategien wählen (z.B. '1 3' oder 'alle'):
```

### Modus 3 — Automatische Portfolio-Optimierung

Der Bot sucht **selbst** das beste Portfolio via Greedy-Algorithmus:
- Sortiert alle Einzelstrategien nach PnL
- Fügt Strategie für Strategie hinzu (keine Coin-Kollisionen)
- Prüft nach jedem Add: Portfolio-DD ≤ `--target-max-dd`
- Speichert Ergebnis → `artifacts/results/optimization_results.json`
- Optionales Update von `settings.json` mit dem optimalen Portfolio

---

## Configs ins Repo pushen

```bash
./push_configs.sh
```

Staged alle `config_*_fibo.json` Dateien und pusht ins Repo (mit Timestamp-Commit).

---

## Updates einspielen

```bash
./update.sh
```

Sichert `secret.json`, macht `git reset --hard origin/main`, stellt `secret.json` wieder her.

---

## Live-Trading

Der `master_runner.py` wird stündlich via Cron ausgeführt:

1. Startet `auto_optimizer_scheduler.py` non-blocking im Hintergrund
2. Lädt aktive Strategien aus `settings.json`
3. Für jede aktive Strategie: `run.py --mode signal`
4. Prüft offene Positionen via globalem State (`artifacts/tracker/global_state.json`)

**Wichtig:** Nur eine Position ist gleichzeitig offen (single-position Model).

### Telegram-Benachrichtigungen

```
vBot Signal — BTC/USDT:USDT (1h)
Richtung   : SHORT
Entry      : 87.200,00
SL         : 88.100,00  (+1.03%)
TP         : 83.351,00  (-4.42%)
Fibo-Level : 0.618
Vorkerze   : High=87.800 | Low=80.600 | Bullish
R:R        : 1:4.29
```

---

## Auto-Optimierung

Aktivierung in `settings.json`:

```json
"optimization_settings": {
  "enabled": true,
  "schedule": {
    "day_of_week": 6,
    "hour": 3
  }
}
```

Der `auto_optimizer_scheduler.py`:
1. Prüft ob Optimierung fällig ist (Interval oder Wochentag/Uhrzeit)
2. Läuft non-blocking neben dem Live-Trading
3. Führt `optimizer.py` für alle Symbol/Timeframe-Paare aus
4. Startet Portfolio-Finder (`show_results.py --mode 3 --auto`)
5. Aktualisiert `settings.json` mit dem neuen Portfolio
6. Sendet Telegram-Summary

---

## Tests

```bash
./run_tests.sh
```

Ausgeführte Tests:
- `test_bullish_gives_short_signal` — Bullische Kerze → SHORT
- `test_bearish_gives_long_signal` — Bearische Kerze → LONG
- `test_doji_filtered_out` — Doji-Kerze erzeugt kein Signal
- `test_fibo_level_calculation` — Fibonacci-Level-Berechnung
- `test_backtester_runs` — Backtest läuft durch ohne Fehler
- `test_place_entry_orders_on_bitget` — Bitget-Order-Platzierung (benötigt `secret.json`)

---

## Projektstruktur

```
vbot/
├── src/vbot/
│   ├── strategy/
│   │   ├── fibo_logic.py          # Signal-Logik (Fibonacci Candle Overlap)
│   │   ├── run.py                 # Strategy Runner (signal / check Modus)
│   │   └── configs/               # Optimierte Config-Dateien (in Git)
│   ├── analysis/
│   │   ├── backtester.py          # Walk-Forward Backtester
│   │   ├── optimizer.py           # Optuna-Optimierung
│   │   ├── portfolio_simulator.py # Multi-Strategie Portfolio-Simulation
│   │   └── show_results.py        # Analyse-CLI (3 Modi)
│   └── utils/
│       ├── exchange.py            # Bitget CCXT (Swap/Futures)
│       ├── trade_manager.py       # Global State, Entry/TP/SL
│       ├── telegram.py            # Telegram Push
│       └── guardian.py            # Exception-Wrapper
├── artifacts/
│   ├── tracker/
│   │   └── global_state.json      # Aktive Position (nicht in Git)
│   └── results/
│       └── optimization_results.json  # Letzter Optimizer-Lauf (nicht in Git)
├── data/
│   └── cache/                     # OHLCV CSV-Cache (nicht in Git)
├── logs/                          # Laufzeit-Logs (nicht in Git)
├── tests/
│   └── test_workflow.py
├── master_runner.py
├── auto_optimizer_scheduler.py
├── run_pipeline.sh
├── show_results.sh
├── push_configs.sh
├── install.sh
├── update.sh
├── cron_setup.sh
├── settings.json
└── requirements.txt
```

---

## Verwandte Projekte

| Bot | Strategie |
|---|---|
| [fibot](https://github.com/Youra82/fibot) | Fibonacci Struktur (Swings, Wedge, Channel, RSI) |
| [mbot](https://github.com/Youra82/mbot) | Momentum Breakout (BB + Volume) |
| [dnabot](https://github.com/Youra82/dnabot) | Genome-basiert (Candle-Encoding + SQLite) |
| [dbot](https://github.com/Youra82/dbot) | LSTM Neural Network |
| **vbot** | **Fibonacci Candle Overlap** |
