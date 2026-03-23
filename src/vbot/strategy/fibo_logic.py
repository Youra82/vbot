# src/vbot/strategy/fibo_logic.py
"""
Fibonacci Candle Overlap Strategie - Kern-Signal-Logik

Konzept:
  Nach jeder vollstaendig ausgebildeten Kerze wird ein Fibonacci-Retracement
  innerhalb dieser Kerze berechnet. Das Fibo-Level gibt an, wie weit sich die
  naechste Kerze mit der vorherigen ueberlagern (zuruecksetzen) wird.

Signal-Logik:
  - Bullische vorherige Kerze (close > open):
      -> Neue Kerze wird sich voraussichtlich NACH UNTEN ueberlagern
      -> SHORT Trade
      -> Entry:  aktueller Kurs (= Close der letzten abgeschlossenen Kerze)
      -> TP:     Fibo-Level innerhalb der vorherigen Kerze, gemessen vom Hoch
                 (z.B. 61.8%: TP = high - 0.618 * range)
      -> SL:     ueber dem Hoch der vorherigen Kerze + Buffer

  - Baerige vorherige Kerze (close < open):
      -> Neue Kerze wird sich voraussichtlich NACH OBEN ueberlagern
      -> LONG Trade
      -> Entry:  aktueller Kurs (= Close der letzten abgeschlossenen Kerze)
      -> TP:     Fibo-Level innerhalb der vorherigen Kerze, gemessen vom Tief
                 (z.B. 61.8%: TP = low + 0.618 * range)
      -> SL:     unter dem Tief der vorherigen Kerze - Buffer

Fibo-Level Bedeutung:
  0.236  = 23.6% Ueberlagerung (schwache Erholung)
  0.382  = 38.2% Ueberlagerung
  0.500  = 50.0% Ueberlagerung (halbe Kerze)
  0.618  = 61.8% Ueberlagerung (goldener Schnitt, Standard)
  0.786  = 78.6% Ueberlagerung (tiefe Erholung)

Filter:
  - Kerzenkörper muss mindestens min_candle_body_pct der Gesamtrange ausmachen
    (filtert Doji-Kerzen und Spinning Tops)
  - Gesamtrange muss mindestens min_candle_range_pct des Preises ausmachen
    (filtert zu kleine, bedeutungslose Kerzen)
  - Optional: Trend-Bestaetigung ueber N Kerzen zurueck
"""

import pandas as pd
import numpy as np
from typing import Optional

FIBO_LEVELS = [0.236, 0.382, 0.5, 0.618, 0.786]


def calculate_fibo_levels(candle_high: float, candle_low: float) -> dict:
    """
    Berechnet Fibonacci-Retracement-Level fuer eine Kerze.

    Returns dict mit:
      'range'   : Gesamtrange der Kerze (high - low)
      'levels'  : {0.236: price, 0.382: price, ...}
                  gemessen vom Hoch nach unten (fuer Short-TP)
                  und vom Tief nach oben (fuer Long-TP)
    """
    candle_range = candle_high - candle_low
    levels_from_high = {}  # fuer Short-Trade TP (Ueberlagerung nach unten)
    levels_from_low  = {}  # fuer Long-Trade TP  (Ueberlagerung nach oben)

    for lvl in FIBO_LEVELS:
        levels_from_high[lvl] = candle_high - lvl * candle_range
        levels_from_low[lvl]  = candle_low  + lvl * candle_range

    return {
        'range':            candle_range,
        'levels_from_high': levels_from_high,
        'levels_from_low':  levels_from_low,
    }


