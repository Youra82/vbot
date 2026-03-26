#!/usr/bin/env python3
# auto_optimizer_scheduler.py — vbot Auto-Optimizer-Scheduler
#
# Wird von master_runner.py beim Start non-blocking aufgerufen.
# Prueft ob eine Portfolio-Optimierung faellig ist und fuehrt sie
# automatisch aus (show_results.py --mode 3 --auto).
# Schreibt danach active_strategies in settings.json.

import json
import os
import sys
import subprocess
import logging
from datetime import datetime, timedelta

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT     = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

SETTINGS_FILE    = os.path.join(PROJECT_ROOT, 'settings.json')
OPT_RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'optimization_results.json')
CONFIGS_DIR      = os.path.join(PROJECT_ROOT, 'src', 'vbot', 'strategy', 'configs')
LAST_RUN_FILE    = os.path.join(PROJECT_ROOT, '.last_optimization_run')
IN_PROGRESS_FILE = os.path.join(PROJECT_ROOT, '.optimization_in_progress')
PYTHON_EXE       = os.path.join(PROJECT_ROOT, '.venv', 'bin', 'python3')
OPTIMIZER_PY     = os.path.join(PROJECT_ROOT, 'src', 'vbot', 'analysis', 'optimizer.py')

log_dir = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, 'auto_optimizer.log')),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"settings.json lesen fehlgeschlagen: {e}")
        return {}


def _interval_seconds(interval: dict) -> int:
    value = int(interval.get('value', 7))
    unit  = interval.get('unit', 'days')
    mult  = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
    return value * mult.get(unit, 86400)


def _is_due(schedule: dict) -> tuple:
    """Gibt (faellig, grund) zurueck."""
    now = datetime.now()

    # Verhindere parallele Laeufe (max. 2h Laufzeit)
    if os.path.exists(IN_PROGRESS_FILE):
        age = now.timestamp() - os.path.getmtime(IN_PROGRESS_FILE)
        if age < 7200:
            return False, 'in_progress'
        os.remove(IN_PROGRESS_FILE)
        log.warning("Stale in-progress-Lock entfernt.")

    # Erster Lauf
    if not os.path.exists(LAST_RUN_FILE):
        return True, 'first_run'

    with open(LAST_RUN_FILE) as f:
        last_run = datetime.fromisoformat(f.read().strip())

    # Interval-Check
    interval   = schedule.get('interval', {'value': 7, 'unit': 'days'})
    interval_s = _interval_seconds(interval)
    elapsed    = (now - last_run).total_seconds()
    if elapsed >= interval_s:
        return True, f'interval ({elapsed / 3600:.1f}h seit letztem Lauf)'

    # Wochenplan-Check (Wochentag + Stunde, 15-Min-Fenster)
    dow    = schedule.get('day_of_week', -1)
    hour   = schedule.get('hour',   -1)
    minute = schedule.get('minute',  0)
    if dow >= 0 and now.weekday() == dow and now.hour == hour:
        window_start = now.replace(minute=minute, second=0, microsecond=0)
        if abs((now - window_start).total_seconds()) <= 900:
            if (now.date() - last_run.date()).days >= 1:
                return True, f'scheduled (Wochentag {dow}, {hour:02d}:{minute:02d})'

    return False, 'not_due'


