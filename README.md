# vbot — Fibonacci Candle Overlap Bot

Ein technischer Trading-Bot, der das **Fibonacci Candle Overlap**-Prinzip automatisiert:
Nach jeder vollständig ausgebildeten Kerze wird ein Fibonacci-Retracement innerhalb dieser Kerze berechnet,
das voraussagt, wie weit sich die neue Kerze in die vorherige überlappen (zurücksetzen) wird.

Kein maschinelles Lernen — die Handelsregel ist fest (Fibonacci-Überlagerung).
Aber: jeder Coin und jeder Timeframe verhält sich anders, deshalb werden die Parameter
(Fibo-Level, Filter-Schwellen, SL-Buffer) per Optuna-Optimierung auf historischen Daten
**individuell pro Symbol/Timeframe kalibriert**. Die Pipeline (`run_pipeline.sh`) ist dafür nötig.

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

### Schritt 4 — Entry (Trigger-Limit)

```
Reihenfolge der Order-Platzierung (ltbbot-Stil):
  1. SL-Trigger platzieren  (reduceOnly) ← zuerst, schützt immer
  2. TP-Trigger platzieren  (reduceOnly) ← danach
  3. Entry Trigger-Limit    (kein reduceOnly) ← zuletzt

Entry:   Trigger-Limit am Close der Vorkerze
         SHORT: trigger = close × 1.0001  (feuert beim ersten Tick)
         LONG:  trigger = close × 0.9999
SL:      Trigger-Market jenseits des Kerzenextrems + Buffer
TP:      Trigger-Market am Fibonacci-Überlagerungslevel
```

**Candle-Timeout:** Wenn der Entry-Trigger nach **einer vollen Kerzenperiode**
nicht gefeuert hat (Kerze hat sich nicht überlagert), werden alle offenen Orders
automatisch storniert und der State geleert. Laufende Trades (Position offen)
werden dabei **nie angetastet**.

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
    "max_open_positions": 1,
    "active_strategies": [
      {
        "symbol": "BTC/USDT:USDT",
        "timeframe": "4h",
        "leverage": 2,
        "margin_mode": "isolated",
        "risk_per_trade_pct": 0.5,
        "active": true
      }
    ]
  },
  "optimization_settings": {
    "enabled": true,
    "schedule": {
      "day_of_week": 6,
      "hour": 15,
      "minute": 0,
      "interval": {
        "value": 7,
        "unit": "days"
      }
    },
    "symbols_to_optimize": "auto",
    "timeframes_to_optimize": "auto",
    "lookback_days": "auto",
    "start_capital": 15,
    "cpu_cores": 1,
    "num_trials": 150,
    "constraints": {
      "max_drawdown_pct": 30,
      "min_win_rate_pct": 45,
      "min_pnl_pct": 50,
      "max_rr": 10
    }
  }
}
```

| Feld | Erklärung |
|---|---|
| `enabled` | Auto-Optimizer ein/ausschalten |
| `day_of_week` | Wochentag (0=Montag, 6=Sonntag) |
| `hour` / `minute` | Uhrzeit für geplanten Lauf |
| `interval.value/unit` | Mindestabstand zwischen Optimierungen |
| `symbols_to_optimize` | `"auto"` = aus `active_strategies` lesen, oder Liste z.B. `["BTC", "ETH"]` |
| `timeframes_to_optimize` | `"auto"` = aus `active_strategies` lesen, oder Liste z.B. `["4h", "1d"]` |
| `lookback_days` | `"auto"` = timeframe-abhängig (1h→365, 4h→730, 1d→1095) |
| `start_capital` | Startkapital für Backtest-Simulation |
| `cpu_cores` | Anzahl paralleler Optuna-Jobs (1 = sicher) |
| `num_trials` | Anzahl Optuna-Trials pro Symbol/Timeframe |
| `constraints.max_drawdown_pct` | Max. erlaubter Drawdown |
| `constraints.min_win_rate_pct` | Mindest-Win-Rate |
| `constraints.min_pnl_pct` | Mindest-PnL für gültige Config |
| `constraints.max_rr` | Max. R:R-Verhältnis (verhindert unrealistische Werte) |

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
  "vbot": [
    {
      "apiKey": "...",
      "secret": "...",
      "password": "..."
    }
  ],
  "telegram": {
    "bot_token": "...",
    "chat_id": "..."
  }
}
```

