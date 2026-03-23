# tests/test_workflow.py
"""
Integration Tests fuer vbot - Fibonacci Candle Overlap

Tests:
  1. test_fibo_signal_bullish_candle   - Bullische Kerze -> SHORT Signal
  2. test_fibo_signal_bearish_candle   - Bearische Kerze -> LONG Signal
  3. test_fibo_signal_doji_filtered    - Doji-Kerze -> kein Signal
  4. test_fibo_all_levels              - Alle Fibo-Level werden korrekt berechnet
  5. test_backtest_runs                - Backtester laeuft durch ohne Fehler
  6. test_place_entry_on_bitget        - Echter Trade auf Bitget (skip wenn kein secret.json)
"""

import os
import sys
import json
import pytest
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from vbot.strategy.fibo_logic import get_fibo_signal, calculate_fibo_levels, FIBO_LEVELS
from vbot.analysis.backtester import run_backtest


# ============================================================
# Hilfsfunktionen
# ============================================================

def make_ohlcv_df(rows: list) -> pd.DataFrame:
    """Erstellt einen OHLCV-DataFrame aus einer Liste von (open, high, low, close, vol) Tupeln."""
    data = []
    for i, (o, h, l, c, v) in enumerate(rows):
        data.append({'timestamp': pd.Timestamp(f'2024-01-{i+1:02d}', tz='UTC'),
                     'open': o, 'high': h, 'low': l, 'close': c, 'volume': v})
    df = pd.DataFrame(data).set_index('timestamp')
    return df


DEFAULT_CONFIG = {
    'fibo_tp_level':        0.618,
    'min_candle_body_pct':  0.3,
    'min_candle_range_pct': 0.2,
    'sl_buffer_pct':        0.15,
    'confirm_overlap_window': 0,
}


# ============================================================
# Test 1: Bullische Kerze -> SHORT Signal
# ============================================================

def test_fibo_signal_bullish_candle():
    """
    Vorherige Kerze bullisch (close > open) -> SHORT Trade erwartet.
    Entry = close der vorherigen Kerze = open der aktuellen Kerze.
    TP = 61.8% Level vom Hoch nach unten.
    SL = oberhalb des Hochs.
    """
    # Kerze 1: Dummy-Kerze
    # Kerze 2: Bullische Signalkerze: open=100, close=110, high=112, low=98
    # Kerze 3: Aktuelle (noch offene) Kerze: open=110 (= close der vorherigen)
    rows = [
        (95.0, 97.0, 93.0, 96.0, 1000.0),   # Dummy
        (100.0, 112.0, 98.0, 110.0, 1500.0), # Signalkerze (bullisch)
        (110.0, 111.0, 109.0, 110.5, 800.0), # Aktuelle Kerze
    ]
    df = make_ohlcv_df(rows)

    signal = get_fibo_signal(df, DEFAULT_CONFIG)

    assert signal['side'] == 'short', f"Erwartet SHORT, bekam: {signal['side']}"
    assert signal['entry_price'] == pytest.approx(110.0, abs=0.01)

    # TP = high - 0.618 * range = 112 - 0.618 * (112 - 98) = 112 - 8.652 = 103.348
    expected_tp = 112.0 - 0.618 * (112.0 - 98.0)
    assert signal['tp_price'] == pytest.approx(expected_tp, abs=0.01), \
        f"TP erwartet: {expected_tp:.4f}, bekommen: {signal['tp_price']:.4f}"

    # SL = high + buffer = 112 * (1 + 0.0015) = 112.168
    expected_sl = 112.0 * (1.0 + 0.15 / 100.0)
    assert signal['sl_price'] == pytest.approx(expected_sl, abs=0.01)

    assert signal['fibo_level'] == 0.618
    assert signal['tp_price'] < signal['entry_price'], "TP muss unter Entry liegen (Short)"
    assert signal['sl_price'] > signal['entry_price'], "SL muss ueber Entry liegen (Short)"
    print(f"\nSHORT Signal: Entry={signal['entry_price']:.4f} | "
          f"TP={signal['tp_price']:.4f} | SL={signal['sl_price']:.4f}")
    print(f"Grund: {signal['reason']}")


# ============================================================
# Test 2: Bearische Kerze -> LONG Signal
# ============================================================