def get_fibo_signal(df: pd.DataFrame, signal_config: dict) -> dict:
    """
    Berechnet das Fibonacci-Candle-Overlap Signal auf Basis des letzten
    abgeschlossenen OHLCV-Datensatzes.

    df: OHLCV DataFrame mit Spalten [open, high, low, close, volume]
        Die letzte Zeile (df.iloc[-1]) ist die aktuell noch OFFENE Kerze.
        Die vorletzte Zeile (df.iloc[-2]) ist die letzte ABGESCHLOSSENE Kerze.

    Returns dict:
      side       : 'long', 'short' oder None (kein Signal)
      entry_price: aktueller Kurs (Close der letzten abgeschl. Kerze)
      sl_price   : Stop Loss Preis
      tp_price   : Take Profit Preis (Fibo-Level)
      fibo_level : genutztes Fibo-Level (z.B. 0.618)
      prev_high  : Hoch der Signalkerze
      prev_low   : Tief der Signalkerze
      reason     : Beschreibung des Signals
    """
    no_signal = {
        'side': None, 'entry_price': None, 'sl_price': None, 'tp_price': None,
        'fibo_level': None, 'prev_high': None, 'prev_low': None, 'reason': 'Kein Signal',
    }

    if df is None or len(df) < 3:
        no_signal['reason'] = 'Nicht genug Kerzen'
        return no_signal

    # Parameter aus Config
    fibo_tp_level       = float(signal_config.get('fibo_tp_level', 0.618))
    min_body_pct        = float(signal_config.get('min_candle_body_pct', 0.3))
    min_range_pct       = float(signal_config.get('min_candle_range_pct', 0.2))
    sl_buffer_pct       = float(signal_config.get('sl_buffer_pct', 0.15))
    confirm_window      = int(signal_config.get('confirm_overlap_window', 0))

    if fibo_tp_level not in FIBO_LEVELS:
        # Naechstes gueltiges Level waehlen
        fibo_tp_level = min(FIBO_LEVELS, key=lambda x: abs(x - fibo_tp_level))

    # Letzte abgeschlossene Kerze (vorletzte Zeile wegen noch offener aktueller Kerze)
    prev = df.iloc[-2]
    curr = df.iloc[-1]

    prev_open  = float(prev['open'])
    prev_high  = float(prev['high'])
    prev_low   = float(prev['low'])
    prev_close = float(prev['close'])

    entry_price = float(curr['open'])  # Open der neuen Kerze = Close der vorherigen

    # --- Filter 1: Kerzenkörper-Verhaeltnis ---
    candle_range = prev_high - prev_low
    if candle_range <= 0:
        no_signal['reason'] = 'Ungueltige Kerze (range=0)'
        return no_signal

    body_size = abs(prev_close - prev_open)
    body_ratio = body_size / candle_range

    if body_ratio < min_body_pct:
        no_signal['reason'] = (
            f"Kerzenkörper zu klein: {body_ratio:.2%} < {min_body_pct:.2%} "
            f"(Doji/Spinning Top gefiltert)"
        )
        return no_signal

    # --- Filter 2: Mindest-Range ---
    range_pct = candle_range / entry_price * 100.0
    if range_pct < min_range_pct:
        no_signal['reason'] = (
            f"Kerzenrange zu klein: {range_pct:.3f}% < {min_range_pct:.3f}%"
        )
        return no_signal

    # --- Fibonacci-Level berechnen ---
    fibo = calculate_fibo_levels(prev_high, prev_low)

    # --- Richtung bestimmen ---
    is_bullish = prev_close > prev_open  # vorherige Kerze bullish -> SHORT Trade
    is_bearish = prev_close < prev_open  # vorherige Kerze bearish -> LONG Trade

    if is_bullish:
        side     = 'short'
        tp_price = fibo['levels_from_high'][fibo_tp_level]
        sl_price = prev_high * (1.0 + sl_buffer_pct / 100.0)

        # TP muss UNTER dem Entry liegen (sonst kein sinnvoller Short)
        if tp_price >= entry_price:
            no_signal['reason'] = (
                f"TP ({tp_price:.6f}) >= Entry ({entry_price:.6f}) - kein sinnvoller Short"
            )
            return no_signal

    elif is_bearish:
        side     = 'long'
        tp_price = fibo['levels_from_low'][fibo_tp_level]
        sl_price = prev_low * (1.0 - sl_buffer_pct / 100.0)

        # TP muss UEBER dem Entry liegen (sonst kein sinnvoller Long)
        if tp_price <= entry_price:
            no_signal['reason'] = (
                f"TP ({tp_price:.6f}) <= Entry ({entry_price:.6f}) - kein sinnvoller Long"
            )
            return no_signal
    else:
        no_signal['reason'] = 'Doji-Kerze (open == close)'
        return no_signal

    # --- Filter 3: Mindest-TP-Abstand (Trade muss sich lohnen) ---
    tp_dist_pct = abs(tp_price - entry_price) / entry_price * 100.0
    sl_dist_pct = abs(sl_price - entry_price) / entry_price * 100.0

    if tp_dist_pct < 0.05:
        no_signal['reason'] = f"TP-Abstand zu gering: {tp_dist_pct:.4f}%"
        return no_signal

    # --- Filter 4: Trend-Bestaetigung (optional) ---
    if confirm_window > 0 and len(df) >= confirm_window + 2:
        trend_signal = _check_trend_confirmation(df, side, confirm_window)
        if not trend_signal:
            no_signal['reason'] = (
                f"Trend-Filter: Kein klarer {'Abwaertstrend' if side == 'short' else 'Aufwaertstrend'} "
                f"in den letzten {confirm_window} Kerzen"
            )
            return no_signal

    rr_ratio = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
    direction_str = 'bullisch -> SHORT Overlap' if side == 'short' else 'baerig -> LONG Overlap'

    reason = (
        f"Fibo {fibo_tp_level*100:.1f}% Overlap | Vorherige Kerze {direction_str} | "
        f"Range: {range_pct:.3f}% | Body: {body_ratio:.2%} | R:R=1:{rr_ratio:.1f}"
    )

    return {
        'side':        side,
        'entry_price': entry_price,
        'sl_price':    sl_price,
        'tp_price':    tp_price,
        'fibo_level':  fibo_tp_level,
        'prev_high':   prev_high,
        'prev_low':    prev_low,
        'candle_range_pct': range_pct,
        'body_ratio':  body_ratio,
        'tp_dist_pct': tp_dist_pct,
        'sl_dist_pct': sl_dist_pct,
        'reason':      reason,
    }


