# src/vbot/analysis/backtester.py
# vbot — Backtester fuer Fibonacci Candle Overlap Strategie
#
# Simulates the strategy on historical OHLCV data.
# Walk-forward: for each bar, compute signal on previous bars, then trade.

import os
import sys
import json
import logging
import time as time_mod
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from vbot.strategy.fibo_logic import get_fibo_signal

logger = logging.getLogger(__name__)

MIN_NOTIONAL_USDT = 5.0
FEE_PCT           = 0.06 / 100   # Bitget Taker-Gebuehr (je Seite)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    bar_idx:    int
    timestamp:  object
    direction:  str
    entry:      float
    sl:         float
    tp:         float
    contracts:  float
    fibo_level: float
    exit_price: float = 0.0
    exit_bar:   int   = 0
    result:     str   = "open"    # "win" | "loss" | "open"
    pnl_usdt:   float = 0.0
    pnl_pct:    float = 0.0
    hold_bars:  int   = 0


@dataclass
class BacktestResult:
    symbol:        str
    timeframe:     str
    start_capital: float
    end_capital:   float
    trades: List[BacktestTrade] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len([t for t in self.trades if t.result != "open"])

    @property
    def wins(self) -> int:
        return len([t for t in self.trades if t.result == "win"])

    @property
    def losses(self) -> int:
        return len([t for t in self.trades if t.result == "loss"])

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades * 100 if self.total_trades else 0.0

    @property
    def pnl_pct(self) -> float:
        return (self.end_capital - self.start_capital) / self.start_capital * 100

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        equity = self.start_capital
        peak   = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.pnl_usdt
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def avg_rr(self) -> float:
        finished = [t for t in self.trades if t.result != "open"]
        if not finished:
            return 0.0
        rrs = []
        for t in finished:
            risk   = abs(t.entry - t.sl)
            reward = abs(t.exit_price - t.entry)
            if risk > 0:
                rrs.append(reward / risk)
        return float(np.mean(rrs)) if rrs else 0.0

    def summary(self) -> str:
        return (
            f"=== vbot Backtest: {self.symbol} ({self.timeframe}) ===\n"
            f"Kapital    : {self.start_capital:.2f} -> {self.end_capital:.2f} USDT "
            f"({self.pnl_pct:+.2f}%)\n"
            f"Trades     : {self.total_trades} | W:{self.wins} L:{self.losses} "
            f"| WR: {self.win_rate:.1f}%\n"
            f"Max DD     : {self.max_drawdown_pct:.2f}%\n"
            f"Avg R:R    : 1:{self.avg_rr:.2f}\n"
        )


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, config: dict,
                  start_capital: float = 1000.0,
                  symbol: str = "UNKNOWN",
                  timeframe: str = "1h") -> BacktestResult:
    """
    Walk-forward backtest.
    Fuer jede Kerze: Signal auf df[:i] berechnen, dann in Kerze i traden.
    """
    risk_cfg  = config.get('risk', {})
    sig_cfg   = config.get('signal', {})
    leverage  = int(risk_cfg.get('leverage', 10))
    risk_pct  = float(risk_cfg.get('risk_per_trade_pct', 1.0))
    warmup    = max(5, int(sig_cfg.get('confirm_overlap_window', 0)) + 3)

    result = BacktestResult(
        symbol=symbol, timeframe=timeframe,
        start_capital=start_capital, end_capital=start_capital
    )

    capital    = start_capital
    open_trade: Optional[BacktestTrade] = None

    high_arr = df['high'].values
    low_arr  = df['low'].values
    open_arr = df['open'].values
    timestamps = df.index

    for i in range(warmup, len(df)):
        ts = timestamps[i]

        # --- Offenen Trade verwalten ---
        if open_trade is not None:
            high_i = high_arr[i]
            low_i  = low_arr[i]

            hit_sl = hit_tp = False

            if open_trade.direction == 'long':
                if low_i  <= open_trade.sl:
                    hit_sl, exit_p = True, open_trade.sl
                elif high_i >= open_trade.tp:
                    hit_tp, exit_p = True, open_trade.tp
            else:
                if high_i >= open_trade.sl:
                    hit_sl, exit_p = True, open_trade.sl
                elif low_i  <= open_trade.tp:
                    hit_tp, exit_p = True, open_trade.tp

            if hit_sl or hit_tp:
                price_diff = exit_p - open_trade.entry
                if open_trade.direction == 'short':
                    price_diff = -price_diff

                notional  = open_trade.contracts * open_trade.entry
                fees_usdt = notional * FEE_PCT * 2
                pnl_usdt  = price_diff * open_trade.contracts - fees_usdt
                pnl_pct   = pnl_usdt / capital * 100

                open_trade.exit_price = exit_p
                open_trade.exit_bar   = i
                open_trade.result     = 'win' if hit_tp else 'loss'
                open_trade.pnl_usdt   = pnl_usdt
                open_trade.pnl_pct    = pnl_pct
                open_trade.hold_bars  = i - open_trade.bar_idx

                capital += pnl_usdt
                result.trades.append(open_trade)
                open_trade = None

                if capital <= 0:
                    logger.warning("Kapital auf 0 gefallen. Backtest beendet.")
                    break

            if open_trade is not None:
                continue

        # --- Signal berechnen auf abgeschlossenen Kerzen (df[:i]) ---
        signal = get_fibo_signal(df.iloc[:i], sig_cfg)

        if signal['side'] is None:
            continue

        # Entry am Open der aktuellen Kerze
        entry_price = float(open_arr[i])
        sl_price    = signal['sl_price']
        tp_price    = signal['tp_price']

        # Positionsgroesse risiko-basiert
        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            continue

        risk_amount = capital * risk_pct / 100.0
        contracts   = risk_amount / sl_distance
        notional    = contracts * entry_price

        if notional < MIN_NOTIONAL_USDT:
            continue

        # Margin-Pruefung: Pflichtmarge darf verfuegbares Kapital nicht uebersteigen
        margin = notional / leverage
        if margin > capital:
            continue

        open_trade = BacktestTrade(
            bar_idx=i,
            timestamp=ts,
            direction=signal['side'],
            entry=entry_price,
            sl=sl_price,
            tp=tp_price,
            contracts=contracts,
            fibo_level=signal.get('fibo_level', 0.618),
        )
        logger.debug(f"[{ts}] {signal['side'].upper()} Entry @ {entry_price:.4f} | "
                     f"SL {sl_price:.4f} | TP {tp_price:.4f} | Fibo {signal['fibo_level']}")

    # Offenen Trade am Ende mit letztem Close-Preis schliessen
    if open_trade is not None:
        last_close = float(df['close'].iloc[-1])
        price_diff = last_close - open_trade.entry
        if open_trade.direction == 'short':
            price_diff = -price_diff
        notional_last = open_trade.contracts * open_trade.entry
        fees_last     = notional_last * FEE_PCT * 2
        pnl_usdt = price_diff * open_trade.contracts - fees_last
        open_trade.exit_price = last_close
        open_trade.exit_bar   = len(df) - 1
        open_trade.result     = 'open'
        open_trade.pnl_usdt   = pnl_usdt
        open_trade.hold_bars  = len(df) - 1 - open_trade.bar_idx
        capital += pnl_usdt
        result.trades.append(open_trade)

    result.end_capital = capital
    logger.info(result.summary())
    return result


