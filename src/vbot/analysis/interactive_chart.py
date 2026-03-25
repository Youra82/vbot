# src/vbot/analysis/interactive_chart.py
# vbot — Interaktiver Candlestick-Chart mit Fibonacci Candle Overlap Trade-Markern
#
# Panels:
#   1. Candlestick + Entry/Exit-Marker + SL/TP-Linien + Equity-Kurve (rechte Achse)
#      Overlay: Fibo-Level-Linien der vorherigen Kerze (letzte Kerze im Datensatz)
#   2. Volumen
#   3. Kerzenkoerper-Ratio (body/range) + min_candle_body_pct Schwelle
#   4. RSI(14) — Kontext
#
# Output: HTML-Datei in artifacts/charts/ (oeffnet im Browser)

import os
import sys
import json
import logging
from datetime import date

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from vbot.analysis.backtester import run_backtest, load_ohlcv, auto_days_for_timeframe, BacktestResult

logger = logging.getLogger(__name__)

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'vbot', 'strategy', 'configs')
CHARTS_DIR  = os.path.join(PROJECT_ROOT, 'artifacts', 'charts')


# ─────────────────────────────────────────────────────────────────────────────
# Pair-Auswahl aus aktuellen Configs
# ─────────────────────────────────────────────────────────────────────────────

def _load_configs() -> list:
    entries = []
    if not os.path.isdir(CONFIGS_DIR):
        return entries
    for fname in sorted(f for f in os.listdir(CONFIGS_DIR)
                        if f.startswith('config_') and f.endswith('.json')):
        try:
            with open(os.path.join(CONFIGS_DIR, fname)) as f:
                cfg = json.load(f)
            symbol    = cfg.get('market', {}).get('symbol', '')
            timeframe = cfg.get('market', {}).get('timeframe', '')
            if symbol and timeframe:
                entries.append({'filename': fname, 'symbol': symbol, 'timeframe': timeframe})
        except Exception:
            pass
    return entries


def select_pairs() -> list:
    configs = _load_configs()
    if not configs:
        print("Keine Configs gefunden. Erst run_pipeline.sh ausfuehren.")
        return []

    w = 70
    print("\n" + "=" * w)
    print("  Verfuegbare Pairs  (aus aktuellen Configs)")
    print("=" * w)
    for i, d in enumerate(configs, 1):
        print(f"  {i:2d}) {d['symbol']:<22} {d['timeframe']:<5}")
    print("=" * w)

    print("\n  Einzeln: '1' | Mehrfach: '1,3' oder '1 3'")
    raw = input("  Auswahl: ").strip()
    selected = []
    for token in raw.replace(',', ' ').split():
        try:
            idx = int(token)
            if 1 <= idx <= len(configs):
                d = configs[idx - 1]
                pair = (d['symbol'], d['timeframe'])
                if pair not in selected:
                    selected.append(pair)
        except ValueError:
            pass
    return selected


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsindikatoren
# ─────────────────────────────────────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame) -> dict:
    """RSI(14) und Kerzenkoerper-Ratio."""
    # Kerzenkoerper-Ratio = abs(close-open) / (high-low)
    body  = (df['close'] - df['open']).abs()
    rang  = (df['high']  - df['low']).replace(0, np.nan)
    body_ratio = (body / rang).fillna(0)

    # RSI(14)
    delta    = df['close'].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - (100 / (1 + rs))

    return {'body_ratio': body_ratio, 'rsi': rsi}


# ─────────────────────────────────────────────────────────────────────────────
# Chart erstellen
# ─────────────────────────────────────────────────────────────────────────────