def test_fibo_signal_bearish_candle():
    """
    Vorherige Kerze bearisch (close < open) -> LONG Trade erwartet.
    TP = 61.8% Level vom Tief nach oben.
    SL = unterhalb des Tiefs.
    """
    # Kerze 2: Bearische Signalkerze: open=110, close=100, high=112, low=98
    rows = [
        (115.0, 117.0, 113.0, 116.0, 1000.0),  # Dummy
        (110.0, 112.0, 98.0, 100.0, 1500.0),   # Signalkerze (bearisch)
        (100.0, 101.0, 99.0, 100.5, 800.0),    # Aktuelle Kerze
    ]
    df = make_ohlcv_df(rows)

    signal = get_fibo_signal(df, DEFAULT_CONFIG)

    assert signal['side'] == 'long', f"Erwartet LONG, bekam: {signal['side']}"
    assert signal['entry_price'] == pytest.approx(100.0, abs=0.01)

    # TP = low + 0.618 * range = 98 + 0.618 * (112 - 98) = 98 + 8.652 = 106.652
    expected_tp = 98.0 + 0.618 * (112.0 - 98.0)
    assert signal['tp_price'] == pytest.approx(expected_tp, abs=0.01), \
        f"TP erwartet: {expected_tp:.4f}, bekommen: {signal['tp_price']:.4f}"

    # SL = low - buffer = 98 * (1 - 0.0015)
    expected_sl = 98.0 * (1.0 - 0.15 / 100.0)
    assert signal['sl_price'] == pytest.approx(expected_sl, abs=0.01)

    assert signal['tp_price'] > signal['entry_price'], "TP muss ueber Entry liegen (Long)"
    assert signal['sl_price'] < signal['entry_price'], "SL muss unter Entry liegen (Long)"
    print(f"\nLONG Signal: Entry={signal['entry_price']:.4f} | "
          f"TP={signal['tp_price']:.4f} | SL={signal['sl_price']:.4f}")
    print(f"Grund: {signal['reason']}")


# ============================================================
# Test 3: Doji-Kerze -> kein Signal
# ============================================================

def test_fibo_signal_doji_filtered():
    """
    Doji-Kerze (kleiner Kerzenkörper, body_ratio < min_candle_body_pct)
    sollte kein Signal erzeugen.
    """
    # Signalkerze: open=100, close=100.5 (sehr kleiner Körper), high=112, low=98
    # Body = 0.5, Range = 14 -> body_ratio = 0.5/14 = 0.036 < 0.3
    rows = [
        (100.0, 102.0, 98.0, 101.0, 1000.0),
        (100.0, 112.0, 98.0, 100.5, 1500.0),  # Doji (0.5 body / 14 range = 3.6%)
        (100.5, 101.0, 100.0, 100.7, 800.0),
    ]
    df = make_ohlcv_df(rows)

    signal = get_fibo_signal(df, DEFAULT_CONFIG)

    assert signal['side'] is None, f"Doji-Kerze sollte kein Signal erzeugen, bekam: {signal['side']}"
    assert 'Kerzenkörper' in signal['reason'] or 'body' in signal['reason'].lower(), \
        f"Erwartete Filterbegruendung, bekam: {signal['reason']}"
    print(f"\nDoji gefiltert: {signal['reason']}")


# ============================================================
# Test 4: Fibonacci-Level Berechnung
# ============================================================

def test_fibo_all_levels():
    """
    Prueft ob alle 5 Fibonacci-Level korrekt berechnet werden.
    Testet sowohl Short (from_high) als auch Long (from_low) Levels.
    """
    high  = 200.0
    low   = 100.0
    fibo  = calculate_fibo_levels(high, low)

    expected_from_high = {
        0.236: 200.0 - 0.236 * 100.0,  # 176.4
        0.382: 200.0 - 0.382 * 100.0,  # 161.8
        0.500: 200.0 - 0.500 * 100.0,  # 150.0
        0.618: 200.0 - 0.618 * 100.0,  # 138.2
        0.786: 200.0 - 0.786 * 100.0,  # 121.4
    }
    expected_from_low = {
        0.236: 100.0 + 0.236 * 100.0,  # 123.6
        0.382: 100.0 + 0.382 * 100.0,  # 138.2
        0.500: 100.0 + 0.500 * 100.0,  # 150.0
        0.618: 100.0 + 0.618 * 100.0,  # 161.8
        0.786: 100.0 + 0.786 * 100.0,  # 178.6
    }

    for lvl in FIBO_LEVELS:
        assert fibo['levels_from_high'][lvl] == pytest.approx(expected_from_high[lvl], abs=0.001), \
            f"from_high {lvl}: erwartet {expected_from_high[lvl]}, bekam {fibo['levels_from_high'][lvl]}"
        assert fibo['levels_from_low'][lvl] == pytest.approx(expected_from_low[lvl], abs=0.001), \
            f"from_low {lvl}: erwartet {expected_from_low[lvl]}, bekam {fibo['levels_from_low'][lvl]}"

    assert fibo['range'] == pytest.approx(100.0)
    print(f"\nAlle Fibo-Level korrekt berechnet (Range: {fibo['range']:.0f})")
    print(f"  Short-TP 61.8%: {fibo['levels_from_high'][0.618]:.1f}")
    print(f"  Long-TP  61.8%: {fibo['levels_from_low'][0.618]:.1f}")