> Nie in Git committen! `secret.json` ist in `.gitignore` eingetragen.

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

### Ablauf

```
╔══════════════════════════════════════════════════════╗
║         vbot — Fibonacci Candle Overlap Pipeline     ║
╚══════════════════════════════════════════════════════╝

1. Coins eingeben       z.B.: BTC ETH XRP SOL
2. Timeframe wählen     z.B.: 1h  4h  1d
3. Zeitraum             'a' für Automatik  oder  JJJJ-MM-TT
4. Startkapital         z.B.: 1000 USDT
5. Anzahl Trials        z.B.: 200  (mehr = gründlicher, langsamer)
6. CPU-Kerne            1 = sicher, -1 = alle Kerne
7. Modus wählen:
     1) Streng   — DD-Limit harte Grenze, WR-Mindest möglich
     2) Max-Profit — Optimizer findet das globale Maximum
```

### Was der Optimizer tut

Der Optimizer probiert mit **Optuna (TPE-Sampler)** systematisch Parameterkombinationen
aus und backtestet jede auf historischen Daten:

| Parameter | Beschreibung | Bereich |
|---|---|---|
| `fibo_tp_level` | Fibonacci-Level für TP | 0.236 / 0.382 / 0.5 / 0.618 / 0.786 |
| `min_candle_body_pct` | Qualitätsfilter — filtert Doji/Spinning Tops | 0.1–0.7 |
| `min_candle_range_pct` | Mindest-Kerzengröße (% des Preises) | 0.1–1.0% |
| `sl_buffer_pct` | SL-Puffer jenseits des Kerzenextrems | 0.05–0.5 |
| `confirm_overlap_window` | Optionaler Trendfilter (N Kerzen zurück) | 0–5 |
| `leverage` | Hebel — kapital- und DD-adaptiv | 2–20x |
| `risk_per_trade_pct` | Risiko pro Trade (% des Kapitals) | 0.5–8.0% |

### Anti-Overfitting: Walk-Forward Validation (WFV)

Da jeder Coin und Timeframe sich anders verhält, besteht die Gefahr dass der Optimizer
Parameter findet, die **nur in der Vergangenheit** funktioniert haben (Overfitting).
Der vbot bekämpft das mit mehreren Maßnahmen:

```
Datensatz (z.B. 1084 Kerzen)
│
├── Training   70%  (758 Kerzen)  → Optimizer findet Parameter
└── Test (OOS) 30%  (326 Kerzen)  → Unsichtbare Validierung
```

**Regeln:**
- Out-of-Sample (OOS) muss profitabel sein — sonst wird der Trial verworfen
- **Score = 30% Training + 70% OOS** — der Optimizer bevorzugt robuste Parameter
- PnL-Wert ist **logarithmiert** (`log1p`) — verhindert dass Millionen-% den Score dominieren
- R:R im Score ist auf **1:20 gedeckelt** — unrealistische Verhältnisse (1:31 etc.) bringen keinen Vorteil
- Mindest-Trades erhöht (1d: 20 Trades) — statistische Signifikanz

**Ausgabe nach Optimierung:**
```
Gesamt:  PnL=+834.57%  WR=41.3%  Trades=690  MaxDD=19.31%  Avg R:R 1:4.2
OOS:     PnL=+124.33%  WR=40.1%  Trades=198  MaxDD=11.20%
         └── Das ist der wichtigste Wert: war der Parameter-Set auch auf
             ungesehenen Daten profitabel?
```

> **Faustregel:** OOS-PnL ≥ 20% des Gesamt-PnL → Parameter wahrscheinlich robust.
> OOS-PnL nahe 0 oder negativ → Vorsicht, möglicherweise noch overfitted.

### Ergebnis

```
src/vbot/strategy/configs/config_BTCUSDTUSDT_1d_fibo.json
```