# ---------------------------------------------------------------------------
# Timeframe → empfohlene Backtest-Tage
# ---------------------------------------------------------------------------

DAYS_BY_TIMEFRAME = {
    "1m":  30,
    "3m":  60,
    "5m":  90,
    "15m": 90,
    "30m": 365,
    "1h":  365,
    "2h":  730,
    "4h":  730,
    "6h":  1095,
    "8h":  1095,
    "12h": 1095,
    "1d":  1095,
    "3d":  1825,
    "1w":  1825,
}


def auto_days_for_timeframe(timeframe: str) -> int:
    """Gibt die empfohlene Anzahl historischer Tage fuer den Timeframe zurueck."""
    return DAYS_BY_TIMEFRAME.get(timeframe, 365)


# ---------------------------------------------------------------------------
# Data loading with cache
# ---------------------------------------------------------------------------

def load_ohlcv(symbol: str, timeframe: str,
               start_date: str, end_date: str) -> pd.DataFrame:
    """
    Laedt OHLCV-Daten fuer einen Datumsbereich.
    Nutzt einen lokalen CSV-Cache (data/cache/) um wiederholte Downloads zu vermeiden.

    Args:
        symbol:     z.B. "BTC/USDT:USDT"
        timeframe:  z.B. "1h"
        start_date: "YYYY-MM-DD"
        end_date:   "YYYY-MM-DD"
    """
    import ccxt

    cache_dir = os.path.join(PROJECT_ROOT, 'data', 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    safe_symbol = symbol.replace('/', '-').replace(':', '-')
    cache_file  = os.path.join(cache_dir, f"{safe_symbol}_{timeframe}.csv")

    req_start = pd.to_datetime(start_date, utc=True)
    req_end   = pd.to_datetime(end_date + 'T23:59:59Z', utc=True)

    cached = pd.DataFrame()

    # Cache lesen
    if os.path.exists(cache_file):
        try:
            cached = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            cached.index = cached.index.tz_localize('UTC') if cached.index.tz is None \
                           else cached.index.tz_convert('UTC')
            cached.sort_index(inplace=True)
            cached = cached[~cached.index.duplicated(keep='last')]

            if cached.index.min() <= req_start and cached.index.max() >= req_end:
                logger.info(f"Cache-Hit: {symbol} ({timeframe}) [{start_date} -> {end_date}]")
                return cached.loc[req_start:req_end].copy()
            else:
                logger.info(f"Cache unvollstaendig — lade fehlende Daten nach.")
        except Exception as e:
            logger.warning(f"Cache-Lesefehler ({cache_file}): {e} — lade neu.")
            cached = pd.DataFrame()

    # Von Bitget herunterladen (kein API-Key noetig fuer OHLCV)
    logger.info(f"Download: {symbol} ({timeframe}) [{start_date} -> {end_date}] ...")
    exchange = ccxt.bitget({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    exchange.load_markets()
    tf_ms     = exchange.parse_timeframe(timeframe) * 1000
    since_ms  = int(exchange.parse8601(start_date + 'T00:00:00Z'))
    end_ms    = int(exchange.parse8601(end_date   + 'T23:59:59Z'))
    all_ohlcv = []

    retries = 0
    while since_ms < end_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since_ms, 200)
            if not ohlcv:
                break
            ohlcv = [c for c in ohlcv if c[0] <= end_ms]
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since_ms = ohlcv[-1][0] + tf_ms
            retries  = 0
            time_mod.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            err_str = str(e)
            # Bitget 40017: startTime zu weit zurueck
            if '40017' in err_str and retries < 3:
                skip_ms = 30 * 24 * 3600 * 1000
                logger.warning(f"Bitget startTime-Fehler — ueberspringe 30 Tage vorwaerts. ({retries+1}/3)")
                since_ms += skip_ms
                retries += 1
            else:
                logger.warning(f"Download-Fehler: {e}")
                break

    if not all_ohlcv:
        logger.error("Keine Daten heruntergeladen.")
        return pd.DataFrame()

    new_df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms', utc=True)
    new_df.set_index('timestamp', inplace=True)
    new_df.sort_index(inplace=True)
    new_df = new_df[~new_df.index.duplicated(keep='last')]

    # Cache aktualisieren
    if not cached.empty:
        merged = pd.concat([cached, new_df])
        merged = merged[~merged.index.duplicated(keep='last')]
        merged.sort_index(inplace=True)
    else:
        merged = new_df

    try:
        merged.to_csv(cache_file)
        logger.info(f"Cache gespeichert: {cache_file} ({len(merged)} Kerzen gesamt)")
    except Exception as e:
        logger.warning(f"Cache-Schreibfehler: {e}")

    return new_df.loc[req_start:req_end].copy()