# ============================================================
# Test 5: Backtester laeuft durch
# ============================================================

def test_backtest_runs():
    """
    Prueft ob der Backtester fehlerfrei auf einem synthetischen Datensatz laeuft
    und sinnvolle Ergebnisse zurueckgibt.
    """
    # Generiere synthetischen BTC-Kurs: 200 Kerzen
    np.random.seed(42)
    n        = 200
    prices   = 50000.0 + np.cumsum(np.random.randn(n) * 100.0)
    opens    = prices + np.random.randn(n) * 20.0
    highs    = np.maximum(opens, prices) + np.abs(np.random.randn(n) * 50.0)
    lows     = np.minimum(opens, prices) - np.abs(np.random.randn(n) * 50.0)
    closes   = prices
    volumes  = np.random.uniform(100, 1000, n)

    data = pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows,
        'close': closes, 'volume': volumes
    }, index=pd.date_range('2024-01-01', periods=n, freq='1h', tz='UTC'))

    signal_config = {
        'fibo_tp_level':        0.618,
        'min_candle_body_pct':  0.3,
        'min_candle_range_pct': 0.1,
        'sl_buffer_pct':        0.15,
        'confirm_overlap_window': 0,
    }
    risk_config = {'leverage': 10, 'risk_per_trade_pct': 1.0}

    results = run_backtest(data, signal_config, risk_config, start_capital=1000.0)

    assert isinstance(results, dict)
    assert 'pnl_pct' in results
    assert 'win_rate' in results
    assert 'total_trades' in results
    assert results['total_trades'] >= 0
    assert 0.0 <= results['win_rate'] <= 100.0

    print(f"\nBacktest laeuft fehlerfrei:")
    print(f"  Trades: {results['total_trades']}")
    print(f"  PnL:    {results['pnl_pct']:+.2f}%")
    print(f"  WR:     {results['win_rate']:.1f}%")
    print(f"  MaxDD:  {results['max_drawdown_pct']:.2f}%")


# ============================================================
# Test 6: Echter Trade auf Bitget (skip ohne secret.json)
# ============================================================

SECRET_PATH = os.path.join(PROJECT_ROOT, 'secret.json')

@pytest.mark.skipif(
    not os.path.exists(SECRET_PATH),
    reason="secret.json nicht gefunden - Bitget-Test uebersprungen"
)
def test_place_entry_on_bitget():
    """
    Prueft ob ein echter Trade auf Bitget platziert werden kann.
    Wird uebersprungen wenn keine secret.json vorhanden.
    Nutzt sl_buffer_pct=5% damit die Order-Distanz gross genug ist.
    """
    from vbot.utils.exchange import Exchange
    from vbot.utils.trade_manager import execute_signal_trade, clear_global_state

    with open(SECRET_PATH, 'r') as f:
        secrets = json.load(f)

    accounts = secrets.get('vbot', [])
    if not accounts:
        pytest.skip("Keine 'vbot'-Accounts in secret.json - Test uebersprungen")

    account = accounts[0]
    exchange = Exchange(account)

    symbol    = 'BTC/USDT:USDT'
    timeframe = '1h'

    df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=10)
    assert not df.empty, "Keine Daten empfangen"

    # Signal mit weitem SL fuer Test (damit Min-Distanz auf Exchange erfuellt ist)
    test_config = {
        'fibo_tp_level':        0.618,
        'min_candle_body_pct':  0.0,   # kein Filter fuer Test
        'min_candle_range_pct': 0.0,   # kein Filter fuer Test
        'sl_buffer_pct':        5.0,   # 5% Buffer -> SL weit genug fuer Exchange
        'confirm_overlap_window': 0,
    }
    signal = get_fibo_signal(df, test_config)
    if signal['side'] is None:
        pytest.skip(f"Kein Signal auf aktuellen BTC-Daten: {signal['reason']}")

    risk_config     = {'leverage': 5, 'margin_mode': 'isolated', 'risk_per_trade_pct': 1.0}
    telegram_config = secrets.get('telegram', {})

    import logging
    logger = logging.getLogger('test_vbot')
    logging.basicConfig(level=logging.INFO)

    clear_global_state()

    success = execute_signal_trade(
        exchange, symbol, timeframe, signal,
        risk_config, telegram_config, logger
    )

    assert success, "Trade konnte nicht platziert werden"
    print(f"\nTrade erfolgreich platziert: {signal['side'].upper()} {symbol}")
    print(f"Entry: {signal['entry_price']:.2f} | TP: {signal['tp_price']:.2f} | SL: {signal['sl_price']:.2f}")

    # Aufraumen: Position sofort wieder schliessen
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        exchange.close_position(symbol)
        clear_global_state()
        print("Position nach Test geschlossen.")
    except Exception as e:
        print(f"Hinweis: Position konnte nicht automatisch geschlossen werden: {e}")
