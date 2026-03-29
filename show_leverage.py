#!/usr/bin/env python3
"""Zeigt Hebel, SL, Risiko und Backtest-Parameter aller aktiven vbot-Strategien."""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
CONFIGS_DIR   = os.path.join(PROJECT_ROOT, 'src', 'vbot', 'strategy', 'configs')


def fmt(val, suffix='', decimals=2, fallback='n/a'):
    if isinstance(val, (int, float)):
        return f"{val:.{decimals}f}{suffix}"
    return fallback


def main():
    try:
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    except FileNotFoundError:
        print("Fehler: settings.json nicht gefunden.")
        sys.exit(1)

    live = settings.get('live_trading_settings', {})
    rows = []

    for s in live.get('active_strategies', []):
        if not isinstance(s, dict) or not s.get('active', True):
            continue
        symbol_clean = s['symbol'].replace('/', '').replace(':', '')
        tf = s['timeframe']
        candidate = f"config_{symbol_clean}_{tf}_fibo.json"
        full_path = os.path.join(CONFIGS_DIR, candidate)
        if not os.path.exists(full_path):
            print(f"  WARN  Config fuer {s['symbol']} {tf} nicht gefunden.")
            continue

        with open(full_path) as f:
            cfg = json.load(f)

        risk   = cfg.get('risk', {})
        signal = cfg.get('signal', {})
        bt     = cfg.get('_backtest', {})
        mkt    = cfg.get('market', {})

        symbol  = mkt.get('symbol', '').split('/')[0]
        label   = f"{symbol}/{tf}"

        leverage    = fmt(risk.get('leverage'), 'x', 0)
        risk_pct    = fmt(risk.get('risk_per_trade_pct'), '%')
        margin      = risk.get('margin_mode', 'n/a')
        fibo_tp     = fmt(signal.get('fibo_tp_level'), '', 3)
        sl_buf      = fmt(signal.get('sl_buffer_pct'), '%')
        trend_win   = signal.get('confirm_overlap_window', 0)
        trend_str   = f"{trend_win} Kerzen" if isinstance(trend_win, int) and trend_win > 0 else 'aus'
        pnl_oos     = fmt(bt.get('oos_pnl_pct', bt.get('pnl_pct')), '%', 1, 'n/a')

        rows.append({
            'Strategie':    label,
            'Hebel':        leverage,
            'Risiko':       risk_pct,
            'Fibo TP':      fibo_tp,
            'SL Buffer':    sl_buf,
            'Trend Filter': trend_str,
            'TSL Akt.':     'kein TSL',
            'TSL Callback': 'kein TSL',
            'PnL OOS':      pnl_oos,
        })

    if not rows:
        print("Keine aktiven Konfigurationen gefunden.")
        sys.exit(0)

    # Spaltenbreiten berechnen
    cols = list(rows[0].keys())
    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(r[c])))

    sep    = '+-' + '-+-'.join('-' * widths[c] for c in cols) + '-+'
    header = '| ' + ' | '.join(c.ljust(widths[c]) for c in cols) + ' |'

    print()
    print(f"  Modus: Manuell (settings.json)  |  Strategien: {len(rows)}")
    print()
    print('  ' + sep)
    print('  ' + header)
    print('  ' + sep)
    for r in rows:
        line = '| ' + ' | '.join(str(r[c]).ljust(widths[c]) for c in cols) + ' |'
        print('  ' + line)
    print('  ' + sep)
    print()


if __name__ == '__main__':
    main()