def _telegram_send(bot_token: str, chat_id: str, message: str):
    """Sendet eine plain-text Nachricht."""
    if not bot_token or not chat_id:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={'chat_id': chat_id, 'text': message},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram-Fehler: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='vbot Auto-Optimizer-Scheduler')
    parser.add_argument('--force', action='store_true',
                        help='Optimierung sofort erzwingen (ignoriert enabled + Schedule)')
    args = parser.parse_args()

    settings = _load_settings()
    opt_cfg  = settings.get('optimization_settings', {})

    if args.force:
        log.info("--force gesetzt: Optimierung wird sofort gestartet.")
        reason = 'force'
    else:
        if not opt_cfg.get('enabled', False):
            log.info("Auto-Optimizer deaktiviert (enabled: false).")
            return

        schedule = opt_cfg.get('schedule', {})
        due, reason = _is_due(schedule)

        if not due:
            log.info(f"Optimierung nicht faellig ({reason}).")
            return

    log.info("=" * 55)
    log.info(f"Starte Auto-Optimierung — Grund: {reason}")
    log.info("=" * 55)

    # Telegram-Credentials lesen
    bot_token, chat_id = '', ''
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            secrets = json.load(f)
        tg        = secrets.get('telegram', {})
        bot_token = tg.get('bot_token', '')
        chat_id   = tg.get('chat_id',   '')
    except Exception:
        pass

    send_tg = opt_cfg.get('send_telegram_on_completion', False)

    # In-progress Marker setzen
    open(IN_PROGRESS_FILE, 'w').close()

    start_time = datetime.now()
    try:
        constraints = opt_cfg.get('constraints', {})
        capital   = float(opt_cfg.get('start_capital',          1000))
        max_dd    = float(constraints.get('max_drawdown_pct',     30))
        min_wr    = float(constraints.get('min_win_rate_pct',      0))
        n_trials  = int(opt_cfg.get('num_trials',                200))
        cpu_cores = int(opt_cfg.get('cpu_cores',                   1))

        # Symbols / Timeframes: "auto" -> aus active_strategies lesen
        sym_setting = opt_cfg.get('symbols_to_optimize',    'auto')
        tf_setting  = opt_cfg.get('timeframes_to_optimize', 'auto')

        active_pairs = []
        if str(sym_setting).lower() == 'auto' or str(tf_setting).lower() == 'auto':
            for s in settings.get('live_trading_settings', {}).get('active_strategies', []):
                sym = s.get('symbol', '')
                tf  = s.get('timeframe', '')
                if sym and tf:
                    active_pairs.append((sym, tf))
        else:
            syms = sym_setting if isinstance(sym_setting, list) else [sym_setting]
            tfs  = tf_setting  if isinstance(tf_setting,  list) else [tf_setting]
            for sym in syms:
                if '/' not in sym:
                    sym = f"{sym.upper()}/USDT:USDT"
                for tf in tfs:
                    active_pairs.append((sym, tf))

        if not active_pairs:
            log.error("Keine Paare fuer Optimierung gefunden.")
            return
        log.info(f"Paare: {[f'{s}/{t}' for s,t in active_pairs]}")

        # Lookback
        lookback_setting = opt_cfg.get('lookback_days', 'auto')
        if str(lookback_setting).lower() == 'auto':
            from vbot.analysis.backtester import auto_days_for_timeframe
            lookback = max(auto_days_for_timeframe(tf) for _, tf in active_pairs)
            log.info(f"Lookback auto: {lookback} Tage")
        else:
            lookback = int(lookback_setting)

        date_from = (datetime.now() - timedelta(days=lookback)).strftime('%Y-%m-%d')
        date_to   = datetime.now().strftime('%Y-%m-%d')

        log.info(f"Kapital={capital} USDT | MaxDD={max_dd}% | MinWR={min_wr}% | "
                 f"Trials={n_trials} | Jobs={cpu_cores} | "
                 f"Zeitraum: {date_from} -> {date_to}")

        pairs_str = ', '.join(f"{sym.split('/')[0]}/{tf}" for sym, tf in active_pairs)

        if send_tg:
            _telegram_send(bot_token, chat_id,
                f"vbot Auto-Optimizer GESTARTET\n"
                f"Paare: {pairs_str}\n"
                f"Trials: {n_trials}\n"
                f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        # Alte Ergebnisse lesen BEVOR show_results.py sie ueberschreibt
        old_pnl = {}
        if os.path.exists(OPT_RESULTS_FILE):
            try:
                with open(OPT_RESULTS_FILE) as f:
                    old_data = json.load(f)
                for r in old_data.get('all_results', []):
                    old_pnl[r['filename']] = r.get('pnl_pct', 0.0)
            except Exception:
                pass

        # ── Schritt 1: Optuna-Optimizer pro Paar ──────────────────────────
        log.info(f"Starte Optuna-Optimierung fuer {len(active_pairs)} Paar(e) "
                 f"({n_trials} Trials, {cpu_cores} CPU-Kern(e))...")

        opt_failed = []
        for sym, tf in active_pairs:
            opt_cmd = [
                PYTHON_EXE, OPTIMIZER_PY,
                '--symbols',    sym,
                '--timeframes', tf,
                '--from',       date_from,
                '--to',         date_to,
                '--capital',    str(capital),
                '--trials',     str(n_trials),
                '--jobs',       str(cpu_cores),
                '--max-dd',     str(max_dd),
                '--min-wr',     str(min_wr),
            ]
            log.info(f"  Optimiere {sym} ({tf}) ...")
            opt_proc = subprocess.run(
                opt_cmd, cwd=PROJECT_ROOT,
                capture_output=True, text=True, timeout=7200,
            )
            if opt_proc.returncode != 0:
                log.error(f"  optimizer.py Fehler fuer {sym}/{tf} "
                          f"(rc={opt_proc.returncode}):\n{opt_proc.stderr[-500:]}")
                opt_failed.append(f"{sym}/{tf}")
            else:
                log.info(f"  {sym} ({tf}) — Optimierung abgeschlossen.")

        if opt_failed:
            log.warning(f"Optimizer fehlgeschlagen fuer: {opt_failed} — "
                        f"fahre mit vorhandenen Configs fort.")

        # Configs nach Optimierung ermitteln
        active_configs = []
        for sym, tf in active_pairs:
            safe  = f"{sym.replace('/', '').replace(':', '')}_{tf}"
            fname = f"config_{safe}_fibo.json"
            if os.path.exists(os.path.join(CONFIGS_DIR, fname)):
                active_configs.append(fname)
            else:
                log.warning(f"Config nach Optimierung nicht gefunden: {fname} — uebersprungen")

        if not active_configs:
            log.error("Keine Configs nach Optimierung verfuegbar.")
            return

        elapsed = (datetime.now() - start_time).total_seconds()

        # Last-run Timestamp speichern
        with open(LAST_RUN_FILE, 'w') as f:
            f.write(datetime.now().isoformat())

        if send_tg:
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            dur_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            total   = len(active_pairs)

            lines        = [f"vbot Auto-Optimizer abgeschlossen (Dauer: {dur_str})", ""]
            kept_lines   = []
            failed_lines = []

            for sym, tf in active_pairs:
                safe     = f"{sym.replace('/', '').replace(':', '')}_{tf}"
                fname    = f"config_{safe}_fibo.json"
                coin     = sym.split('/')[0]
                new_pnl_val = None
                cfg_path = os.path.join(CONFIGS_DIR, fname)
                if os.path.exists(cfg_path):
                    try:
                        with open(cfg_path) as cf:
                            new_pnl_val = json.load(cf).get('_backtest', {}).get('pnl_pct')
                    except Exception:
                        pass
                old_val = old_pnl.get(fname)
                if f"{sym}/{tf}" in opt_failed or new_pnl_val is None:
                    failed_lines.append(f"- {coin}/{tf}: Optimizer fehlgeschlagen")
                elif old_val is not None and new_pnl_val <= old_val:
                    failed_lines.append(f"- {coin}/{tf}: bestehende Config besser "
                                        f"({old_val:.2f}% vs {new_pnl_val:.2f}%) — unveraendert")
                else:
                    sign = '+' if new_pnl_val >= 0 else ''
                    kept_lines.append(f"- {coin}/{tf}: {sign}{new_pnl_val:.2f}% gespeichert")

            lines.append(f"Gespeichert ({len(kept_lines)}/{total}):")
            lines.extend(kept_lines if kept_lines else ["  — keine Verbesserung"])
            if failed_lines:
                lines.append("")
                lines.append(f"Unveraendert / Fehler ({len(failed_lines)}/{total}):")
                lines.extend(failed_lines)

            _telegram_send(bot_token, chat_id, '\n'.join(lines))

        log.info(f"Auto-Optimierung erfolgreich abgeschlossen in {elapsed / 60:.1f} min.")

    except subprocess.TimeoutExpired:
        log.error("Timeout: Optimierung hat zu lange gedauert (>120 min).")
        if send_tg:
            _telegram_send(bot_token, chat_id, "vbot Auto-Optimierung: Timeout!")
    except Exception as e:
        log.error(f"Unerwarteter Fehler: {e}", exc_info=True)
        if send_tg:
            _telegram_send(bot_token, chat_id, f"vbot Auto-Optimierung FEHLER: {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)


if __name__ == '__main__':
    main()
