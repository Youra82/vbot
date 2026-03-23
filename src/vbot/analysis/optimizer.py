# src/vbot/analysis/optimizer.py
# vbot — Parameter-Optimierung per Optuna
# Findet die besten Fibonacci-Candle-Overlap-Parameter fuer ein Symbol/Timeframe

import os
import sys
import json
import logging
import argparse
import warnings
import math
from datetime import date

import pandas as pd

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    print("FEHLER: optuna nicht installiert. Bitte: pip install optuna")
    sys.exit(1)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from vbot.analysis.backtester import run_backtest, load_ohlcv, auto_days_for_timeframe

logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
logging.getLogger('optuna').setLevel(logging.WARNING)
warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'vbot', 'strategy', 'configs')

# Minimale Trades pro Timeframe
_TF_MIN_TRADES = {
    "1m": 150, "3m": 120, "5m": 100, "15m": 80,
    "30m": 60, "1h": 40, "2h": 30,
    "4h": 20, "6h": 15, "8h": 15, "12h": 10,
    "1d": 8, "3d": 6, "1w": 4,
}


def _min_trades(timeframe: str) -> int:
    return _TF_MIN_TRADES.get(timeframe, 5)


# ---------------------------------------------------------------------------
# Kapital- und DD-adaptive Parameter-Ranges
# ---------------------------------------------------------------------------

def _max_eff_risk_from_dd(max_dd: float, k: int = 30) -> float:
    """
    Berechnet das maximale effektive Risiko pro Trade aus dem gewuenschten max_dd.

    Formel: nach k aufeinanderfolgenden Verlusten soll Drawdown <= max_dd bleiben.
      (1 - eff/100)^k >= 1 - max_dd/100
      eff <= (1 - (1 - max_dd/100)^(1/k)) * 100

    k=30: Fibonacci Candle Overlap hat typisch WR ~45-65%.
    Konservative Annahme: 30 Verluste in Folge.
    """
    survival = 1.0 - max_dd / 100.0
    if survival <= 0:
        return 100.0
    return (1.0 - survival ** (1.0 / k)) * 100.0


def _get_capital_ranges(capital: float, max_dd: float = 30.0) -> dict:
    """
    Gibt Optimierungs-Ranges zurueck, abhaengig von Kapital und max_dd.
    max_effective_risk wird mathematisch aus max_dd abgeleitet.
    """
    max_eff_risk = _max_eff_risk_from_dd(max_dd)

    if capital < 50:
        return {
            "risk_per_trade_pct": (0.5, 8.0, 0.5),
            "leverage":           (2, 20),
            "max_effective_risk": max_eff_risk,
        }
    elif capital < 200:
        return {
            "risk_per_trade_pct": (0.5, 5.0, 0.5),
            "leverage":           (2, 20),
            "max_effective_risk": max_eff_risk,
        }
    else:
        return {
            "risk_per_trade_pct": (0.5, 3.0, 0.1),
            "leverage":           (2, 20),
            "max_effective_risk": max_eff_risk,
        }


# ---------------------------------------------------------------------------
# Objective fuer Optuna
# ---------------------------------------------------------------------------

