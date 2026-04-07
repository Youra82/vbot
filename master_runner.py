# master_runner.py — vbot Master Runner
# Liest aktive Strategien aus settings.json und startet run.py fuer jede.
# Entwickelt fuer Cronjob-Ausfuehrung (einmal pro Intervall).

import json
import subprocess
import sys
import os
import time
import logging

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

log_dir  = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'master_runner.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)


def _run_auto_optimizer():
    """Startet den Auto-Optimizer-Scheduler non-blocking im Hintergrund."""
    scheduler  = os.path.join(SCRIPT_DIR, 'auto_optimizer_scheduler.py')
    python_exe = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
    if not os.path.exists(scheduler) or not os.path.exists(python_exe):
        return
    log_path = os.path.join(SCRIPT_DIR, 'logs', 'auto_optimizer.log')
    try:
        subprocess.Popen(
            [python_exe, scheduler],
            stdout=open(log_path, 'a'),
            stderr=subprocess.STDOUT,
            cwd=SCRIPT_DIR,
        )
        logging.info("[Auto-Optimizer] Scheduler gestartet (prueft ob Optimierung faellig).")
    except Exception as e:
        logging.warning(f"[Auto-Optimizer] Konnte Scheduler nicht starten: {e}")


def main():
    settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
    secret_file   = os.path.join(SCRIPT_DIR, 'secret.json')
    run_script    = os.path.join(SCRIPT_DIR, 'src', 'vbot', 'strategy', 'run.py')
    python_exe    = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')

    if not os.path.exists(python_exe):
        python_exe = os.path.join(SCRIPT_DIR, '.venv', 'Scripts', 'python.exe')
    if not os.path.exists(python_exe):
        python_exe = sys.executable
        logging.warning(f"Kein .venv gefunden, verwende: {python_exe}")

    logging.info("=" * 55)
    logging.info("vbot Master Runner - Fibonacci Candle Overlap")
    logging.info("=" * 55)

    # Auto-Optimizer im Hintergrund starten (non-blocking)
    _run_auto_optimizer()

    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)
        with open(secret_file, 'r') as f:
            secrets = json.load(f)
    except FileNotFoundError as e:
        logging.critical(f"Datei nicht gefunden: {e}")
        return
    except json.JSONDecodeError as e:
        logging.critical(f"JSON-Fehler: {e}")
        return

    if not secrets.get('vbot'):
        logging.critical("Kein 'vbot'-Account in secret.json gefunden.")
        return

    live_settings     = settings.get('live_trading_settings', {})
    active_strategies = live_settings.get('active_strategies', [])

    if not active_strategies:
        logging.warning("Keine aktiven Strategien in settings.json.")
        return

    active = [s for s in active_strategies if s.get('active', False)]

    # --- Phase 1: Check/Repair fuer alle Symbole (wie ltbbot) ---
    logging.info("--- Phase 1: Check & Repair bestehender Positionen ---")
    check_procs = []
    for strategy in active:
        symbol    = strategy['symbol']
        timeframe = strategy['timeframe']
        logging.info(f"  Check: {symbol} ({timeframe})")
        cmd  = [python_exe, run_script, '--symbol', symbol, '--timeframe', timeframe, '--mode', 'check']
        proc = subprocess.Popen(cmd, cwd=SCRIPT_DIR)
        check_procs.append((symbol, timeframe, proc))
        time.sleep(0.3)

    for symbol, timeframe, proc in check_procs:
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            logging.error(f"  Check-Timeout: {symbol} ({timeframe}). Prozess beendet.")
            proc.kill()

    # --- Phase 2: Signal fuer alle Symbole pruefen ---
    logging.info("--- Phase 2: Signal-Pruefung & neue Trades ---")
    signal_procs = []
    for strategy in active:
        symbol    = strategy['symbol']
        timeframe = strategy['timeframe']
        logging.info(f"  Signal: {symbol} ({timeframe})")
        cmd  = [python_exe, run_script, '--symbol', symbol, '--timeframe', timeframe, '--mode', 'signal']
        proc = subprocess.Popen(cmd, cwd=SCRIPT_DIR)
        signal_procs.append((symbol, timeframe, proc))
        time.sleep(0.5)

    for symbol, timeframe, proc in signal_procs:
        try:
            proc.wait(timeout=300)
            rc = proc.returncode
            if rc != 0:
                logging.warning(f"  {symbol} ({timeframe}) beendet mit Code {rc}")
            else:
                logging.info(f"  {symbol} ({timeframe}) erfolgreich abgeschlossen.")
        except subprocess.TimeoutExpired:
            logging.error(f"  {symbol} ({timeframe}) Timeout! Prozess wird beendet.")
            proc.kill()

    logging.info("Master Runner abgeschlossen.")


if __name__ == "__main__":
    main()
