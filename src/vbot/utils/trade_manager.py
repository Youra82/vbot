# src/vbot/utils/trade_manager.py
"""
Trade Manager fuer vbot - Fibonacci Candle Overlap.

- Entry mit risiko-basierter Positionsgroesse
- Positionsgroesse = (Kapital * risk_per_trade_pct%) / SL-Abstand (Preis)
- SL: jenseits des Extrems der vorherigen Kerze + Buffer
- TP: Fibonacci-Level innerhalb der vorherigen Kerze (Overlap-Ziel)
- Global State: nur EIN Symbol darf gleichzeitig traden
"""

import os
import sys
import json
import logging
import time
from datetime import datetime, timezone
import ccxt

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from vbot.utils.telegram import send_message

GLOBAL_STATE_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker', 'global_state.json')
MIN_NOTIONAL_USDT = 5.0


# ============================================================
# Global State Management
# ============================================================

def read_global_state() -> dict:
    if not os.path.exists(GLOBAL_STATE_PATH):
        return _empty_state()
    try:
        with open(GLOBAL_STATE_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_state()


def write_global_state(state: dict):
    os.makedirs(os.path.dirname(GLOBAL_STATE_PATH), exist_ok=True)
    with open(GLOBAL_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def clear_global_state():
    write_global_state(_empty_state())
    logging.getLogger(__name__).info("Global State geleert - bereit fuer neuen Trade.")


def _empty_state() -> dict:
    return {
        'active_symbol':    None,
        'active_timeframe': None,
        'active_since':     None,
        'entry_price':      None,
        'side':             None,
        'sl_price':         None,
        'tp_price':         None,
        'contracts':        None,
        'fibo_level':       None,
        'prev_candle_high': None,
        'prev_candle_low':  None,
    }


def is_globally_free() -> bool:
    return read_global_state().get('active_symbol') is None


def claim_global_state(symbol: str, timeframe: str, side: str,
                        entry_price: float, sl_price: float, tp_price: float,
                        contracts: float, fibo_level: float,
                        prev_high: float, prev_low: float) -> bool:
    state = read_global_state()
    if state.get('active_symbol') is not None:
        return False
    write_global_state({
        'active_symbol':    symbol,
        'active_timeframe': timeframe,
        'active_since':     datetime.now(timezone.utc).isoformat(),
        'entry_price':      entry_price,
        'side':             side,
        'sl_price':         sl_price,
        'tp_price':         tp_price,
        'contracts':        contracts,
        'fibo_level':       fibo_level,
        'prev_candle_high': prev_high,
        'prev_candle_low':  prev_low,
    })
    return True


# ============================================================
# Positionsgroessen-Berechnung
# ============================================================

def calculate_contracts(balance_usdt: float, entry_price: float,
                          sl_price: float, min_amount: float,
                          risk_per_trade_pct: float = 1.0) -> float:
    """
    Risiko-basierte Kontraktanzahl.
    Positionsgroesse = (balance * risk_pct%) / SL-Abstand-in-Preis
    """
    risk_amount = balance_usdt * risk_per_trade_pct / 100.0
    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        return min_amount
    contracts = risk_amount / sl_distance
    return max(contracts, min_amount)


# ============================================================
# Haupt-Trading-Funktion: Signal-Modus
# ============================================================

def execute_signal_trade(exchange, symbol: str, timeframe: str,
                          signal: dict, risk_config: dict,
                          telegram_config: dict, logger: logging.Logger) -> bool:
    """
    Platziert einen Fibonacci-Candle-Overlap Trade.

    signal muss enthalten:
      side         : 'long' oder 'short'
      entry_price  : aktueller Kurs (close der letzten Kerze)
      sl_price     : Stop Loss Preis
      tp_price     : Take Profit Preis (Fibo-Level der vorherigen Kerze)
      fibo_level   : genutztes Fibo-Level (z.B. 0.618)
      prev_high    : Hoch der vorherigen Kerze
      prev_low     : Tief der vorherigen Kerze
      reason       : Text-Beschreibung des Signals
    """
    side               = signal['side']
    leverage           = int(risk_config.get('leverage', 10))
    margin_mode        = risk_config.get('margin_mode', 'isolated')
    risk_per_trade_pct = float(risk_config.get('risk_per_trade_pct', 1.0))

    # --- Kapital abrufen ---
    balance = exchange.fetch_balance_usdt()
    if balance < MIN_NOTIONAL_USDT:
        logger.warning(f"Zu wenig Kapital ({balance:.2f} USDT < {MIN_NOTIONAL_USDT} USDT). Kein Trade.")
        return False

    # --- Hebel und Margin setzen ---
    exchange.set_margin_mode(symbol, margin_mode)
    exchange.set_leverage(symbol, leverage, margin_mode)

    entry_side    = 'buy' if side == 'long' else 'sell'
    min_amount    = exchange.fetch_min_amount_tradable(symbol)
    current_price = signal['entry_price']
    sl_price      = signal['sl_price']
    tp_price      = signal['tp_price']

    # --- Positionsgroesse berechnen ---
    contracts = calculate_contracts(balance, current_price, sl_price, min_amount, risk_per_trade_pct)

    notional = contracts * current_price
    if notional < MIN_NOTIONAL_USDT:
        logger.warning(f"Notional {notional:.2f} USDT zu klein (< {MIN_NOTIONAL_USDT}). Kein Trade.")
        return False

    logger.info(
        f"Platziere Entry: {side.upper()} {contracts:.4f} {symbol} "
        f"| Hebel: {leverage}x | Kapital: {balance:.2f} USDT | Risiko: {risk_per_trade_pct}%"
    )

    try:
        entry_order = exchange.place_market_order(symbol, entry_side, contracts,
                                                   margin_mode=margin_mode)
    except Exception as e:
        logger.error(f"Entry fehlgeschlagen: {e}")
        return False

    # Tatsaechlicher Entry-Preis und Kontraktanzahl aus Order
    entry_price = float(entry_order.get('average') or entry_order.get('price') or current_price)
    if entry_price <= 0:
        entry_price = current_price
        logger.warning(f"Kein average aus Order, verwende aktuellen Kurs {entry_price}")

    filled = float(entry_order.get('filled') or entry_order.get('amount') or contracts)
    if filled <= 0:
        filled = contracts

    logger.info(
        f"Entry-Preis: {entry_price:.6f} | SL: {sl_price:.6f} | TP: {tp_price:.6f} "
        f"| Fibo-Level: {signal.get('fibo_level', '?')}"
    )
    sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
    tp_dist_pct = abs(tp_price - entry_price) / entry_price * 100
    rr_ratio    = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
    logger.info(f"SL-Abstand: {sl_dist_pct:.3f}% | TP-Abstand: {tp_dist_pct:.3f}% | R:R=1:{rr_ratio:.1f}")

    time.sleep(1.0)

    # --- SL platzieren ---
    sl_order_side = 'sell' if side == 'long' else 'buy'
    try:
        exchange.place_trigger_market_order(symbol, sl_order_side, filled, sl_price, reduce=True)
        logger.info(f"SL platziert @ {sl_price:.6f}")
    except Exception as e:
        logger.error(f"SL konnte nicht platziert werden: {e}. Schliesse Position!")
        try:
            exchange.close_position(symbol)
        except Exception as ce:
            logger.critical(f"Konnte Position nicht schliessen: {ce}")
        return False

    # --- TP platzieren ---
    try:
        exchange.place_trigger_market_order(symbol, sl_order_side, filled, tp_price, reduce=True)
        logger.info(f"TP platziert @ {tp_price:.6f}")
    except Exception as e:
        logger.error(f"TP konnte nicht platziert werden: {e}")

    # --- Global State beanspruchen ---
    claimed = claim_global_state(
        symbol, timeframe, side, entry_price, sl_price, tp_price, filled,
        signal.get('fibo_level', 0.618),
        signal.get('prev_high', 0.0),
        signal.get('prev_low', 0.0),
    )
    if not claimed:
        logger.warning("Global State wurde von anderem Symbol belegt. Schliesse Position.")
        try:
            exchange.cancel_all_orders_for_symbol(symbol)
            exchange.close_position(symbol)
        except Exception as ce:
            logger.error(f"Fehler beim Schliessen: {ce}")
        return False

    # --- Telegram-Benachrichtigung ---
    direction_emoji = "🟢" if side == 'long' else "🔴"
    risk_usdt       = balance * risk_per_trade_pct / 100.0
    fibo_pct        = signal.get('fibo_level', 0.618) * 100
    msg = (
        f"📐 vbot SIGNAL: {symbol} ({timeframe})\n"
        f"{'─' * 32}\n"
        f"{direction_emoji} Richtung:  {side.upper()}\n"
        f"💰 Entry:    ${entry_price:.6f}\n"
        f"🛑 SL:       ${sl_price:.6f} (-{sl_dist_pct:.2f}%)\n"
        f"🎯 TP:       ${tp_price:.6f} (+{tp_dist_pct:.2f}%)\n"
        f"📊 R:R:      1:{rr_ratio:.1f}\n"
        f"📏 Fibo-TP:  {fibo_pct:.1f}% Overlap\n"
        f"⚙️ Hebel:    {leverage}x\n"
        f"🛡️ Risiko:   {risk_per_trade_pct:.1f}% ({risk_usdt:.2f} USDT)\n"
        f"📦 Kontr.:   {filled:.0f}\n"
        f"{'─' * 32}\n"
        f"🔍 {signal.get('reason', '')}"
    )
    send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
    logger.info("Trade erfolgreich platziert und Telegram-Nachricht gesendet.")

    return True


# ============================================================
# Positions-Check-Funktion: Check-Modus
# ============================================================

def check_position_status(exchange, symbol: str, timeframe: str,
                           telegram_config: dict, logger: logging.Logger):
    """
    Prueft ob die aktive Position noch offen ist.
    Falls nicht mehr offen: Global State loeschen, Telegram-Nachricht senden.
    """
    state = read_global_state()

    if state.get('active_symbol') != symbol:
        logger.debug(f"check_position_status: {symbol} ist nicht das aktive Symbol, ueberspringe.")
        return

    positions = exchange.fetch_open_positions(symbol)

    if positions:
        pos      = positions[0]
        pos_side = pos.get('side', '?')
        unr_pnl  = pos.get('unrealizedPnl', 0.0)
        entry_p  = state.get('entry_price', '?')
        logger.info(
            f"Position fuer {symbol} noch offen: {pos_side.upper()} "
            f"| Entry: {entry_p} | Unrealized PnL: {unr_pnl:.2f} USDT"
        )
        return

    # Position nicht mehr offen -> TP oder SL wurde getroffen
    logger.info(f"Position fuer {symbol} wurde geschlossen (TP oder SL getroffen).")

    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        logger.info(f"Verbleibende Orders fuer {symbol} storniert.")
    except Exception as e:
        logger.warning(f"Fehler beim Stornieren verbleibender Orders: {e}")

    entry_p  = state.get('entry_price', '?')
    sl_p     = state.get('sl_price', '?')
    tp_p     = state.get('tp_price', '?')
    side_str = state.get('side', '?')
    since    = state.get('active_since', '?')
    fibo_lvl = state.get('fibo_level', '?')

    direction_emoji = "🟢" if side_str == 'long' else "🔴"
    msg = (
        f"✅ vbot TRADE GESCHLOSSEN\n"
        f"{'─' * 32}\n"
        f"{direction_emoji} {side_str.upper() if side_str else '?'} | {symbol} ({timeframe})\n"
        f"💰 Entry:   ${entry_p}\n"
        f"🛑 SL:      ${sl_p}\n"
        f"🎯 TP:      ${tp_p}\n"
        f"📏 Fibo:    {float(fibo_lvl)*100:.1f}% Overlap\n"
        f"🕐 Seit:    {since}\n"
        f"{'─' * 32}\n"
        f"⏳ Warte auf naechstes Signal..."
    )
    send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)

    clear_global_state()
