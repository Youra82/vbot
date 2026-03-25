# src/vbot/analysis/portfolio_simulator.py
# Chronologische Portfolio-Simulation fuer mehrere vbot-Strategien.
#
# Verhält sich wie der Live-Bot (Global State):
#   - Gemeinsamer Kapital-Topf fuer alle Strategien
#   - Nur EINE Position gleichzeitig aktiv (global)
#   - Wenn eine Strategie im Trade ist, warten alle anderen
#   - Erste Strategie mit gueltigem Signal bekommt den naechsten Trade

import os
import sys
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

FEE_PCT      = 0.06 / 100
MIN_NOTIONAL = 5.0


def run_portfolio_simulation(start_capital: float,
                              strategies_data: dict,
                              start_date: str,
                              end_date: str) -> dict | None:
    """
    Portfolio-Simulation mit Global-State-Modell (wie Live-Bot).

    - Gemeinsamer Kapital-Topf
    - Max. 1 offene Position gleichzeitig (ueber alle Strategien)
    - Erste Strategie mit Signal bekommt den Trade

    strategies_data: {
        filename: {
            'symbol':    str,
            'timeframe': str,
            'df':        pd.DataFrame  (OHLCV)
            'config':    dict
        }
    }
    """
    from vbot.strategy.fibo_logic import get_fibo_signal

    processed = {}
    for fname, strat in strategies_data.items():
        df = strat.get('df')
        if df is None or df.empty:
            continue
        processed[fname] = {
            'symbol':    strat['symbol'],
            'timeframe': strat['timeframe'],
            'df':        df,
            'config':    strat['config'],
        }

    if not processed:
        return None

    # Precompute signals fuer jede Strategie
    for fname, strat in processed.items():
        df      = strat['df']
        cfg     = strat['config']
        sig_cfg = cfg.get('signal', {})

        # Gleicher Warmup wie Backtester: max(5, confirm_overlap_window + 3)
        confirm_window = int(sig_cfg.get('confirm_overlap_window', 0))
        warmup         = max(5, confirm_window + 3)

        none_sig = {'side': None, 'entry_price': None, 'sl_price': None,
                    'tp_price': None, 'fibo_level': None}

        signals = [none_sig] * warmup
        for i in range(warmup, len(df)):
            sig = get_fibo_signal(df.iloc[:i], sig_cfg)
            signals.append({
                'side':        sig['side'],
                'entry_price': sig.get('entry_price'),
                'sl_price':    sig.get('sl_price'),
                'tp_price':    sig.get('tp_price'),
                'fibo_level':  sig.get('fibo_level'),
            })
        strat['signals'] = signals

    # Gemeinsamen Zeitstrahl aufbauen
    all_ts: set = set()
    for strat in processed.values():
        all_ts.update(strat['df'].index)
    sorted_ts = sorted(all_ts)

    # Simulation — Global State: max. 1 Position gleichzeitig
    equity        = float(start_capital)
    peak_equity   = equity
    max_dd_pct    = 0.0
    equity_curve  = []
    wins = losses = 0
    open_position = None   # Nur EIN aktives Trade-Dict global
    trade_history = []

    for ts in sorted_ts:
        # 1. Offene Position checken
        if open_position is not None:
            pos   = open_position
            fname = pos['fname']
            strat = processed[fname]
            df    = strat['df']

            if ts in df.index:
                row  = df.loc[ts]
                high = float(row['high'])
                low  = float(row['low'])

                hit_sl = hit_tp = False
                if pos['direction'] == 'long':
                    if low <= pos['sl']:
                        hit_sl, exit_p = True, pos['sl']
                    elif high >= pos['tp']:
                        hit_tp, exit_p = True, pos['tp']
                else:
                    if high >= pos['sl']:
                        hit_sl, exit_p = True, pos['sl']
                    elif low <= pos['tp']:
                        hit_tp, exit_p = True, pos['tp']

                if hit_sl or hit_tp:
                    price_diff = exit_p - pos['entry']
                    if pos['direction'] == 'short':
                        price_diff = -price_diff
                    notional  = pos['contracts'] * pos['entry']
                    fees      = notional * FEE_PCT * 2
                    pnl_usdt  = price_diff * pos['contracts'] * pos['leverage'] - fees
                    equity   += pnl_usdt
                    if hit_tp:
                        wins += 1
                    else:
                        losses += 1
                    trade_history.append({
                        'ts':         pos['ts_open'],
                        'fname':      fname,
                        'direction':  pos['direction'],
                        'entry':      pos['entry'],
                        'exit':       exit_p,
                        'pnl':        pnl_usdt,
                        'fibo_level': pos.get('fibo_level'),
                    })
                    open_position = None

                    if equity <= 0:
                        break

        # 2. Neues Signal suchen (nur wenn kein Trade offen)
        if open_position is None:
            for fname, strat in processed.items():
                df = strat['df']
                if ts not in df.index:
                    continue
                idx = df.index.get_loc(ts)
                if idx >= len(strat['signals']):
                    continue
                sig = strat['signals'][idx]
                if sig['side'] is None:
                    continue

                cfg      = strat['config']
                risk_cfg = cfg.get('risk', {})
                leverage = int(risk_cfg.get('leverage', 10))
                risk_pct = float(risk_cfg.get('risk_per_trade_pct', 1.0))

                entry_price = float(df.loc[ts, 'open'])
                sl_price    = sig['sl_price']
                tp_price    = sig['tp_price']

                sl_dist = abs(entry_price - sl_price)
                if sl_dist <= 0:
                    continue

                risk_amount = equity * risk_pct / 100.0
                contracts   = risk_amount / sl_dist
                notional    = contracts * entry_price

                if notional < MIN_NOTIONAL:
                    continue

                open_position = {
                    'fname':      fname,
                    'direction':  sig['side'],
                    'entry':      entry_price,
                    'sl':         sl_price,
                    'tp':         tp_price,
                    'contracts':  contracts,
                    'leverage':   leverage,
                    'ts_open':    ts,
                    'fibo_level': sig.get('fibo_level'),
                }
                break   # Nur erste Strategie mit Signal bekommt den Trade

        # 3. Equity tracken
        equity_curve.append({'timestamp': ts, 'equity': equity})
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    total_trades = wins + losses
    win_rate     = wins / total_trades * 100 if total_trades else 0.0
    pnl_pct      = (equity - start_capital) / start_capital * 100

    return {
        'end_capital':      round(equity, 2),
        'total_pnl_pct':    round(pnl_pct, 2),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'trade_count':      total_trades,
        'wins':             wins,
        'losses':           losses,
        'win_rate':         round(win_rate, 2),
        'equity_curve':     equity_curve,
        'trade_history':    trade_history,
    }