Eine Config wird **nur überschrieben wenn das neue Ergebnis besser ist** als das bestehende.
Das schützt vor Verschlechterung bei wiederholten Optimierungsläufen.

---

## Ergebnisse analysieren

```bash
./show_results.sh
```

### Modus 1 — Einzel-Analyse

Alle Configs werden **isoliert** getestet. Zeigt Tabelle mit PnL, Win-Rate,
Max-Drawdown, R:R, genutztem Fibo-Level und Endkapital:

```
  Strategie    Trades    WR %    PnL %  MaxDD %    R:R   Fibo    Endkapital
  BTC/1d          269    42.4  2532.29    10.70   3.58  0.618       658.07 USDT
  XRP/1d          140    45.7   817.78     4.20   2.69  0.786       229.44 USDT
  ETH/1d          222    37.8   802.33     6.15   1.94  0.786       225.58 USDT
```

### Modus 2 — Manuelle Portfolio-Simulation

Du wählst eine Kombination aus Configs — der Bot simuliert sie als **chronologisches
gemeinsames Portfolio** (geteiltes Kapital, simultane Trades):

```
Verfügbare Configs:
  1) config_BTCUSDTUSDT_1d_fibo.json
  2) config_ETHUSDTUSDT_1d_fibo.json
  3) config_XRPUSDTUSDT_1d_fibo.json
  ...
Strategien wählen (z.B. '1 3' oder 'alle'):
```

Ausgabe: Portfolio-PnL, Max-Drawdown, Win-Rate, Endkapital.

### Modus 3 — Automatische Portfolio-Optimierung

Der Bot sucht **selbst** das beste Portfolio via Greedy-Algorithmus:
- Sortiert alle Einzelstrategien nach PnL
- Fügt Strategie für Strategie hinzu (keine Coin-Kollisionen: BTC/1h + BTC/4h = blockiert)
- Prüft nach jedem Add: Portfolio-DD ≤ `--target-max-dd`
- Speichert Ergebnis → `artifacts/results/optimization_results.json`
- Optionales Update von `settings.json` mit dem optimalen Portfolio

### Modus 4 — Interaktive Charts

Erstellt einen interaktiven **Equity-Chart** des Portfolios (HTML) und eine
**Trade-Tabelle** (Excel) mit allen Einstiegen, Ausstiegen, PnL je Trade.
Kann optional via Telegram gesendet werden.

```
artifacts/charts/vbot_portfolio_equity.html
artifacts/charts/vbot_trades.xlsx
```

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
3. Sendet Telegram-Nachricht: Optimierung gestartet
4. Führt `optimizer.py` für alle Symbol/Timeframe-Paare aus
5. Config wird **nur überschrieben wenn das neue Ergebnis besser ist** — bestehende Configs bleiben erhalten
6. Sendet Telegram-Summary mit PnL-Vergleich je Paar (neu vs. alt)

### Manuell auslösen

```bash
# Scheduler direkt starten (prüft ob fällig, hält sich an enabled + Schedule)
.venv/bin/python3 auto_optimizer_scheduler.py

# Sofort erzwingen — ignoriert enabled und Schedule (für Tests)
.venv/bin/python3 auto_optimizer_scheduler.py --force
```

`--force` überspringt den `enabled`-Check und den Zeitplan-Check.
Nützlich um nach einer Konfigurationsänderung direkt zu testen, ob der Ablauf korrekt funktioniert.

---

## Tests

```bash
./run_tests.sh
```

Ausgeführte Tests:
- `test_fibo_signal_bullish_candle` — Bullische Kerze → SHORT Signal
- `test_fibo_signal_bearish_candle` — Bearische Kerze → LONG Signal
- `test_fibo_signal_doji_filtered` — Doji-Kerze erzeugt kein Signal
- `test_fibo_all_levels` — Alle Fibonacci-Level korrekt berechnet
- `test_backtest_runs` — Backtester läuft fehlerfrei durch
- `test_place_entry_on_bitget` — Echter Trade auf Bitget: PEPE/USDT:USDT, Isolated, SL+TP+Entry platziert und sofort geschlossen (benötigt `secret.json`)

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
