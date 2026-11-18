"""Live paper trading on the Bitvavo WebSocket stream. No API keys needed.

REST is used once at start-up (market rules, maker fee). After that everything
is event-driven:

* ``watch_order_book`` -> ``engine.on_quote``: best bid/ask for the postOnly
  rule, and fills when the book moves through one of our resting orders.
* ``watch_trades`` -> ``engine.on_trade``: fills when a real market trade prints
  through one of our resting orders.
* ``watch_ticker``: exchange timestamps for staleness monitoring.

While the feed is stale, trades are not passed to the engine: a fill simulated
against an outdated book could not be trusted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from config import GridConfig, MarketSpec
from exchange_ws import BitvavoRest, MarketStream, install_signal_handlers, now_ms
from grid_engine import Fill, GridEngine
from results import EquityRecorder, RunResult

log = logging.getLogger("paper")


class PaperSession:
    """One paper-trading session: engine + stream + periodic sampling and state saves."""

    def __init__(self, cfg: GridConfig, duration_s: Optional[float] = None, market: Optional[MarketSpec] = None,
                 maker_fee: Optional[float] = None, fee_source: str = "config",
                 log: Callable[[str], None] = print, install_signals: bool = True) -> None:
        self.cfg = cfg
        self.duration_s = duration_s
        self.log = log
        self.install_signals = install_signals
        if market is None or maker_fee is None:  # blocking REST; the GUI server passes these in instead
            rest = BitvavoRest(cfg)
            market = rest.load_market()
            maker_fee, fee_source = rest.resolve_maker_fee(market)
        self.market, self.maker_fee, self.fee_source = market, maker_fee, fee_source
        self.stream = MarketStream(cfg, on_book=self._on_book, on_trade=self._on_trade)
        self.engine: Optional[GridEngine] = None
        self.recorder: Optional[EquityRecorder] = None
        self.started_at = datetime.now(timezone.utc)
        self.state_path = Path(cfg.data_dir) / f"paper_state_{self.started_at:%Y%m%d_%H%M%S}.json"
        self.skipped_stale_trades = 0
        self.stale_seconds = 0.0
        self._stale_reason: Optional[str] = "nog geen orderboek ontvangen"

    # ------------------------------------------------------------ stream events
    def _on_book(self, bid: float, bid_size: float, ask: float, ask_size: float, recv_ms: int) -> None:
        if self.engine is None:
            mid = (bid + ask) / 2
            self.engine = GridEngine(self.cfg, self.market, self.maker_fee, mid, recv_ms, bid=bid, ask=ask)
            self.recorder = EquityRecorder(self.cfg, self.engine, recv_ms, mid)
            lo, hi = self.engine.window
            self.log(f"Grid geplaatst rond € {mid:,.0f}: {len(self.engine.orders)} maker-orders "
                  f"tussen € {lo:,.0f} en € {hi:,.0f}, stap {self.engine.step * 100:.3f}%, "
                  f"€ {self.engine.order_size:,.2f} per order")
            return
        self._report(self.engine.on_quote(recv_ms, bid, ask))

    def _on_trade(self, trade: dict, recv_ms: int) -> None:
        if self.engine is None:
            return
        if not self.stream.is_fresh():
            self.skipped_stale_trades += 1
            return
        self._report(self.engine.on_trade(int(trade.get("timestamp") or recv_ms), float(trade["price"])))

    def _report(self, fills: list[Fill]) -> None:
        for f in fills:
            pnl = f"  cyclus +€ {f.cycle_pnl_eur:.3f}" if f.cycle_pnl_eur is not None else ""
            self.log(f"  {datetime.fromtimestamp(f.ts / 1000, timezone.utc):%H:%M:%S}  "
                  f"{'BUY ' if f.side.value == 'buy' else 'SELL'} {f.amount:.8f} BTC @ € {f.price:,.0f}  "
                  f"(fee € {f.fee:.3f}){pnl}")

    # ------------------------------------------------------------ periodic work
    def _mid(self) -> Optional[float]:
        s = self.stream
        return None if s.bid is None or s.ask is None else (s.bid + s.ask) / 2

    def save_state(self) -> None:
        """Write the current state atomically (so an interrupted write never corrupts it)."""
        if self.engine is None:
            return
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "started_at": self.started_at.isoformat(),
            "symbol": self.cfg.symbol,
            "config": self.cfg.to_dict(),
            "engine": self.engine.state_dict(self._mid()),
            "stream": {"reconnects": self.stream.reconnects, "counts": self.stream.counts,
                       "stale_seconds": round(self.stale_seconds, 1), "skipped_stale_trades": self.skipped_stale_trades},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_path)

    def _status(self, elapsed: float) -> None:
        e, mid = self.engine, self._mid()
        if e is None or mid is None or self.recorder is None:
            self.log(f"  {elapsed:6.0f}s  wachten op orderboek…")
            return
        bench = self.recorder.benchmarks.last
        equity = e.equity(mid)
        halted = ", ".join(f"{k}:{v}" for k, v in e.halted.items() if v) or "-"
        self.log(f"  {elapsed:6.0f}s  € {mid:,.0f}  equity € {equity:,.2f}  alpha vs herbal. € {equity - bench.rebalance:+.2f}  "
              f"BTC {e.btc_weight(mid) * 100:.1f}%  fills {len(e.fills)}  cycli {len(e.cycles)}  "
              f"orders {len(e.orders)}  stop: {halted}"
              + (f"  [STALE: {self._stale_reason}]" if self._stale_reason else ""))

    async def _ticker(self) -> None:
        cfg, stream = self.cfg, self.stream
        started = last_status = last_save = last_tick = time.monotonic()
        while not stream.stop_event.is_set():
            try:
                await asyncio.wait_for(stream.stop_event.wait(), cfg.snapshot_interval_s)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            reason = stream.staleness()
            if reason:
                self.stale_seconds += now - last_tick
            if bool(reason) != bool(self._stale_reason) and self.engine is not None:
                self.log(f"  ⚠ feed verouderd ({reason}); gesimuleerde fills gepauzeerd" if reason
                      else "  ✓ feed weer vers; fills hervat")
            self._stale_reason = reason
            last_tick = now
            mid = self._mid()
            if self.recorder is not None and mid is not None and not reason:
                self.recorder.record(now_ms(), mid)
            if now - last_status >= cfg.status_interval_s:
                last_status = now
                self._status(now - started)
            if now - last_save >= cfg.state_save_interval_s:
                last_save = now
                self.save_state()
            if self.duration_s is not None and now - started >= self.duration_s:
                stream.stop()

    # --------------------------------------------------------------------- run
    async def run(self) -> Optional[RunResult]:
        """Run until the duration elapses or SIGINT/SIGTERM; returns the result (``None`` if no data arrived)."""
        if self.install_signals:
            install_signal_handlers(self.stream.stop)
        self.log(f"Paper-trading {self.cfg.symbol} gestart - maker fee {self.maker_fee * 100:.3f}% ({self.fee_source}). "
              f"Ctrl+C stopt en maakt het dashboard.")
        try:
            await asyncio.gather(self.stream.run(), self._ticker())
        finally:
            self.save_state()
        if self.engine is None or self.recorder is None:
            return None
        mid = self._mid() or self.engine.last_price
        self.recorder.record(now_ms(), mid)
        return self.recorder.result(
            "paper", self.cfg, self.market, self.fee_source,
            meta={"source": "Bitvavo WebSocket (live)", "reconnects": self.stream.reconnects,
                  "stale_seconds": round(self.stale_seconds, 1), "skipped_stale_trades": self.skipped_stale_trades,
                  "stream_counts": dict(self.stream.counts), "state_file": str(self.state_path)},
        )