def create_chart(symbol: str, timeframe: str, df: pd.DataFrame,
                 result: BacktestResult, config: dict):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("plotly nicht installiert. Bitte: pip install plotly")
        return None

    indicators  = _compute_indicators(df)
    body_ratio  = indicators['body_ratio']
    rsi         = indicators['rsi']

    signal_cfg  = config.get('signal', {})
    min_body    = float(signal_cfg.get('min_candle_body_pct', 0.3))

    # ── Subplots ─────────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        specs=[
            [{'secondary_y': True}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
        ],
        vertical_spacing=0.022,
        row_heights=[0.50, 0.12, 0.19, 0.19],
        subplot_titles=[
            '',
            'Volumen',
            f'Kerzenkoerper-Ratio  (Filter: >= {min_body:.0%})',
            'RSI (14)',
        ],
    )

    # ── Startkapital-Referenzlinie ────────────────────────────────────────────
    fig.add_hline(
        y=result.start_capital,
        line=dict(color='rgba(100,100,100,0.35)', width=1, dash='dash'),
        annotation_text=f'Start {result.start_capital:.0f} USDT',
        annotation_position='top left',
        row=1, col=1,
    )

    # ── Fibo-Levels der letzten vollstaendigen Kerze als Overlay ──────────────
    FIBO_LEVELS = [0.236, 0.382, 0.500, 0.618, 0.786]
    FIBO_COLORS = {
        0.236: 'rgba(255,255,100,0.6)',
        0.382: 'rgba(255,167,38,0.7)',
        0.500: 'rgba(150,255,150,0.7)',
        0.618: 'rgba(38,166,154,0.9)',
        0.786: 'rgba(239,83,80,0.7)',
    }
    if len(df) >= 2:
        last_candle = df.iloc[-2]  # letzte abgeschlossene Kerze
        h, l = float(last_candle['high']), float(last_candle['low'])
        rang = h - l
        if rang > 0:
            x0 = df.index[-50] if len(df) > 50 else df.index[0]
            x1 = df.index[-1]
            for lvl in FIBO_LEVELS:
                from_high = h - lvl * rang
                from_low  = l + lvl * rang
                color = FIBO_COLORS[lvl]
                for price, label in [(from_high, f'H-{lvl:.3f}'), (from_low, f'L-{lvl:.3f}')]:
                    fig.add_shape(
                        type='line', x0=x0, x1=x1, y0=price, y1=price,
                        line=dict(color=color, width=1, dash='dot'),
                        row=1, col=1,
                    )
                    fig.add_annotation(
                        x=x1, y=price, text=f'  {label}',
                        showarrow=False,
                        font=dict(color=color, size=8),
                        xanchor='left',
                        row=1, col=1,
                    )

    # ── Panel 1: Candlesticks ─────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'],
        low=df['low'],   close=df['close'],
        name='OHLC',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        showlegend=True,
    ), row=1, col=1, secondary_y=False)

    # ── Trade-Marker ──────────────────────────────────────────────────────────
    entry_long_x, entry_long_y, entry_long_txt    = [], [], []
    entry_short_x, entry_short_y, entry_short_txt = [], [], []
    exit_win_x,  exit_win_y  = [], []
    exit_loss_x, exit_loss_y = [], []

    for t in result.trades:
        entry_ts = t.timestamp
        bar_idx  = t.bar_idx + t.hold_bars if t.hold_bars else len(df) - 1
        exit_ts  = df.index[min(bar_idx, len(df) - 1)]
        tip = (
            f"Fibo-Level: {t.fibo_level:.3f}<br>"
            f"SL: {t.sl:.4f}  TP: {t.tp:.4f}<br>"
            f"Ergebnis: {t.result.upper()}<br>"
            f"PnL: {t.pnl_usdt:+.2f} USDT"
        )

        if t.direction == 'long':
            entry_long_x.append(entry_ts)
            entry_long_y.append(t.entry)
            entry_long_txt.append(tip)
        else:
            entry_short_x.append(entry_ts)
            entry_short_y.append(t.entry)
            entry_short_txt.append(tip)

        if t.result == 'win':
            exit_win_x.append(exit_ts);  exit_win_y.append(t.exit_price)
        else:
            exit_loss_x.append(exit_ts); exit_loss_y.append(t.exit_price)

        # SL- und TP-Linien pro Trade (duenn, transparent)
        fig.add_shape(
            type='line', x0=entry_ts, x1=exit_ts, y0=t.sl, y1=t.sl,
            line=dict(color='rgba(239,68,68,0.40)', width=1, dash='dot'),
        )
        fig.add_shape(
            type='line', x0=entry_ts, x1=exit_ts, y0=t.tp, y1=t.tp,
            line=dict(color='rgba(34,197,94,0.40)', width=1, dash='dot'),
        )

    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode='markers',
            marker=dict(color='#26a69a', symbol='triangle-up', size=14,
                        line=dict(width=1, color='#fff')),
            name='Entry Long  (Bullish Kerze → LONG)', text=entry_long_txt,
            hovertemplate='%{text}<extra>Entry Long</extra>',
        ), row=1, col=1, secondary_y=False)

    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode='markers',
            marker=dict(color='#ffa726', symbol='triangle-down', size=14,
                        line=dict(width=1, color='#fff')),
            name='Entry Short  (Bearish Kerze → SHORT)', text=entry_short_txt,
            hovertemplate='%{text}<extra>Entry Short</extra>',
        ), row=1, col=1, secondary_y=False)

    if exit_win_x:
        fig.add_trace(go.Scatter(
            x=exit_win_x, y=exit_win_y, mode='markers',
            marker=dict(color='#00bcd4', symbol='circle', size=10,
                        line=dict(width=1, color='#fff')),
            name='Exit TP  ✓',
        ), row=1, col=1, secondary_y=False)

    if exit_loss_x:
        fig.add_trace(go.Scatter(
            x=exit_loss_x, y=exit_loss_y, mode='markers',
            marker=dict(color='#ef5350', symbol='x', size=10,
                        line=dict(width=2, color='#ef5350')),
            name='Exit SL  ✗',
        ), row=1, col=1, secondary_y=False)

    # ── Equity-Kurve (rechte Y-Achse) ─────────────────────────────────────────
    eq_times = [df.index[0]]
    eq_vals  = [result.start_capital]
    equity   = result.start_capital
    for t in sorted(result.trades, key=lambda x: x.timestamp):
        equity += t.pnl_usdt
        eq_times.append(t.timestamp)
        eq_vals.append(round(equity, 4))

    fig.add_trace(go.Scatter(
        x=eq_times, y=eq_vals,
        name='Equity (USDT)',
        line=dict(color='#5c9bd6', width=1.5),
        hovertemplate='Equity: %{y:.2f} USDT<extra></extra>',
    ), row=1, col=1, secondary_y=True)

    # ── Panel 2: Volumen ───────────────────────────────────────────────────────
    vol_colors = ['#26a69a' if c >= o else '#ef5350'
                  for c, o in zip(df['close'], df['open'])]
    fig.add_trace(go.Bar(
        x=df.index, y=df['volume'],
        marker_color=vol_colors, opacity=0.65,
        name='Volumen', showlegend=False,
        hovertemplate='Vol: %{y:,.0f}<extra></extra>',
    ), row=2, col=1)

    # ── Panel 3: Kerzenkoerper-Ratio ───────────────────────────────────────────
    ratio_colors = ['#26a69a' if v >= min_body else '#9e9e9e' for v in body_ratio]
    fig.add_trace(go.Bar(
        x=df.index, y=body_ratio,
        marker_color=ratio_colors, opacity=0.75,
        name='Body-Ratio', showlegend=False,
        hovertemplate='Body-Ratio: %{y:.2f}<extra></extra>',
    ), row=3, col=1)
    fig.add_hline(
        y=min_body, line_dash='dot',
        line_color='rgba(255,255,255,0.55)',
        annotation_text=f'Min Body {min_body:.0%}',
        annotation_position='right',
        annotation_font_size=9,
        row=3, col=1,
    )

    # ── Panel 4: RSI ───────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=rsi,
        mode='lines', line=dict(color='#ce93d8', width=1.5),
        fill='tozeroy', fillcolor='rgba(206,147,216,0.07)',
        name='RSI(14)', showlegend=False,
        hovertemplate='RSI: %{y:.1f}<extra></extra>',
    ), row=4, col=1)
    fig.add_hline(y=70, line_dash='dot', line_color='rgba(239,83,80,0.55)',  row=4, col=1)
    fig.add_hline(y=30, line_dash='dot', line_color='rgba(38,166,154,0.55)', row=4, col=1)
    fig.add_hline(y=50, line_dash='dot', line_color='rgba(150,150,150,0.3)', row=4, col=1)

    # ── Titel & Layout ─────────────────────────────────────────────────────────
    pnl_pct = result.pnl_pct
    sign    = '+' if pnl_pct >= 0 else ''
    title = (
        f"{symbol} {timeframe} — vbot Fibonacci Candle Overlap | "
        f"Trades: {result.total_trades}  W:{result.wins} L:{result.losses} | "
        f"WR: {result.win_rate:.1f}% | "
        f"PnL: {sign}{pnl_pct:.1f}% ({sign}{result.end_capital - result.start_capital:.2f} USDT) | "
        f"Endkapital: {result.end_capital:.2f} USDT | "
        f"MaxDD: {result.max_drawdown_pct:.1f}%"
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=12), x=0.5, xanchor='center'),
        height=1050,
        hovermode='x unified',
        template='plotly_dark',
        dragmode='zoom',
        xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01,
                    xanchor='center', x=0.5, font=dict(size=11)),
        margin=dict(l=60, r=80, t=80, b=40),
        yaxis2=dict(
            title='Equity (USDT)', showgrid=False,
            tickfont=dict(color='#5c9bd6'),
            title_font=dict(color='#5c9bd6'),
        ),
    )

    fig.update_yaxes(title_text='Preis',       row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text='Volumen',      row=2, col=1)
    fig.update_yaxes(title_text='Body-Ratio',   row=3, col=1, range=[0, 1.05])
    fig.update_yaxes(title_text='RSI',          row=4, col=1, range=[0, 100])

    for row in range(1, 5):
        fig.update_xaxes(rangeslider_visible=False, row=row, col=1)

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run_interactive_chart(secrets: dict):
    print("\n" + "=" * 65)
    print("  INTERAKTIVE CHARTS — vbot Fibonacci Candle Overlap")
    print("=" * 65)

    selected = select_pairs()
    if not selected:
        return

    print()
    start_raw = input("Startdatum (JJJJ-MM-TT) [leer=auto]: ").strip()
    end_raw   = input("Enddatum   (JJJJ-MM-TT) [leer=heute]: ").strip()

    cap_raw = input("Startkapital in USDT [Standard: 1000]: ").strip()
    start_capital = float(cap_raw) if cap_raw.replace('.', '').isdigit() else 1000.0

    tg_raw  = input("Per Telegram senden? (j/n) [Standard: n]: ").strip().lower()
    send_tg = tg_raw in ('j', 'y', 'ja')

    os.makedirs(CHARTS_DIR, exist_ok=True)
    generated = []
    today = date.today().isoformat()

    for symbol, timeframe in selected:
        print(f"\n--- {symbol} ({timeframe}) ---")

        if start_raw:
            sd = start_raw
        else:
            n_days = auto_days_for_timeframe(timeframe)
            sd = (pd.Timestamp(today, tz='UTC') - pd.Timedelta(days=n_days)).strftime('%Y-%m-%d')
        ed = end_raw if end_raw else today

        print(f"  Lade Daten [{sd} -> {ed}]...")
        df = load_ohlcv(symbol, timeframe, sd, ed)
        if df.empty:
            print("  Keine Daten — uebersprungen.")
            continue
        print(f"  {len(df)} Kerzen")

        safe     = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
        cfg_path = os.path.join(CONFIGS_DIR, f"config_{safe}_fibo.json")
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                config = json.load(f)
        else:
            config = {
                "market":  {"symbol": symbol, "timeframe": timeframe},
                "signal":  {"fibo_tp_level": 0.618, "min_candle_body_pct": 0.3,
                            "min_candle_range_pct": 0.3, "sl_buffer_pct": 0.1},
                "risk":    {"leverage": 10, "risk_per_trade_pct": 1.0, "margin_mode": "isolated"},
            }

        print("  Fuehre Backtest durch...")
        result = run_backtest(df, config, start_capital, symbol, timeframe)
        print(f"  {result.total_trades} Trades | WR: {result.win_rate:.1f}% | "
              f"PnL: {result.pnl_pct:+.1f}% | MaxDD: {result.max_drawdown_pct:.1f}%")

        # Datum-Filter auf Chart-Ansicht
        df_chart = df.copy()
        if start_raw:
            df_chart = df_chart[df_chart.index >= pd.Timestamp(start_raw, tz='UTC')]
        if end_raw:
            df_chart = df_chart[df_chart.index <= pd.Timestamp(end_raw + 'T23:59:59', tz='UTC')]

        print("  Erstelle Chart...")
        fig = create_chart(symbol, timeframe, df_chart, result, config)
        if fig is None:
            continue

        out_file = os.path.join(CHARTS_DIR, f"vbot_{safe}.html")
        fig.write_html(out_file)
        print(f"  Chart gespeichert: {out_file}")
        generated.append((symbol, timeframe, out_file, result))

    print(f"\n{len(generated)} Chart(s) generiert!")
    for _, _, path, _ in generated:
        print(f"  -> {path}")

    if send_tg and generated:
        tg = secrets.get('telegram', {})
        if tg.get('bot_token') and tg.get('chat_id'):
            from vbot.utils.telegram import send_document
            for sym, tf, path, res in generated:
                try:
                    sign = '+' if res.pnl_pct >= 0 else ''
                    caption = (
                        f"vbot Chart | {sym} {tf} | "
                        f"Trades: {res.total_trades} | WR: {res.win_rate:.1f}% | "
                        f"PnL: {sign}{res.pnl_pct:.1f}% | "
                        f"MaxDD: {res.max_drawdown_pct:.1f}%"
                    )
                    send_document(tg['bot_token'], tg['chat_id'], path, caption=caption)
                    print(f"  Chart via Telegram gesendet: {sym} {tf}")
                except Exception as e:
                    print(f"  Telegram-Fehler: {e}")
        else:
            print("  Telegram nicht konfiguriert (bot_token/chat_id fehlt).")
