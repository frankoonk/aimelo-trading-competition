"""Replay engine: historical candles or recorded WebSocket ticks.

Both modes drive the same :class:`grid_engine.GridEngine` and produce the same
:class:`results.RunResult`.

Candle replay
    Each candle is walked as a continuous path ``open -> low -> high -> close``
    (bullish candle) or ``open -> high -> low -> close`` (bearish candle). That
    is the conventional, deterministic assumption for intrabar order; a finer
    timeframe makes it matter less. Fills happen at the order's own limit price.

Tick replay
    Recorded top-of-book updates and public trades are replayed in arrival
    order through ``on_quote`` / ``on_trade`` - the exact code path of live
    paper trading, including the postOnly check against the recorded book.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from config import GridConfig, MarketSpec
from grid_engine import GridEngine
from results import EquityRecorder, RunResult

TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def run_candle_backtest(
    cfg: GridConfig,
    market: MarketSpec,
    maker_fee: float,
    candles: pd.DataFrame,
    fee_source: str = "config",
    timeframe: str = "1m",
) -> RunResult:
    """Replay OHLCV candles (columns ``ts, open, high, low, close`` with ``ts`` in ms)."""
    if len(candles) < 2:
        raise ValueError("need at least two candles to run a backtest")
    candles = candles.sort_values("ts").reset_index(drop=True)
    ts_arr = candles["ts"].to_numpy(dtype="int64")
    o_arr, h_arr = candles["open"].to_numpy(float), candles["high"].to_numpy(float)
    l_arr, c_arr = candles["low"].to_numpy(float), candles["close"].to_numpy(float)

    start_ts, start_price = int(ts_arr[0]), float(o_arr[0])
    engine = GridEngine(cfg, market, maker_fee, start_price, start_ts)
    recorder = EquityRecorder(cfg, engine, start_ts, start_price)
    bar_ms = TIMEFRAME_MS.get(timeframe, 0)

    for i in range(len(ts_arr)):
        ts = int(ts_arr[i])
        o, h, l, c = float(o_arr[i]), float(h_arr[i]), float(l_arr[i]), float(c_arr[i])
        engine.on_path(ts, o)
        if c >= o:
            engine.on_path(ts, l)
            engine.on_path(ts, h)
        else:
            engine.on_path(ts, h)
            engine.on_path(ts, l)
        engine.on_path(ts, c)
        recorder.record(ts + bar_ms, c)

    ohlc = candles[["open", "high", "low", "close"]].copy()
    ohlc.index = pd.to_datetime(candles["ts"], unit="ms", utc=True)
    return recorder.result(
        "backtest-candles", cfg, market, fee_source, ohlc=ohlc,
        meta={"source": f"Bitvavo {cfg.symbol} {timeframe}-candles", "timeframe": timeframe, "n_candles": len(candles)},
    )


def load_recording(path: str | Path) -> pd.DataFrame:
    """Load a WebSocket recording made by :mod:`recorder` (SQLite file, Parquet file or Parquet directory)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir() or path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        with sqlite3.connect(path) as conn:
            df = pd.read_sql_query("SELECT * FROM events ORDER BY rowid", conn)
    return df.sort_values("recv_ms", kind="stable").reset_index(drop=True)


def run_tick_backtest(
    cfg: GridConfig,
    market: MarketSpec,
    maker_fee: float,
    events: pd.DataFrame,
    fee_source: str = "config",
    source: str = "opname",
) -> RunResult:
    """Replay recorded top-of-book updates (``kind == 'book'``) and trades (``kind == 'trade'``)."""
    engine: Optional[GridEngine] = None
    recorder: Optional[EquityRecorder] = None
    interval_ms = max(1, int(cfg.snapshot_interval_s * 1000))
    next_snapshot = 0
    last_ts = 0
    n_book = n_trade = 0

    for kind, recv_ms, bid, ask, price in zip(
        events["kind"].to_numpy(), events["recv_ms"].to_numpy(), events["bid"].to_numpy(float),
        events["ask"].to_numpy(float), events["price"].to_numpy(float),
    ):
        ts = int(recv_ms)
        last_ts = ts
        if kind == "book":
            if not (bid > 0 and ask > bid):
                continue
            n_book += 1
            if engine is None:
                bid, ask = float(bid), float(ask)
                mid = (bid + ask) / 2
                engine = GridEngine(cfg, market, maker_fee, mid, ts, bid=bid, ask=ask)
                recorder = EquityRecorder(cfg, engine, ts, mid)
                next_snapshot = ts + interval_ms
                continue
            engine.on_quote(ts, float(bid), float(ask))
        elif kind == "trade" and engine is not None and price > 0:
            n_trade += 1
            engine.on_trade(ts, float(price))
        if engine is not None and recorder is not None and ts >= next_snapshot:
            recorder.record(ts, (engine.bid + engine.ask) / 2)
            next_snapshot = ts + interval_ms

    if engine is None or recorder is None:
        raise ValueError("recording contains no usable order book updates")
    recorder.record(last_ts, (engine.bid + engine.ask) / 2)
    return recorder.result(
        "backtest-ticks", cfg, market, fee_source,
        meta={"source": f"WebSocket-opname {source}", "n_book_updates": n_book, "n_trades": n_trade},
    )