def _make_objective(df, symbol, timeframe, capital, max_dd, min_wr, _stats: list):
    ranges     = _get_capital_ranges(capital, max_dd)
    r_min, r_max, r_step = ranges["risk_per_trade_pct"]
    lev_min, lev_max     = ranges["leverage"]
    max_eff_risk         = ranges["max_effective_risk"]
    min_trades_needed    = _min_trades(timeframe)

    lev_max_safe = max(lev_min, int(max_eff_risk / r_min))
    lev_max      = min(lev_max, lev_max_safe)

    def _objective(trial: optuna.Trial) -> float:
        leverage = trial.suggest_int("leverage", lev_min, lev_max)
        risk_pct_max = min(r_max, max(r_min, max_eff_risk / leverage))
        risk_pct     = trial.suggest_float("risk_per_trade_pct", r_min, risk_pct_max, step=r_step)

        config = {
            "market": {"symbol": symbol, "timeframe": timeframe},
            "signal": {
                # Fibonacci TP-Level: welches Retracement-Niveau wird als TP angesteuert
                "fibo_tp_level":         trial.suggest_categorical(
                    "fibo_tp_level", [0.236, 0.382, 0.5, 0.618, 0.786]
                ),
                # Kerzenkörper-Filter (Doji-Filterung)
                "min_candle_body_pct":   trial.suggest_float(
                    "min_candle_body_pct", 0.1, 0.7, step=0.1
                ),
                # Mindest-Kerzrange relativ zum Preis
                "min_candle_range_pct":  trial.suggest_float(
                    "min_candle_range_pct", 0.1, 1.0, step=0.1
                ),
                # SL-Buffer ueber/unter Kerzenhoch/-tief
                "sl_buffer_pct":         trial.suggest_float(
                    "sl_buffer_pct", 0.05, 0.5, step=0.05
                ),
                # Trend-Bestaetigung: Anzahl Kerzen zurueck (0 = deaktiviert)
                "confirm_overlap_window": trial.suggest_int(
                    "confirm_overlap_window", 0, 5
                ),
            },
            "risk": {
                "risk_per_trade_pct": risk_pct,
                "leverage":           leverage,
                "margin_mode":        "isolated",
            }
        }

        try:
            result = run_backtest(df, config, capital, symbol, timeframe)
        except Exception:
            return -999.0

        # Diagnose: max Trades tracken
        if result.total_trades > _stats[0]:
            _stats[0] = result.total_trades

        if result.total_trades < min_trades_needed:
            _stats[2] += 1
            return -999.0
        if result.max_drawdown_pct > max_dd:
            _stats[3] += 1
            if result.max_drawdown_pct < _stats[4]:
                _stats[4] = result.max_drawdown_pct
            return -999.0
        if result.win_rate < min_wr:
            return -999.0

        # Score: Profit + R:R-Qualitaet + Trade-Haeufigkeit
        trade_bonus = math.log1p(result.total_trades) * 10.0
        return result.pnl_pct + result.avg_rr * 5.0 + trade_bonus

    return _objective


# ---------------------------------------------------------------------------
# Haupt-Optimierungsfunktion
# ---------------------------------------------------------------------------

