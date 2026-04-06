# src/vbot/strategy/run.py
"""
vbot - Fibonacci Candle Overlap Strategy Runner

Modi:
  --mode signal  : Signal pruefen, Trade platzieren wenn Fibo-Signal vorhanden
  --mode check   : Offene Position pruefen, Global State loeschen falls geschlossen

Signal-Logik (fibo_logic.py):
  - Letzte abgeschlossene Kerze analysieren
  - Fibo-Retracement auf diese Kerze legen
  - Erwartete Ueberlagerung der neuen Kerze als Trade-Ziel (TP)
  - Bullische Kerze -> SHORT Trade (neue Kerze retraciert nach unten)
  - Bearische Kerze -> LONG Trade  (neue Kerze retraciert nach oben)

Signal-Parameter aus Config-Datei:
  src/vbot/strategy/configs/config_BTCUSDTUSDT_1h_fibo.json
  (erstellt von run_pipeline.sh via optimizer.py)
  Fallback: settings.json

Wird vom master_runner.py aufgerufen.
"""

import os
import sys
import json
import logging
import argparse
from logging.handlers import RotatingFileHandler

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from vbot.utils.exchange import Exchange
from vbot.utils.telegram import send_message
from vbot.utils.guardian import guardian_decorator
from vbot.utils.trade_manager import (
    has_open_slot,
    is_symbol_active,
    execute_signal_trade,
    check_position_status,
    read_global_state,
)
from vbot.strategy.fibo_logic import get_fibo_signal, get_all_fibo_levels_info


# ============================================================
# Logging Setup
# ============================================================

def setup_logging(symbol: str, timeframe: str) -> logging.Logger:
    safe_name = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    log_dir   = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file  = os.path.join(log_dir, f'vbot_{safe_name}.log')

    logger_name = f'vbot_{safe_name}'
    logger = logging.getLogger(logger_name)

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            f'%(asctime)s [{safe_name}] %(levelname)s: %(message)s', datefmt='%H:%M:%S'
        ))
        logger.addHandler(ch)
        logger.propagate = False

    return logger


# ============================================================
# Dekorierte Ausfuehrungs-Funktion
# ============================================================

