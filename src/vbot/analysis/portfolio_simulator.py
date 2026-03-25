# src/vbot/analysis/portfolio_simulator.py
# Chronologische Portfolio-Simulation fuer mehrere vbot-Strategien.
#
# Kapital wird gleichmaessig auf die Strategien aufgeteilt.
# Jede Strategie laeuft unabhaengig auf ihrem eigenen Kapital-Slice.
# SL/TP werden bar-fuer-bar geprueft.

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
    Chronologische Portfolio-Simulation fuer mehrere vbot Fibonacci-Strategien.

    Kapital-Aufteilung: start_capital / n_strategien pro Strategie.
    Jede Strategie rechnet unabhaengig — kein Overflows durch Cross-Compounding.

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

    n_strats      = len(processed)
    capital_slice = start_capital / n_strats  # pro Strategie

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
        strat['signals']  = signals
        strat['equity']   = float(capital_slice)   # eigener Kapital-Topf
        strat['peak_eq']  = float(capital_slice)

    # Gemeinsamen Zeitstrahl aufbauen
    all_ts: set = set()
    for strat in processed.values():
        all_ts.update(strat['df'].index)
    sorted_ts = sorted(all_ts)

    # Simulation
    max_dd_pct     = 0.0
    equity_curve   = []
    wins = losses  = 0
    open_positions = {}   # fname -> position-dict
    trade_history  = []

    for ts in sorted_ts:
        # 1. Offene Positionen checken
        for fname in list(open_positions.keys()):
            strat = processed[fname]
            df    = strat['df']
            if ts not in df.index:
                continue
            pos  = open_positions[fname]
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

                strat['equity'] += pnl_usdt
                if strat['equity'] > strat['peak_eq']:
                    strat['peak_eq'] = strat['equity']

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
                del open_positions[fname]

        # 2. Neue Signale pruefen
        for fname, strat in processed.items():
            if fname in open_positions:
                continue
            if strat['equity'] <= 0:
                continue
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

            risk_amount = strat['equity'] * risk_pct / 100.0
            contracts   = risk_amount / sl_dist
            notional    = contracts * entry_price

            if notional < MIN_NOTIONAL:
                continue

            open_positions[fname] = {
                'direction':  sig['side'],
                'entry':      entry_price,
                'sl':         sl_price,
                'tp':         tp_price,
                'contracts':  contracts,
                'leverage':   leverage,
                'ts_open':    ts,
                'fibo_level': sig.get('fibo_level'),
            }

        # 3. Gesamt-Equity tracken (Summe aller Strategie-Toepfe)
        total_equity = sum(s['equity'] for s in processed.values())
        equity_curve.append({'timestamp': ts, 'equity': total_equity})

        # Drawdown auf Gesamt-Equity
        peak_total = sum(s['peak_eq'] for s in processed.values())
        dd = (peak_total - total_equity) / peak_total * 100 if peak_total > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        if total_equity <= 0:
            break

    total_equity = sum(s['equity'] for s in processed.values())
    total_trades = wins + losses
    win_rate     = wins / total_trades * 100 if total_trades else 0.0
    pnl_pct      = (total_equity - start_capital) / start_capital * 100

    return {
        'end_capital':      round(total_equity, 2),
        'total_pnl_pct':    round(pnl_pct, 2),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'trade_count':      total_trades,
        'wins':             wins,
        'losses':           losses,
        'win_rate':         round(win_rate, 2),
        'equity_curve':     equity_curve,
        'trade_history':    trade_history,
    }