def _check_trend_confirmation(df: pd.DataFrame, side: str, window: int) -> bool:
    """
    Optionaler Trend-Filter: Prueft ob die letzten N Kerzen (exkl. der aktuellen
    Signal-Kerze) einen Trend in Richtung des erwarteten Overlaps bestaetigen.

    Fuer SHORT: mindestens 60% der letzten N Kerzen sollten bullish sein
                (=> Preis war zuletzt gestiegen, Overlap nach unten wahrscheinlicher)
    Fuer LONG:  mindestens 60% der letzten N Kerzen sollten bearish sein
    """
    # Letzte 'window' Kerzen VOR der Signalkerze
    window_candles = df.iloc[-(window + 2):-2]
    if len(window_candles) == 0:
        return True

    closes = window_candles['close'].values
    opens  = window_candles['open'].values
    bullish_count = np.sum(closes > opens)
    bullish_ratio = bullish_count / len(window_candles)

    if side == 'short':
        return bullish_ratio >= 0.6  # ueberwiegend bullish -> Short-Overlap wahrscheinlich
    else:
        return bullish_ratio <= 0.4  # ueberwiegend bearish -> Long-Overlap wahrscheinlich


def get_all_fibo_levels_info(prev_high: float, prev_low: float, entry_price: float) -> str:
    """
    Hilfsfunktion fuer Logging: Gibt alle Fibo-Level als lesbaren String zurueck.
    """
    fibo = calculate_fibo_levels(prev_high, prev_low)
    lines = [f"Fibo-Level fuer Kerze (High: {prev_high:.6f} | Low: {prev_low:.6f}):"]
    for lvl in FIBO_LEVELS:
        price_short = fibo['levels_from_high'][lvl]
        price_long  = fibo['levels_from_low'][lvl]
        dist_short  = (entry_price - price_short) / entry_price * 100.0
        dist_long   = (price_long - entry_price)  / entry_price * 100.0
        lines.append(
            f"  {lvl*100:.1f}%:  Short-TP={price_short:.6f} ({dist_short:.3f}%) | "
            f"Long-TP={price_long:.6f} ({dist_long:.3f}%)"
        )
    return '\n'.join(lines)
