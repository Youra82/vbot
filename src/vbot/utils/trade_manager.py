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

_TIMEFRAME_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '12h': 43200,
    '1d': 86400, '3d': 259200, '1w': 604800,
}

def _timeframe_to_seconds(tf: str) -> int:
    return _TIMEFRAME_SECONDS.get(tf, 3600)


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
        'entry_order_id':   None,
    }


def is_globally_free() -> bool:
    return read_global_state().get('active_symbol') is None


def claim_global_state(symbol: str, timeframe: str, side: str,
                        entry_price: float, sl_price: float, tp_price: float,
                        contracts: float, fibo_level: float,
                        prev_high: float, prev_low: float,
                        entry_order_id: str = '') -> bool:
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
        'entry_order_id':   entry_order_id,
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

    sl_order_side = 'sell' if side == 'long' else 'buy'
    sl_dist_pct   = abs(current_price - sl_price) / current_price * 100
    tp_dist_pct   = abs(tp_price - current_price) / current_price * 100
    rr_ratio      = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
    logger.info(
        f"Entry: {current_price:.6f} | SL: {sl_price:.6f} (-{sl_dist_pct:.2f}%) "
        f"| TP: {tp_price:.6f} (+{tp_dist_pct:.2f}%) | R:R=1:{rr_ratio:.1f} "
        f"| Fibo: {signal.get('fibo_level', '?')}"
    )

    # --- 1. SL zuerst platzieren (reduceOnly) ---
    try:
        exchange.place_trigger_market_order(symbol, sl_order_side, contracts, sl_price, reduce=True)
        logger.info(f"SL platziert @ {sl_price:.6f}")
    except Exception as e:
        logger.error(f"SL konnte nicht platziert werden: {e}")
        return False

    time.sleep(0.5)

    # --- 2. TP platzieren (reduceOnly) ---
    try:
        exchange.place_trigger_market_order(symbol, sl_order_side, contracts, tp_price, reduce=True)
        logger.info(f"TP platziert @ {tp_price:.6f}")
    except Exception as e:
        logger.error(f"TP konnte nicht platziert werden: {e}. Raeume SL auf.")
        exchange.cancel_all_orders_for_symbol(symbol)
        return False

    time.sleep(0.5)

    # --- 3. Entry Trigger-Limit zuletzt platzieren ---
    # Trigger minimal versetzt damit Bitget die Richtung akzeptiert.
    # SHORT: trigger knapp ueber aktuellem Kurs (sell wenn Kurs steigt/bleibt)
    # LONG:  trigger knapp unter aktuellem Kurs (buy wenn Kurs faellt/bleibt)
    if side == 'short':
        trigger_price = current_price * 1.0001
        limit_price   = current_price * (1 - 0.0005)   # Limit etwas unter Trigger
    else:
        trigger_price = current_price * 0.9999
        limit_price   = current_price * (1 + 0.0005)   # Limit etwas ueber Trigger

    try:
        entry_order    = exchange.place_trigger_limit_order(
            symbol, entry_side, contracts, trigger_price, limit_price
        )
        entry_order_id = entry_order.get('id', '') if entry_order else ''
        logger.info(f"Entry Trigger-Limit: {entry_side.upper()} @ trigger={trigger_price:.6f} limit={limit_price:.6f}")
    except Exception as e:
        logger.error(f"Entry Trigger fehlgeschlagen: {e}. Raeume SL/TP auf.")
        exchange.cancel_all_orders_for_symbol(symbol)
        return False

    entry_price = current_price   # fuer Telegram und State

    # --- Global State beanspruchen ---
    claimed = claim_global_state(
        symbol, timeframe, side, entry_price, sl_price, tp_price, contracts,
        signal.get('fibo_level', 0.618),
        signal.get('prev_high', 0.0),
        signal.get('prev_low', 0.0),
        entry_order_id,
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
        # ── Trade laeuft ── SL und TP werden NICHT angefasst ──────────────
        pos      = positions[0]
        pos_side = pos.get('side', '?')
        unr_pnl  = pos.get('unrealizedPnl', 0.0)
        entry_p  = state.get('entry_price', '?')
        logger.info(
            f"Position fuer {symbol} noch offen: {pos_side.upper()} "
            f"| Entry: {entry_p} | Unrealized PnL: {unr_pnl:.2f} USDT"
        )
        return

    # ── Keine offene Position ──────────────────────────────────────────────
    # Pruefen: Entry-Trigger noch pending oder abgelaufen?
    active_since_str = state.get('active_since')
    if active_since_str:
        try:
            active_since = datetime.fromisoformat(active_since_str)
            if active_since.tzinfo is None:
                active_since = active_since.replace(tzinfo=timezone.utc)
            age_seconds  = (datetime.now(timezone.utc) - active_since).total_seconds()
            tf_seconds   = _timeframe_to_seconds(timeframe)

            if age_seconds < tf_seconds:
                # Noch innerhalb der Kerzenperiode -> Entry koennte noch feuern
                logger.info(
                    f"Kein Position, aber Entry-Trigger noch aktiv "
                    f"({age_seconds/60:.0f}/{tf_seconds/60:.0f} min). Warte."
                )
                return

            # Kerzenperiode abgelaufen -> Entry hat nicht gefeuert
            logger.info(
                f"Entry-Trigger fuer {symbol} abgelaufen "
                f"({age_seconds/3600:.1f}h > {tf_seconds/3600:.1f}h Kerze). "
                f"Storniere alle Orders und leere State."
            )
            try:
                exchange.cancel_all_orders_for_symbol(symbol)
                logger.info(f"Alle Orders fuer {symbol} storniert.")
            except Exception as e:
                logger.warning(f"Fehler beim Stornieren: {e}")

            send_message(
                telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                f"⏰ vbot Entry abgelaufen: {symbol} ({timeframe})\n"
                f"Kerze hat sich nicht ueberlagert — Entry-Trigger storniert.\n"
                f"SL @ {state.get('sl_price', '?')} | TP @ {state.get('tp_price', '?')}\n"
                f"Warte auf naechstes Signal..."
            )
            clear_global_state()
            return
        except Exception as e:
            logger.error(f"Fehler beim Timeout-Check: {e}")

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