def optimize(symbol: str, timeframe: str,
             start_date: str, end_date: str,
             capital: float = 1000.0,
             n_trials: int = 200,
             max_dd: float = 30.0,
             min_wr: float = 0.0,
             n_jobs: int = 1) -> dict | None:
    """
    Laedt Daten, optimiert Parameter mit Optuna und gibt die beste Config zurueck.
    """
    ranges = _get_capital_ranges(capital, max_dd)
    r      = ranges["risk_per_trade_pct"]
    l      = ranges["leverage"]
    m      = ranges["max_effective_risk"]
    print(f"\n  Parameter-Ranges (Kapital: {capital:.0f} USDT, Max-DD: {max_dd:.0f}%):")
    print(f"    risk_per_trade_pct : {r[0]:.1f} - {r[1]:.1f}%")
    print(f"    leverage           : {l[0]} - {l[1]}x")
    print(f"    max effective risk : {m:.1f}%  (aus max_dd={max_dd:.0f}%: nach ~30 Verlusten <= {max_dd:.0f}% DD)")
    print(f"    min trades         : {_min_trades(timeframe)}  (Timeframe: {timeframe})")

    print(f"\n  Lade Daten: {symbol} ({timeframe}) [{start_date} -> {end_date}]")
    df = load_ohlcv(symbol, timeframe, start_date, end_date)
    if df.empty or len(df) < 50:
        print(f"  FEHLER: Nicht genug Daten ({len(df)} Kerzen). Uebersprungen.")
        return None
    print(f"  {len(df)} Kerzen geladen.")

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(),
    )

    # _stats: [max_trades_seen, reserved, n_too_few_trades, n_high_dd, best_dd_seen]
    _stats = [0, 0, 0, 0, float('inf')]
    objective = _make_objective(df, symbol, timeframe, capital, max_dd, min_wr, _stats)
    cores_str = "alle Kerne" if n_jobs == -1 else f"{n_jobs} Kern(e)"
    print(f"  Optimiere {n_trials} Trials ({cores_str})...")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True, n_jobs=n_jobs)

    best = study.best_trial
    if best.value <= -999.0:
        max_trades, _, n_few, n_dd, best_dd = _stats
        print(f"  WARNUNG: Kein gueltiges Ergebnis gefunden.")
        print(f"  Diagnose: zu wenige Trades: {n_few}  |  DD zu hoch: {n_dd}")
        if n_dd > 0 and best_dd < float('inf'):
            suggested_dd = int(best_dd) + 10
            print(f"  Bester erreichbarer DD: {best_dd:.1f}%  (Limit war: {max_dd:.0f}%)")
            print(f"  TIPP: --max-dd {suggested_dd} verwenden um Configs zu finden.")
        elif max_trades < _min_trades(timeframe):
            tf_map = {"1d": "4h", "4h": "1h", "1h": "30m", "6h": "2h"}
            alt_tf  = tf_map.get(timeframe, "kleinerer Timeframe")
            print(f"  TIPP: Strategie findet auf '{timeframe}' zu selten Signale "
                  f"(max. {max_trades} Trades, Minimum: {_min_trades(timeframe)}).")
            print(f"        Empfehlung: '{alt_tf}' verwenden (mehr Kerzen = mehr Setups).")
        return None

    print(f"  Bestes Ergebnis: Score={best.value:.2f}")

    params = best.params
    config = {
        "market": {"symbol": symbol, "timeframe": timeframe},
        "signal": {
            "fibo_tp_level":          params["fibo_tp_level"],
            "min_candle_body_pct":    round(params["min_candle_body_pct"], 2),
            "min_candle_range_pct":   round(params["min_candle_range_pct"], 2),
            "sl_buffer_pct":          round(params["sl_buffer_pct"], 3),
            "confirm_overlap_window": int(params["confirm_overlap_window"]),
        },
        "risk": {
            "leverage":           params["leverage"],
            "risk_per_trade_pct": round(params["risk_per_trade_pct"], 2),
            "margin_mode":        "isolated",
        }
    }

    # Backtest mit bester Config fuer finale Metriken
    try:
        result = run_backtest(df, config, capital, symbol, timeframe)
        config["_backtest"] = {
            "pnl_pct":      round(result.pnl_pct, 2),
            "win_rate":     round(result.win_rate, 1),
            "total_trades": result.total_trades,
            "max_drawdown": round(result.max_drawdown_pct, 2),
            "avg_rr":       round(result.avg_rr, 2),
            "start_date":   start_date,
            "end_date":     end_date,
            "capital":      capital,
        }
        print(f"  Backtest: PnL={result.pnl_pct:+.2f}%  WR={result.win_rate:.1f}%  "
              f"Trades={result.total_trades}  MaxDD={result.max_drawdown_pct:.2f}%  "
              f"Avg R:R 1:{result.avg_rr:.2f}")
    except Exception as e:
        logger.warning(f"Finale Backtest-Berechnung fehlgeschlagen: {e}")

    return config