@guardian_decorator
def run_for_account(account: dict, telegram_config: dict,
                     symbol: str, timeframe: str,
                     mode: str, settings: dict, logger: logging.Logger):
    """
    Hauptausfuehrung fuer einen Account.
    mode='signal': Fibo-Signal pruefen und Trade platzieren
    mode='check':  Offene Position pruefen
    """
    logger.info(f"=== vbot Fibo Start | {symbol} ({timeframe}) | Modus: {mode} ===")

    exchange    = Exchange(account)
    risk_config = settings.get('risk', {})

    # Signal-Parameter aus generierter Config-Datei laden
    safe_name   = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    config_path = os.path.join(
        PROJECT_ROOT, 'src', 'vbot', 'strategy', 'configs',
        f'config_{safe_name}_fibo.json'
    )
    if os.path.exists(config_path):
        with open(config_path, 'r') as cf:
            loaded_cfg = json.load(cf)
        signal_config = loaded_cfg.get('signal', {})
        # Optimierte Risiko-Parameter aus Config uebernehmen
        risk_config = dict(risk_config)
        for key in ['leverage', 'risk_per_trade_pct']:
            if key in loaded_cfg.get('risk', {}):
                risk_config[key] = loaded_cfg['risk'][key]
        logger.info(
            f"Config geladen: config_{safe_name}_fibo.json "
            f"(PnL: {loaded_cfg.get('_backtest', {}).get('pnl_pct', '?')}% | "
            f"Fibo: {signal_config.get('fibo_tp_level', '?')} | "
            f"Hebel: {risk_config.get('leverage', 10)}x)"
        )
    else:
        signal_config = settings.get('signal', {})
        logger.warning(
            f"Keine Config gefunden fuer {symbol} ({timeframe}). "
            f"Verwende Defaults aus settings.json."
        )

    max_positions = settings.get('live_trading_settings', {}).get('max_open_positions', 1)

    if mode == 'check':
        check_position_status(exchange, symbol, timeframe, telegram_config, logger)

    elif mode == 'signal':
        if is_symbol_active(symbol):
            logger.info(f"{symbol} hat bereits eine offene Position - ueberspringe.")
            return

        if not has_open_slot(max_positions):
            state     = read_global_state()
            aktive    = list(state.get('positions', {}).keys())
            logger.info(
                f"Max. {max_positions} Position(en) offen {aktive} - ueberspringe {symbol}."
            )
            return

        # OHLCV-Daten laden
        confirm_window = int(signal_config.get('confirm_overlap_window', 3))
        limit = max(50, confirm_window + 10)
        df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=limit)
        if df.empty:
            logger.warning(f"Keine OHLCV-Daten fuer {symbol}. Ueberspringe.")
            return

        # Fibo-Signal berechnen
        signal = get_fibo_signal(df, signal_config)

        logger.info(f"Fibo-Signal {symbol}: side={signal['side']} | {signal['reason']}")

        if signal['side'] is None:
            logger.info(f"Kein Fibo-Signal fuer {symbol}.")
            return

        # Alle Fibo-Level ins Log schreiben (zur Analyse)
        prev_high = signal.get('prev_high', 0)
        prev_low  = signal.get('prev_low', 0)
        entry     = signal.get('entry_price', 0)
        if prev_high and prev_low and entry:
            logger.info(get_all_fibo_levels_info(prev_high, prev_low, entry))

        # nochmal pruefen (Race Condition bei parallelen Prozessen)
        if not has_open_slot(max_positions) or is_symbol_active(symbol):
            logger.info(f"Slot nicht mehr frei - ueberspringe {symbol}.")
            return

        success = execute_signal_trade(
            exchange, symbol, timeframe, signal,
            risk_config, telegram_config, logger,
            max_positions=max_positions,
        )

        if success:
            logger.info(f"Fibo Trade fuer {symbol} erfolgreich platziert.")
        else:
            logger.info(f"Fibo Trade fuer {symbol} nicht platziert.")

    logger.info(f"=== vbot Fibo Ende | {symbol} ({timeframe}) | Modus: {mode} ===")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='vbot Fibonacci Candle Overlap Runner')
    parser.add_argument('--symbol',    required=True, type=str, help='Handelspaar (z.B. BTC/USDT:USDT)')
    parser.add_argument('--timeframe', required=True, type=str, help='Zeitrahmen (z.B. 1h)')
    parser.add_argument('--mode',      required=True, type=str,
                        choices=['signal', 'check'],
                        help='signal=Signal pruefen | check=Position pruefen')
    args = parser.parse_args()

    symbol    = args.symbol
    timeframe = args.timeframe
    mode      = args.mode

    logger = setup_logging(symbol, timeframe)

    try:
        settings_path = os.path.join(PROJECT_ROOT, 'settings.json')
        with open(settings_path, 'r') as f:
            settings = json.load(f)

        secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
        with open(secret_path, 'r') as f:
            secrets = json.load(f)

        accounts = secrets.get('vbot', [])
        if not accounts:
            logger.critical("Keine 'vbot'-Accounts in secret.json gefunden.")
            sys.exit(1)

        telegram_config = secrets.get('telegram', {})

    except FileNotFoundError as e:
        logger.critical(f"Datei nicht gefunden: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"JSON-Fehler: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Initialisierungsfehler: {e}", exc_info=True)
        sys.exit(1)

    account = accounts[0]
    try:
        run_for_account(account, telegram_config, symbol, timeframe, mode, settings, logger)
    except Exception as e:
        logger.error(f"Fehler beim Ausfuehren fuer {symbol}: {e}", exc_info=True)
        sys.exit(1)

    logger.info(f"vbot-Lauf fuer {symbol} ({timeframe}) abgeschlossen.")


if __name__ == '__main__':
    main()
