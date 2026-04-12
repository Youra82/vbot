# src/vbot/utils/trade_manager.py
"""
Trade Manager fuer vbot - Fibonacci Candle Overlap.

- Entry mit risiko-basierter Positionsgroesse
- Positionsgroesse = (Kapital * risk_per_trade_pct%) / SL-Abstand (Preis)
- Global State: mehrere Symbole gleichzeitig moeglich (max_open_positions)
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
# Global State Management (Multi-Position)
# ============================================================

def read_global_state() -> dict:
    if not os.path.exists(GLOBAL_STATE_PATH):
        return {'positions': {}}
    try:
        with open(GLOBAL_STATE_PATH, 'r') as f:
            state = json.load(f)
        # Migration: altes Single-Symbol-Format
        if 'active_symbol' in state and 'positions' not in state:
            sym = state.get('active_symbol')
            if sym:
                return {'positions': {sym: {
                    'timeframe':       state.get('active_timeframe'),
                    'active_since':    state.get('active_since'),
                    'entry_price':     state.get('entry_price'),
                    'side':            state.get('side'),
                    'sl_price':        state.get('sl_price'),
                    'tp_price':        state.get('tp_price'),
                    'contracts':       state.get('contracts'),
                    'fibo_level':      state.get('fibo_level'),
                    'prev_candle_high': state.get('prev_candle_high'),
                    'prev_candle_low':  state.get('prev_candle_low'),
                    'entry_order_id':  state.get('entry_order_id'),
                }}}
            return {'positions': {}}
        return state
    except (json.JSONDecodeError, OSError):
        return {'positions': {}}


def write_global_state(state: dict):
    os.makedirs(os.path.dirname(GLOBAL_STATE_PATH), exist_ok=True)
    with open(GLOBAL_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def clear_global_state(symbol: str = None):
    """Leert den State fuer ein bestimmtes Symbol (oder alles wenn symbol=None)."""
    logger = logging.getLogger(__name__)
    if symbol is None:
        write_global_state({'positions': {}})
        logger.info("Global State komplett geleert.")
    else:
        state = read_global_state()
        state['positions'].pop(symbol, None)
        write_global_state(state)
        logger.info(f"Global State fuer {symbol} geleert.")


def get_last_signal_ts(symbol: str) -> str:
    """Gibt den Timestamp der letzten Signalkerze zurueck, fuer die ein Trade platziert wurde."""
    state = read_global_state()
    return state.get('last_signal_ts', {}).get(symbol, '')


def set_last_signal_ts(symbol: str, ts: str):
    """Speichert den Timestamp der Signalkerze nach erfolgreichem Trade-Eintritt."""
    state = read_global_state()
    if 'last_signal_ts' not in state:
        state['last_signal_ts'] = {}
    state['last_signal_ts'][symbol] = str(ts)
    write_global_state(state)


def has_open_slot(max_positions: int) -> bool:
    """True wenn noch ein Slot frei ist."""
    state = read_global_state()
    return len(state.get('positions', {})) < max_positions


def is_symbol_active(symbol: str) -> bool:
    """True wenn dieses Symbol bereits eine offene Position hat."""
    state = read_global_state()
    return symbol in state.get('positions', {})


def is_globally_free() -> bool:
    """Rueckwaerts-kompatibel: True wenn keine einzige Position offen."""
    state = read_global_state()
    return len(state.get('positions', {})) == 0


def claim_global_state(symbol: str, timeframe: str, side: str,
                        entry_price: float, sl_price: float, tp_price: float,
                        contracts: float, fibo_level: float,
                        prev_high: float, prev_low: float,
                        entry_order_id: str = '',
                        sl_order_id: str = '',
                        tp_order_id: str = '',
                        max_positions: int = 1) -> bool:
    state = read_global_state()
    positions = state.get('positions', {})

    if symbol in positions:
        return False  # bereits aktiv
    if len(positions) >= max_positions:
        return False  # kein Slot frei

    positions[symbol] = {
        'timeframe':        timeframe,
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
        'sl_order_id':      sl_order_id,
        'tp_order_id':      tp_order_id,
    }
    write_global_state({'positions': positions})
    return True


# ============================================================
# Positionsgroessen-Berechnung
# ============================================================

def calculate_contracts(balance_usdt: float, entry_price: float,
                          sl_price: float, min_amount: float,
                          risk_per_trade_pct: float = 1.0) -> float:
    """Risiko-basierte Kontraktanzahl. Kein automatisches Aufblaehen auf min_amount —
    das wird im Aufrufer mit explizitem Margin-Check gehandhabt (titanbot-Stil)."""
    risk_amount = balance_usdt * risk_per_trade_pct / 100.0
    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        return 0.0
    return risk_amount / sl_distance


# ============================================================
# Haupt-Trading-Funktion: Signal-Modus
# ============================================================

def execute_signal_trade(exchange, symbol: str, timeframe: str,
                          signal: dict, risk_config: dict,
                          telegram_config: dict, logger: logging.Logger,
                          max_positions: int = 1,
                          signal_candle_ts: str = '') -> bool:
    side               = signal['side']
    leverage           = int(risk_config.get('leverage', 10))
    margin_mode        = risk_config.get('margin_mode', 'isolated')
    risk_per_trade_pct = float(risk_config.get('risk_per_trade_pct', 1.0))

    balance = exchange.fetch_balance_usdt()
    if balance < MIN_NOTIONAL_USDT:
        logger.warning(f"Zu wenig Kapital ({balance:.2f} USDT < {MIN_NOTIONAL_USDT} USDT). Kein Trade.")
        return False

    exchange.set_margin_mode(symbol, margin_mode)
    exchange.set_leverage(symbol, leverage, margin_mode)

    entry_side    = 'buy' if side == 'long' else 'sell'
    min_amount    = exchange.fetch_min_amount_tradable(symbol)
    current_price = signal['entry_price']
    sl_price      = signal['sl_price']
    tp_price      = signal['tp_price']

    contracts = calculate_contracts(balance, current_price, sl_price, min_amount, risk_per_trade_pct)

    # --- Position-Groessen-Validierung (titanbot-Stil) ---
    # 1. Exchange-Mindestmenge pruefen
    if contracts > 0 and contracts < min_amount:
        logger.warning(
            f"{symbol}: Risiko-Contracts {contracts:.6f} < Exchange-Minimum {min_amount}. "
            f"Ueberspringe Trade — Position zu klein fuer korrektes Risk-Management."
        )
        return False

    notional = contracts * current_price

    # 2. Mindest-Notional pruefen — auf Minimum anheben falls noetig, aber Margin pruefen
    if notional < MIN_NOTIONAL_USDT:
        # Ceil-Runden: amount_to_precision trunciert (rundet ab), daher wuerde
        # z.B. 5.05 USDT / AVAX-Preis -> 0.5562 AVAX -> truncate -> 0.55 AVAX -> 4.99 USDT < 5 USDT.
        # exchange.amount_ceil_to_precision rundet AUFRUNDEND zur naechsten Step-Groesse,
        # sodass das Notional nach Exchange-Rundung garantiert >= 5.05 USDT bleibt.
        contracts_raw  = (MIN_NOTIONAL_USDT * 1.01) / current_price
        contracts_min  = exchange.amount_ceil_to_precision(symbol, contracts_raw)
        notional_min   = contracts_min * current_price
        margin_min     = notional_min / leverage
        if margin_min > balance:
            logger.warning(
                f"{symbol}: Min-Notional-Anhebung ({notional_min:.2f} USDT) benoetigt "
                f"Margin {margin_min:.2f} USDT > Kapital {balance:.2f} USDT. Kein Trade."
            )
            return False
        logger.info(
            f"{symbol}: Notional {notional:.2f} USDT < {MIN_NOTIONAL_USDT} USDT "
            f"— angehoben auf {notional_min:.2f} USDT (Margin: {margin_min:.2f} USDT)."
        )
        contracts = contracts_min
        notional  = notional_min

    # Bestehende Orders stornieren (verhindert Stacking bei erneutem Signal)
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        logger.info(f"Bestehende Orders fuer {symbol} vor Trade-Platzierung storniert.")
    except Exception as e:
        logger.warning(f"Konnte bestehende Orders nicht stornieren: {e}")

    logger.info(
        f"Platziere Entry: {side.upper()} {contracts:.4f} {symbol} "
        f"| Hebel: {leverage}x | Kapital: {balance:.2f} USDT | Risiko: {risk_per_trade_pct}%"
    )

    sl_order_side = 'sell' if side == 'long' else 'buy'
    hold_side     = 'long' if side == 'long' else 'short'
    sl_dist_pct   = abs(current_price - sl_price) / current_price * 100
    tp_dist_pct   = abs(tp_price - current_price) / current_price * 100
    rr_ratio      = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
    logger.info(
        f"Entry: {current_price:.6f} | SL: {sl_price:.6f} (-{sl_dist_pct:.2f}%) "
        f"| TP: {tp_price:.6f} (+{tp_dist_pct:.2f}%) | R:R=1:{rr_ratio:.1f} "
        f"| Fibo: {signal.get('fibo_level', '?')}"
    )

    # 1. SL platzieren — ID fuer State merken
    try:
        sl_resp     = exchange.place_trigger_market_order(symbol, sl_order_side, contracts, sl_price,
                                                          reduce=True, hold_side=hold_side)
        sl_order_id = sl_resp.get('id', '') if sl_resp else ''
        logger.info(f"SL platziert @ {sl_price:.6f} (ID: {sl_order_id})")
    except Exception as e:
        logger.error(f"SL konnte nicht platziert werden: {e}")
        return False

    time.sleep(0.5)

    # 2. TP platzieren — ID fuer State merken
    try:
        tp_resp     = exchange.place_trigger_market_order(symbol, sl_order_side, contracts, tp_price,
                                                          reduce=True, hold_side=hold_side)
        tp_order_id = tp_resp.get('id', '') if tp_resp else ''
        logger.info(f"TP platziert @ {tp_price:.6f} (ID: {tp_order_id})")
    except Exception as e:
        logger.error(f"TP konnte nicht platziert werden: {e}. Raeume SL auf.")
        exchange.cancel_all_orders_for_symbol(symbol)
        return False

    time.sleep(0.5)

    # 3. Entry Trigger-Limit
    if side == 'short':
        trigger_price = current_price * 1.0001
        limit_price   = current_price * (1 - 0.0005)
    else:
        trigger_price = current_price * 0.9999
        limit_price   = current_price * (1 + 0.0005)

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

    entry_price = current_price

    # Global State beanspruchen — mit SL/TP Order-IDs
    claimed = claim_global_state(
        symbol, timeframe, side, entry_price, sl_price, tp_price, contracts,
        signal.get('fibo_level', 0.618),
        signal.get('prev_high', 0.0),
        signal.get('prev_low', 0.0),
        entry_order_id,
        sl_order_id=sl_order_id,
        tp_order_id=tp_order_id,
        max_positions=max_positions,
    )
    if not claimed:
        logger.warning("Global State voll oder Symbol bereits aktiv. Schliesse Orders.")
        try:
            exchange.cancel_all_orders_for_symbol(symbol)
        except Exception as ce:
            logger.error(f"Fehler beim Stornieren: {ce}")
        return False

    # Signalkerze merken — verhindert Wiedereintritt in derselben Kerze
    if signal_candle_ts:
        set_last_signal_ts(symbol, signal_candle_ts)

    # Telegram
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
        f"📦 Kontr.:   {contracts:.4f}\n"
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
    state     = read_global_state()
    positions = state.get('positions', {})

    # Exchange direkt pruefen — auch wenn State fehlt
    try:
        open_pos = exchange.fetch_open_positions(symbol)
    except Exception as e:
        logger.error(f"Konnte offene Positionen nicht abrufen: {e}")
        open_pos = []

    if symbol not in positions:
        if not open_pos:
            logger.debug(f"check_position_status: {symbol} nicht im State und keine Position — ueberspringe.")
            return
        # Position auf Exchange, aber State verloren — rekonstruiere minimalen State
        logger.warning(
            f"{symbol}: Offene Position auf Exchange gefunden, aber kein State-Eintrag! "
            f"Erstelle minimalen State-Eintrag aus Exchange-Daten."
        )
        pos_ex = open_pos[0]
        real_entry = (pos_ex.get('entryPrice')
                      or pos_ex.get('info', {}).get('openPriceAvg')
                      or pos_ex.get('info', {}).get('avgOpenPrice'))
        try:
            entry_reconstructed = float(real_entry) if real_entry else 0.0
        except (ValueError, TypeError):
            entry_reconstructed = 0.0
        pos_state = {
            'side':           pos_ex.get('side', 'long'),
            'entry_price':    entry_reconstructed,
            'contracts':      float(pos_ex.get('contracts') or 0),
            'sl_price':       None,  # Nicht rekonstruierbar
            'tp_price':       None,  # Nicht rekonstruierbar
            'timeframe':      timeframe,
            'active_since':   datetime.now(timezone.utc).isoformat(),
        }
        # State schreiben damit naechste Phase den Eintrag findet
        positions[symbol] = pos_state
        write_global_state({'positions': positions})
        send_message(
            telegram_config.get('bot_token'), telegram_config.get('chat_id'),
            f"⚠️ vbot: State fuer {symbol} war verloren!\n"
            f"Position auf Exchange gefunden und rekonstruiert.\n"
            f"SL/TP-Preise unbekannt — bitte manuell pruefen!"
        )
    else:
        pos_state = positions[symbol]

    if open_pos:
        pos      = open_pos[0]
        pos_side = pos.get('side', '?')
        unr_pnl  = pos.get('unrealizedPnl', 0.0)
        entry_p   = pos_state.get('entry_price', '?')
        contracts = float(pos.get('contracts') or pos_state.get('contracts', 0))
        # Echter Entry-Preis von der Position bevorzugen (robuster bei Slippage)
        real_entry = (pos.get('entryPrice')
                      or pos.get('info', {}).get('openPriceAvg')
                      or pos.get('info', {}).get('avgOpenPrice'))
        try:
            entry_float = float(real_entry) if real_entry else float(entry_p)
        except (ValueError, TypeError):
            entry_float = 0.0
        logger.info(
            f"Position fuer {symbol} noch offen: {pos_side.upper()} "
            f"| Entry: {entry_float or entry_p} | Unrealized PnL: {unr_pnl:.2f} USDT"
        )

        # Self-Repair: Pruefen ob SL und TP noch existieren
        try:
            trigger_orders   = exchange.fetch_open_trigger_orders(symbol)
            open_order_ids   = {str(o.get('id', '')) for o in trigger_orders}
            sl_order_side    = 'sell' if pos_side == 'long' else 'buy'
            hold_side_repair = pos_side
            sl_price_val     = pos_state.get('sl_price')
            tp_price_val     = pos_state.get('tp_price')

            # --- Primaer: ID-basierte Erkennung (zuverlaessig unabhaengig von ccxt-Feld-Mapping) ---
            saved_sl_id = str(pos_state.get('sl_order_id', ''))
            saved_tp_id = str(pos_state.get('tp_order_id', ''))

            if saved_sl_id and saved_tp_id:
                sl_exists = saved_sl_id in open_order_ids
                tp_exists = saved_tp_id in open_order_ids
                logger.info(
                    f"{symbol}: ID-Check — SL={sl_exists} (ID:{saved_sl_id}) "
                    f"TP={tp_exists} (ID:{saved_tp_id}) | {len(open_order_ids)} offene Trigger-Orders"
                )
            else:
                # --- Fallback: Preis-basierte Erkennung (fuer alte Trades ohne gespeicherte IDs) ---
                sl_exists = False
                tp_exists = False
                if entry_float <= 0:
                    logger.warning(
                        f"{symbol}: Keine Order-IDs im State und Entry-Preis unbekannt — "
                        f"ueberspringe Self-Repair um falsches Platzieren zu verhindern."
                    )
                else:
                    for order in trigger_orders:
                        trig_raw = (order.get('stopPrice')
                                    or order.get('triggerPrice')
                                    or order.get('info', {}).get('triggerPrice')
                                    or order.get('info', {}).get('planPrice'))
                        try:
                            trig = float(trig_raw)
                        except (ValueError, TypeError):
                            continue
                        if trig <= 0:
                            continue
                        if pos_side == 'long':
                            if trig < entry_float:
                                sl_exists = True
                            elif trig > entry_float:
                                tp_exists = True
                        else:
                            if trig > entry_float:
                                sl_exists = True
                            elif trig < entry_float:
                                tp_exists = True
                    logger.info(
                        f"{symbol}: Preis-Fallback-Check — SL={sl_exists} TP={tp_exists} "
                        f"| {len(trigger_orders)} Trigger-Orders | Entry={entry_float:.6f}"
                    )

            # --- Repair: fehlende Orders neu platzieren und IDs im State aktualisieren ---
            if not sl_exists or not tp_exists:
                logger.warning(
                    f"Self-Repair {symbol}: SL={sl_exists} TP={tp_exists} — platziere fehlende Orders neu"
                )
                state_update = read_global_state()
                pos_entry    = state_update.get('positions', {}).get(symbol, {})

                if not sl_exists and sl_price_val and contracts > 0:
                    try:
                        sl_resp    = exchange.place_trigger_market_order(
                            symbol, sl_order_side, contracts, float(sl_price_val),
                            reduce=True, hold_side=hold_side_repair
                        )
                        new_sl_id  = str(sl_resp.get('id', '')) if sl_resp else ''
                        pos_entry['sl_order_id'] = new_sl_id
                        logger.info(f"SL repariert @ {sl_price_val} (neue ID: {new_sl_id})")
                    except Exception as e:
                        logger.error(f"SL-Reparatur fehlgeschlagen: {e}")
                elif not sl_exists and not sl_price_val:
                    logger.error(f"SL fehlt fuer {symbol} aber SL-Preis unbekannt. Manuelle Intervention noetig!")

                if not tp_exists and tp_price_val and contracts > 0:
                    try:
                        tp_resp    = exchange.place_trigger_market_order(
                            symbol, sl_order_side, contracts, float(tp_price_val),
                            reduce=True, hold_side=hold_side_repair
                        )
                        new_tp_id  = str(tp_resp.get('id', '')) if tp_resp else ''
                        pos_entry['tp_order_id'] = new_tp_id
                        logger.info(f"TP repariert @ {tp_price_val} (neue ID: {new_tp_id})")
                    except Exception as e:
                        logger.error(f"TP-Reparatur fehlgeschlagen: {e}")
                elif not tp_exists and not tp_price_val:
                    logger.error(f"TP fehlt fuer {symbol} aber TP-Preis unbekannt. Manuelle Intervention noetig!")

                # Neue IDs in State zurueckschreiben
                if symbol in state_update.get('positions', {}):
                    state_update['positions'][symbol] = pos_entry
                    write_global_state(state_update)

        except Exception as e:
            logger.error(f"Fehler beim Self-Repair-Check fuer {symbol}: {e}")

        # --- Preis-Overshoot-Check: Position schließen falls Preis SL oder TP überschritten ---
        if sl_price_val and tp_price_val and contracts > 0:
            try:
                current_price = float(exchange.exchange.fetch_ticker(symbol)['last'])
                sl_val = float(sl_price_val)
                tp_val = float(tp_price_val)
                if pos_side == 'long':
                    breached = current_price <= sl_val or current_price >= tp_val
                    reason   = "SL" if current_price <= sl_val else "TP"
                else:
                    breached = current_price >= sl_val or current_price <= tp_val
                    reason   = "SL" if current_price >= sl_val else "TP"
                if breached:
                    level = sl_val if reason == 'SL' else tp_val
                    logger.warning(
                        f"Preis-Overshoot: {current_price:.6f} hat {reason} ({level:.6f}) überschritten — "
                        f"schließe Position {symbol} per Market."
                    )
                    try:
                        exchange.cancel_all_orders_for_symbol(symbol)
                    except Exception as ce:
                        logger.warning(f"Cancel-Orders fehlgeschlagen (ignoriert): {ce}")
                    exchange.place_market_order(symbol, sl_order_side, contracts, reduce=True)
                    # State leeren
                    state_update = read_global_state()
                    if 'positions' in state_update and symbol in state_update['positions']:
                        del state_update['positions'][symbol]
                        write_global_state(state_update)
                    send_message(
                        telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                        f"vBot NOTSCHLIESSUNG ({symbol}): Preis {current_price:.6f} hat "
                        f"{reason} ({level:.6f}) überschritten. Position geschlossen."
                    )
                    logger.info(f"Position {symbol} geschlossen — State geleert.")
            except Exception as e:
                logger.error(f"Fehler beim Preis-Overshoot-Check fuer {symbol}: {e}")

        return

    # Keine offene Position — Timeout oder geschlossen?
    active_since_str = pos_state.get('active_since')
    if active_since_str:
        try:
            active_since = datetime.fromisoformat(active_since_str)
            if active_since.tzinfo is None:
                active_since = active_since.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - active_since).total_seconds()
            tf_seconds  = _timeframe_to_seconds(timeframe)

            if age_seconds < tf_seconds:
                # Entry noch pending — Self-Repair fuer fehlende SL/TP
                fetch_error    = False
                trigger_orders = []
                try:
                    trigger_orders = exchange.fetch_open_trigger_orders(symbol)
                except Exception as e:
                    logger.error(f"Fehler beim Pending-Trigger-Fetch fuer {symbol}: {e}")
                    fetch_error = True

                entry_exists  = False
                saved_side    = pos_state.get('side', 'long')
                saved_entry_id = str(pos_state.get('entry_order_id', ''))
                open_ids       = {str(o.get('id', '')) for o in trigger_orders}

                if not fetch_error:
                    if saved_entry_id:
                        # ID-basierte Erkennung: Entry-Order noch offen?
                        entry_exists = saved_entry_id in open_ids
                    else:
                        # Fallback: irgendeine nicht-Close-Order vorhanden?
                        expected_close_side = 'sell' if saved_side == 'long' else 'buy'
                        for order in trigger_orders:
                            if order.get('side', '') != expected_close_side:
                                entry_exists = True
                                break

                    if not entry_exists:
                        # Keine Entry-Order auf Exchange — Position wurde geschlossen
                        # oder Entry wurde abgebrochen. State leeren.
                        logger.info(
                            f"{symbol}: Entry-Trigger nicht mehr auf Exchange gefunden "
                            f"(ID: {saved_entry_id or 'unbekannt'}). "
                            f"Storniere Rest-Orders und leere State."
                        )
                        try:
                            exchange.cancel_all_orders_for_symbol(symbol)
                        except Exception as ce:
                            logger.warning(f"Cleanup-Fehler: {ce}")
                        send_message(
                            telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                            f"⏰ vbot: Entry fuer {symbol} ({timeframe}) nicht mehr aktiv.\n"
                            f"Order wurde ausgefuehrt oder abgebrochen — State geleert.\n"
                            f"Warte auf naechstes Signal..."
                        )
                        clear_global_state(symbol)
                        return

                logger.info(
                    f"Entry-Trigger noch aktiv "
                    f"({age_seconds/60:.0f}/{tf_seconds/60:.0f} min). Warte."
                )
                return

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
                f"SL @ {pos_state.get('sl_price', '?')} | TP @ {pos_state.get('tp_price', '?')}\n"
                f"Warte auf naechstes Signal..."
            )
            clear_global_state(symbol)
            return
        except Exception as e:
            logger.error(f"Fehler beim Timeout-Check: {e}")

    # Position wurde geschlossen (TP oder SL getroffen)
    logger.info(f"Position fuer {symbol} wurde geschlossen (TP oder SL getroffen).")

    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        logger.info(f"Verbleibende Orders fuer {symbol} storniert.")
    except Exception as e:
        logger.warning(f"Fehler beim Stornieren verbleibender Orders: {e}")

    entry_p  = pos_state.get('entry_price', '?')
    sl_p     = pos_state.get('sl_price', '?')
    tp_p     = pos_state.get('tp_price', '?')
    side_str = pos_state.get('side', '?')
    since    = pos_state.get('active_since', '?')
    fibo_lvl = pos_state.get('fibo_level', '?')

    direction_emoji = "🟢" if side_str == 'long' else "🔴"
    try:
        fibo_str = f"{float(fibo_lvl)*100:.1f}% Overlap"
    except (ValueError, TypeError):
        fibo_str = str(fibo_lvl)

    msg = (
        f"✅ vbot TRADE GESCHLOSSEN\n"
        f"{'─' * 32}\n"
        f"{direction_emoji} {side_str.upper() if side_str else '?'} | {symbol} ({timeframe})\n"
        f"💰 Entry:   ${entry_p}\n"
        f"🛑 SL:      ${sl_p}\n"
        f"🎯 TP:      ${tp_p}\n"
        f"📏 Fibo:    {fibo_str}\n"
        f"🕐 Seit:    {since}\n"
        f"{'─' * 32}\n"
        f"⏳ Warte auf naechstes Signal..."
    )
    send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
    clear_global_state(symbol)
