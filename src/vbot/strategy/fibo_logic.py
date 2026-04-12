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


def _calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Berechnet den letzten ADX-Wert (Average Directional Index) per Wilder-Glaettung.
    Gibt 0.0 zurueck wenn nicht genug Kerzen vorhanden (kein Filter → Trade erlaubt)."""
    needed = period * 2 + 1
    if len(df) < needed:
        return 0.0

    high  = df['high'].values[-needed:]
    low   = df['low'].values[-needed:]
    close = df['close'].values[-needed:]
    n     = len(high)

    tr_arr  = np.empty(n - 1)
    dmp_arr = np.empty(n - 1)
    dmn_arr = np.empty(n - 1)

    for i in range(1, n):
        tr_arr[i-1]  = max(high[i] - low[i],
                            abs(high[i] - close[i-1]),
                            abs(low[i]  - close[i-1]))
        h_diff = high[i] - high[i-1]
        l_diff = low[i-1] - low[i]
        dmp_arr[i-1] = h_diff if h_diff > l_diff and h_diff > 0 else 0.0
        dmn_arr[i-1] = l_diff if l_diff > h_diff and l_diff > 0 else 0.0

    def _wilder(arr: np.ndarray, p: int) -> np.ndarray:
        out = np.zeros(len(arr))
        out[p - 1] = arr[:p].sum()
        for i in range(p, len(arr)):
            out[i] = out[i-1] - out[i-1] / p + arr[i]
        return out

    atr    = _wilder(tr_arr,  period)
    dmp_sm = _wilder(dmp_arr, period)
    dmn_sm = _wilder(dmn_arr, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        di_plus  = np.where(atr > 0, 100.0 * dmp_sm / atr, 0.0)
        di_minus = np.where(atr > 0, 100.0 * dmn_sm / atr, 0.0)
        di_sum   = di_plus + di_minus
        dx       = np.where(di_sum > 0, 100.0 * np.abs(di_plus - di_minus) / di_sum, 0.0)

    dx_slice = dx[period - 1:]
    if len(dx_slice) < period:
        return 0.0
    adx_arr = _wilder(dx_slice, period)
    return float(adx_arr[-1])


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
    Fibonacci Candle Overlap Signal — Fibo-Level ALS ENTRY (Limit-Order).

    Konzept:
      Fibo-Retracement wird auf die letzte abgeschlossene Kerze gelegt.
      Das gewaehlte Fibo-Level (z.B. 0.786) innerhalb dieser Kerze ist der
      Entry-Trigger: die neue Kerze muss bis zu diesem Level "eintauchen".
      TP = Entry +/- sl_abstand * tp_rr_multiplier (festes R:R).

    df: OHLCV DataFrame — letzte Zeile (iloc[-1]) ist die aktuell offene Kerze,
        vorletzte (iloc[-2]) ist die letzte abgeschlossene Signalkerze.

    Returns dict:
      side       : 'long', 'short' oder None
      entry_price: Fibo-Level (Limit-Order-Preis)
      sl_price   : Stop Loss
      tp_price   : Take Profit (festes R:R)
      fibo_level : genutztes Fibo-Level (Eintauch-Tiefe)
      prev_high  : Hoch der Signalkerze
      prev_low   : Tief der Signalkerze
      reason     : Beschreibung
    """
    no_signal = {
        'side': None, 'entry_price': None, 'sl_price': None, 'tp_price': None,
        'fibo_level': None, 'prev_high': None, 'prev_low': None, 'reason': 'Kein Signal',
    }

    if df is None or len(df) < 3:
        no_signal['reason'] = 'Nicht genug Kerzen'
        return no_signal

    # Parameter aus Config
    fibo_entry_level = float(signal_config.get('fibo_tp_level', 0.618))  # Entry-Tiefe
    min_body_pct     = float(signal_config.get('min_candle_body_pct', 0.3))
    min_range_pct    = float(signal_config.get('min_candle_range_pct', 0.2))
    sl_buffer_pct    = float(signal_config.get('sl_buffer_pct', 0.15))
    confirm_window   = int(signal_config.get('confirm_overlap_window', 0))
    tp_rr_multiplier = float(signal_config.get('tp_rr_multiplier', 2.0))
    adx_period       = int(signal_config.get('adx_period', 14))
    adx_max          = float(signal_config.get('adx_max', 0.0))

    if fibo_entry_level not in FIBO_LEVELS:
        fibo_entry_level = min(FIBO_LEVELS, key=lambda x: abs(x - fibo_entry_level))

    prev = df.iloc[-2]  # letzte abgeschlossene Kerze (Signalkerze)
    curr = df.iloc[-1]  # aktuell offene Kerze (Referenz fuer Range-Filter)

    prev_open  = float(prev['open'])
    prev_high  = float(prev['high'])
    prev_low   = float(prev['low'])
    prev_close = float(prev['close'])
    curr_open  = float(curr['open'])  # Marktpreis zum Signal-Zeitpunkt

    # --- Filter 1: Kerzenkörper-Verhaeltnis ---
    candle_range = prev_high - prev_low
    if candle_range <= 0:
        no_signal['reason'] = 'Ungueltige Kerze (range=0)'
        return no_signal

    body_size  = abs(prev_close - prev_open)
    body_ratio = body_size / candle_range

    if body_ratio < min_body_pct:
        no_signal['reason'] = (
            f"Kerzenkörper zu klein: {body_ratio:.2%} < {min_body_pct:.2%} "
            f"(Doji/Spinning Top gefiltert)"
        )
        return no_signal

    # --- Filter 2: Mindest-Range (bezogen auf Marktpreis) ---
    range_pct = candle_range / curr_open * 100.0
    if range_pct < min_range_pct:
        no_signal['reason'] = f"Kerzenrange zu klein: {range_pct:.3f}% < {min_range_pct:.3f}%"
        return no_signal

    # --- Fibonacci-Level berechnen ---
    fibo = calculate_fibo_levels(prev_high, prev_low)

    # --- Richtung + Entry am Fibo-Level + SL/TP ---
    is_bullish = prev_close > prev_open  # bullische Vorkerze -> SHORT
    is_bearish = prev_close < prev_open  # baerische Vorkerze -> LONG

    if is_bullish:
        # SHORT: neue Kerze taucht VON OBEN in die bullische Vorkerze ein
        # Entry = Fibo-Level gemessen vom HIGH nach unten (z.B. 0.786 = 78.6% vom High)
        side        = 'short'
        entry_price = fibo['levels_from_high'][fibo_entry_level]
        sl_price    = prev_high * (1.0 + sl_buffer_pct / 100.0)
        sl_dist     = sl_price - entry_price
        if sl_dist <= 0:
            no_signal['reason'] = 'SL-Abstand ungueltig (SHORT)'
            return no_signal
        tp_price = entry_price - sl_dist * tp_rr_multiplier

    elif is_bearish:
        # LONG: neue Kerze taucht VON UNTEN in die baerische Vorkerze ein
        # Entry = Fibo-Level gemessen vom LOW nach oben (z.B. 0.786 = 78.6% vom Low)
        side        = 'long'
        entry_price = fibo['levels_from_low'][fibo_entry_level]
        sl_price    = prev_low * (1.0 - sl_buffer_pct / 100.0)
        sl_dist     = entry_price - sl_price
        if sl_dist <= 0:
            no_signal['reason'] = 'SL-Abstand ungueltig (LONG)'
            return no_signal
        tp_price = entry_price + sl_dist * tp_rr_multiplier

    else:
        no_signal['reason'] = 'Doji-Kerze (open == close)'
        return no_signal

    # --- Filter 3: Mindest-TP-Abstand ---
    tp_dist_pct = abs(tp_price - entry_price) / entry_price * 100.0
    sl_dist_pct = abs(sl_price - entry_price) / entry_price * 100.0

    if tp_dist_pct < 0.05:
        no_signal['reason'] = f"TP-Abstand zu gering: {tp_dist_pct:.4f}%"
        return no_signal

    # --- Filter 4: ADX Trend-Filter (optional) ---
    if adx_max > 0:
        adx_val = _calculate_adx(df.iloc[:-1], adx_period)
        if adx_val > adx_max:
            no_signal['reason'] = (
                f"ADX Trendmarkt: {adx_val:.1f} > {adx_max:.0f} "
                f"(Mean-Reversion unwahrscheinlich)"
            )
            return no_signal

    # --- Filter 5: Trend-Bestaetigung (optional) ---
    if confirm_window > 0 and len(df) >= confirm_window + 2:
        trend_signal = _check_trend_confirmation(df, side, confirm_window)
        if not trend_signal:
            no_signal['reason'] = (
                f"Trend-Filter: Kein klarer {'Abwaertstrend' if side == 'short' else 'Aufwaertstrend'} "
                f"in den letzten {confirm_window} Kerzen"
            )
            return no_signal

    direction_str = 'bullisch -> SHORT Eintauchen' if side == 'short' else 'baerig -> LONG Eintauchen'
    reason = (
        f"Fibo {fibo_entry_level*100:.1f}% Entry | {direction_str} | "
        f"Range: {range_pct:.3f}% | Body: {body_ratio:.2%} | R:R=1:{tp_rr_multiplier:.1f}"
    )

    return {
        'side':             side,
        'entry_price':      entry_price,   # Fibo-Level = Limit-Order-Preis
        'sl_price':         sl_price,
        'tp_price':         tp_price,
        'fibo_level':       fibo_entry_level,
        'prev_high':        prev_high,
        'prev_low':         prev_low,
        'candle_range_pct': range_pct,
        'body_ratio':       body_ratio,
        'tp_dist_pct':      tp_dist_pct,
        'sl_dist_pct':      sl_dist_pct,
        'reason':           reason,
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
