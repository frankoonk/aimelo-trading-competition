"""Records the live Bitvavo WebSocket stream for realistic replay.

Every change of the top of the book and every public trade becomes one row in a
single ``events`` table, in arrival order:

``recv_ms, ts_ms, kind, bid, bid_size, ask, ask_size, price, amount, side, trade_id``

``kind`` is ``book`` or ``trade``. ``recv_ms`` is the local receive time;
``ts_ms`` is the exchange timestamp (trades only - Bitvavo's book has none).

Two storage formats:

* **sqlite** (default) - one file, committed every second, safe against crashes.
* **parquet** - a directory of part files, flushed every few thousand rows.

Both are read back by :func:`backtester.load_recording`.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

import pandas as pd

from config import GridConfig
from exchange_ws import MarketStream, install_signal_handlers

log = logging.getLogger("recorder")

COLUMNS = ["recv_ms", "ts_ms", "kind", "bid", "bid_size", "ask", "ask_size", "price", "amount", "side", "trade_id"]
Row = tuple


class Sink(Protocol):
    path: Path

    def write(self, rows: list[Row]) -> None: ...
    def close(self) -> None: ...


class SqliteSink:
    """Append-only SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events (recv_ms INTEGER NOT NULL, ts_ms INTEGER, kind TEXT NOT NULL, "
            "bid REAL, bid_size REAL, ask REAL, ask_size REAL, price REAL, amount REAL, side TEXT, trade_id TEXT)"
        )
        self.conn.commit()

    def write(self, rows: list[Row]) -> None:
        self.conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class ParquetSink:
    """Directory of Parquet part files."""

    def __init__(self, path: Path, rows_per_part: int = 5_000) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.rows_per_part = rows_per_part
        self.buffer: list[Row] = []
        self.part = len(list(path.glob("part-*.parquet")))

    def write(self, rows: list[Row]) -> None:
        self.buffer.extend(rows)
        if len(self.buffer) >= self.rows_per_part:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        df = pd.DataFrame(self.buffer, columns=COLUMNS)
        for col in ("bid", "bid_size", "ask", "ask_size", "price", "amount"):
            df[col] = df[col].astype("float64")
        df["ts_ms"] = df["ts_ms"].astype("Int64")
        self.part += 1
        df.to_parquet(self.path / f"part-{self.part:05d}.parquet", index=False)
        self.buffer.clear()

    def close(self) -> None:
        self._flush()


def default_path(cfg: GridConfig, fmt: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"ws_{cfg.symbol.replace('/', '-')}_{stamp}"
    return Path(cfg.data_dir) / "recordings" / (name if fmt == "parquet" else f"{name}.sqlite")


async def record(cfg: GridConfig, fmt: str = "sqlite", out: Optional[str] = None,
                 duration_s: Optional[float] = None, log: Callable[[str], None] = print,
                 install_signals: bool = True,
                 on_start: Optional[Callable[[MarketStream], None]] = None) -> Path:
    """Record until ``duration_s`` elapses or SIGINT/SIGTERM arrives. Returns the output path."""
    path = Path(out) if out else default_path(cfg, fmt)
    sink: Sink = ParquetSink(path) if fmt == "parquet" else SqliteSink(path)
    pending: list[Row] = []
    totals = {"book": 0, "trade": 0}

    def on_book(bid: float, bid_size: float, ask: float, ask_size: float, recv_ms: int) -> None:
        pending.append((recv_ms, None, "book", bid, bid_size, ask, ask_size, None, None, None, None))
        totals["book"] += 1

    def on_trade(trade: dict, recv_ms: int) -> None:
        pending.append((recv_ms, trade.get("timestamp"), "trade", None, None, None, None,
                        float(trade["price"]), float(trade["amount"]), trade.get("side"), str(trade.get("id"))))
        totals["trade"] += 1

    stream = MarketStream(cfg, on_book=on_book, on_trade=on_trade)
    if install_signals:
        install_signal_handlers(stream.stop)
    if on_start:
        on_start(stream)
    log(f"Opname gestart → {path}  (Ctrl+C om te stoppen)")

    async def writer() -> None:
        started = time.monotonic()
        last_status = started
        while not stream.stop_event.is_set():
            try:
                await asyncio.wait_for(stream.stop_event.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            if pending:
                sink.write(list(pending))
                pending.clear()
            now = time.monotonic()
            if duration_s is not None and now - started >= duration_s:
                stream.stop()
            if now - last_status >= cfg.status_interval_s:
                last_status = now
                stale = stream.staleness()
                log(f"  {now - started:6.0f}s  boek-updates {totals['book']:>7}  trades {totals['trade']:>6}  "
                      f"bid/ask {stream.bid}/{stream.ask}  {'STALE: ' + stale if stale else 'vers'}")

    try:
        await asyncio.gather(stream.run(), writer())
    finally:
        if pending:
            sink.write(list(pending))
        sink.close()
    log(f"Opname gestopt: {totals['book']} boek-updates, {totals['trade']} trades, "
          f"{stream.reconnects} reconnects → {path}")
    return path