def save_config(config: dict, symbol: str, timeframe: str) -> str | None:
    """Speichert Config nur wenn das neue Ergebnis besser ist als die bestehende."""
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    path = os.path.join(CONFIGS_DIR, f"config_{safe}_fibo.json")

    new_pnl = config.get('_backtest', {}).get('pnl_pct')

    if os.path.exists(path) and new_pnl is not None:
        try:
            with open(path) as f:
                existing = json.load(f)
            existing_pnl = existing.get('_backtest', {}).get('pnl_pct')
            if existing_pnl is not None and new_pnl <= existing_pnl:
                print(f"  Bestehende Config besser ({existing_pnl:.2f}% vs {new_pnl:.2f}%) "
                      f"— wird nicht ueberschrieben.")
                return None
        except Exception:
            pass

    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="vbot Optimizer — Fibonacci Candle Overlap")
    parser.add_argument('--symbols',    nargs='+', required=True,
                        help="Symbole (z.B. BTC ETH oder BTC/USDT:USDT)")
    parser.add_argument('--timeframes', nargs='+', required=True,
                        help="Timeframes (z.B. 1h 4h)")
    parser.add_argument('--from',  dest='date_from', default=None, metavar='YYYY-MM-DD')
    parser.add_argument('--to',    dest='date_to',   default=None, metavar='YYYY-MM-DD')
    parser.add_argument('--days',  type=int, default=None)
    parser.add_argument('--capital',  type=float, default=1000.0)
    parser.add_argument('--trials',   type=int,   default=200)
    parser.add_argument('--max-dd',   type=float, default=30.0,
                        help="Max erlaubter Drawdown %% (Standard: 30)")
    parser.add_argument('--min-wr',   type=float, default=0.0,
                        help="Min Win-Rate %% (Standard: 0)")
    parser.add_argument('--jobs',     type=int,   default=1,
                        help="CPU-Kerne fuer Parallelisierung (Standard: 1)")
    args = parser.parse_args()

    today = date.today().isoformat()

    GREEN  = '\033[0;32m'
    YELLOW = '\033[1;33m'
    RED    = '\033[0;31m'
    BOLD   = '\033[1m'
    NC     = '\033[0m'

    for raw_sym in args.symbols:
        if '/' not in raw_sym:
            symbol = f"{raw_sym.upper()}/USDT:USDT"
        else:
            symbol = raw_sym

        for timeframe in args.timeframes:
            if args.date_from:
                start_date = args.date_from
                end_date   = args.date_to if args.date_to else today
            else:
                n_days     = args.days if args.days else auto_days_for_timeframe(timeframe)
                end_date   = today
                start_date = (pd.Timestamp(today, tz='UTC') -
                              pd.Timedelta(days=n_days)).strftime('%Y-%m-%d')

            print(f"\n{BOLD}{'='*55}{NC}")
            print(f"{BOLD}Optimiere: {symbol} ({timeframe}){NC}")
            print(f"  Zeitraum: {start_date} -> {end_date}")
            print(f"  Trials:   {args.trials}  |  Kapital: {args.capital}  |  "
                  f"Max-DD: {args.max_dd}%  |  Min-WR: {args.min_wr}%")

            best_config = optimize(
                symbol, timeframe, start_date, end_date,
                capital=args.capital, n_trials=args.trials,
                max_dd=args.max_dd, min_wr=args.min_wr,
                n_jobs=args.jobs
            )

            if best_config is None:
                print(f"  {RED}Keine gueltige Config gefunden. Uebersprungen.{NC}")
                continue

            path = save_config(best_config, symbol, timeframe)
            bt = best_config.get("_backtest", {})
            if path is None:
                continue
            print(f"\n  {GREEN}Config gespeichert: {os.path.basename(path)}{NC}")
            if bt:
                color = GREEN if bt.get('pnl_pct', 0) >= 0 else RED
                print(f"  {color}PnL: {bt['pnl_pct']:+.2f}%{NC}  "
                      f"WR: {bt['win_rate']:.1f}%  "
                      f"Trades: {bt['total_trades']}  "
                      f"MaxDD: {bt['max_drawdown']:.2f}%  "
                      f"Avg R:R 1:{bt['avg_rr']:.2f}")

    print(f"\n{BOLD}Optimierung abgeschlossen.{NC}")
