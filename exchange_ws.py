"""Bitvavo access through ccxt / ccxt.pro only.

* :class:`BitvavoRest` - start-up REST calls: market metadata, maker fee and
  historical candles, each with a local cache so backtests can run offline.
* :class:`MarketStream` - the WebSocket manager used by the paper runner and
  the recorder: ``watch_order_book`` + ``watch_trades`` + ``watch_ticker`` as
  parallel asyncio tasks with reconnect/backoff and staleness tracking.

No API keys are required. If ``BITVAVO_API_KEY`` / ``BITVAVO_API_SECRET`` are
set they are used for one read-only call: the account's real maker fee tier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import ccxt
import ccxt.pro as ccxtpro
import pandas as pd

from backtester import TIMEFRAME_MS
from config import GridConfig, MarketSpec

log = logging.getLogger("exchange")

WS_METHODS = ["watchOrderBook", "watchTrades", "watchTicker", "watchOHLCV"]
REST_METHODS = ["fetchOHLCV", "fetchTradingFee", "fetchTradingFees", "fetchOrderBook", "fetchTicker"]
MAX_CANDLES_PER_CALL = 1440


def now_ms() -> int:
    return int(time.time() * 1000)


class BitvavoRest:
    """REST side: market rules, fees and cached candles."""

    def __init__(self, cfg: GridConfig) -> None:
        self.cfg = cfg
        self.data_dir = Path(cfg.data_dir)
        params: dict[str, Any] = {"enableRateLimit": True}
        if os.environ.get("BITVAVO_API_KEY") and os.environ.get("BITVAVO_API_SECRET"):
            params.update(apiKey=os.environ["BITVAVO_API_KEY"], secret=os.environ["BITVAVO_API_SECRET"])
        self.exchange = ccxt.bitvavo(params)
        self.has_keys = "apiKey" in params
        self._slug = cfg.symbol.replace("/", "-")

    # ------------------------------------------------------------ market rules
    def load_market(self, offline: bool = False) -> MarketSpec:
        """Load tick size, lot step, minimum order and default fees (cached to disk)."""
        cache = self.data_dir / f"market_{self._slug}.json"
        if not offline:
            try:
                ex, symbol = self.exchange, self.cfg.symbol
                m = ex.load_markets()[symbol]
                spec = MarketSpec(
                    symbol=symbol,
                    tick_size=float(m["precision"]["price"]),
                    amount_step=float(m["precision"]["amount"]),
                    min_cost=float(m["limits"]["cost"]["min"] or 5.0),
                    min_amount=float(m["limits"]["amount"]["min"] or 0.0),
                    maker_fee=float(m["maker"]),
                    taker_fee=float(m["taker"]),
                    price_fn=lambda p: float(ex.price_to_precision(symbol, p)),
                    amount_fn=lambda a: float(ex.amount_to_precision(symbol, a)),
                )
                self.data_dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(spec.to_dict(), indent=2))
                return spec
            except ccxt.BaseError as exc:
                log.warning("load_markets mislukt (%s); val terug op lokale cache", exc)
        if cache.exists():
            return MarketSpec.from_dict(json.loads(cache.read_text()))
        raise RuntimeError("Geen marktgegevens: Bitvavo onbereikbaar en geen lokale cache aanwezig")

    def resolve_maker_fee(self, market: MarketSpec) -> tuple[float, str]:
        """Maker fee and where it came from: override > account API > public market info > fallback."""
        if self.cfg.maker_fee_override is not None:
            return float(self.cfg.maker_fee_override), "config-override"
        if self.has_keys:
            try:
                fee = self.exchange.fetch_trading_fee(self.cfg.symbol)
                if fee.get("maker") is not None:
                    return float(fee["maker"]), "Bitvavo account-API"
            except ccxt.BaseError as exc:
                log.warning("fetch_trading_fee mislukt (%s)", exc)
        if market.maker_fee is not None:
            return float(market.maker_fee), "Bitvavo API, standaardtier"
        return float(self.cfg.maker_fee_fallback), "config-fallback"

    # ----------------------------------------------------------------- candles
    def fetch_ohlcv_cached(self, timeframe: str, start_ms: int, end_ms: int, offline: bool = False,
                           progress: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
        """Candles in ``[start_ms, end_ms)``; only ranges missing from the local Parquet cache are downloaded."""
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"onbekend timeframe {timeframe!r}; kies uit {', '.join(TIMEFRAME_MS)}")
        bar = TIMEFRAME_MS[timeframe]
        cache = self.data_dir / f"ohlcv_{self._slug}_{timeframe}.parquet"
        columns = ["ts", "open", "high", "low", "close", "volume"]
        have = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=columns)

        if not offline:
            if have.empty:
                ranges = [(start_ms, end_ms)]
            else:
                first, last = int(have["ts"].min()), int(have["ts"].max())
                ranges = [(start_ms, min(first, end_ms))] if start_ms < first else []
                if end_ms > last + bar:
                    # Re-fetch the last cached bar too: it may have been incomplete.
                    ranges.append((max(start_ms, last), end_ms))
            parts = [have]
            for lo, hi in ranges:
                parts.append(self._download(timeframe, lo, hi, progress))
            have = pd.concat([p for p in parts if not p.empty] or [have], ignore_index=True)
            have = have.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            have.to_parquet(cache, index=False)
        # Drop the still-forming candle.
        end_ms = min(end_ms, now_ms() // bar * bar)
        return have[(have["ts"] >= start_ms) & (have["ts"] < end_ms)].reset_index(drop=True)

    def _download(self, timeframe: str, start_ms: int, end_ms: int,
                  progress: Optional[Callable[[str], None]]) -> pd.DataFrame:
        bar = TIMEFRAME_MS[timeframe]
        rows: list[list[float]] = []
        cursor = start_ms // bar * bar
        while cursor < end_ms:
            batch = self.exchange.fetch_ohlcv(self.cfg.symbol, timeframe, since=cursor, limit=MAX_CANDLES_PER_CALL)
            batch = [r for r in batch if cursor <= r[0] < end_ms]
            rows.extend(batch)
            cursor = max(cursor + MAX_CANDLES_PER_CALL * bar, (batch[-1][0] + bar) if batch else 0)
            if progress:
                progress(f"  {len(rows):>7} candles t/m {pd.to_datetime(min(cursor, end_ms), unit='ms', utc=True):%Y-%m-%d %H:%M}")
        return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])

    # ------------------------------------------------------------- phase 0 check
    def capabilities(self) -> dict[str, Any]:
        """What ccxt / ccxt.pro report for Bitvavo (the ``check-ccxt`` command)."""
        pro = ccxtpro.bitvavo()
        return {
            "ccxt_version": ccxt.__version__,
            "ws": {m: bool(pro.has.get(m)) for m in WS_METHODS},
            "rest": {m: bool(self.exchange.has.get(m)) for m in REST_METHODS},
            "timeframes": list(self.exchange.timeframes),
        }


class MarketStream:
    """ccxt.pro WebSocket manager with reconnect, backoff and staleness tracking.

    Callbacks are plain synchronous functions, called on the event loop:

    * ``on_book(bid, bid_size, ask, ask_size, recv_ms)`` - on every top-of-book change
    * ``on_trade(trade, recv_ms)`` - for every new public trade (ccxt unified trade dict)
    * ``on_ticker(ticker, recv_ms)``

    Bitvavo's order book feed carries no exchange timestamp, so freshness is
    judged on two signals: the age of the last book update, and the lag of the
    exchange timestamps on trades/tickers relative to the best lag seen so far
    (which cancels out a constant local clock offset).
    """

    def __init__(self, cfg: GridConfig,
                 on_book: Optional[Callable[[float, float, float, float, int], None]] = None,
                 on_trade: Optional[Callable[[dict, int], None]] = None,
                 on_ticker: Optional[Callable[[dict, int], None]] = None) -> None:
        self.cfg = cfg
        self.symbol = cfg.symbol
        self.on_book, self.on_trade, self.on_ticker = on_book, on_trade, on_ticker
        self.ws = ccxtpro.bitvavo({"enableRateLimit": True, "newUpdates": True})
        self.stop_event = asyncio.Event()

        self.bid: Optional[float] = None
        self.ask: Optional[float] = None
        self.last_trade_price: Optional[float] = None
        self.last_book_mono: Optional[float] = None
        self.last_exchange_ts: Optional[int] = None
        self.last_exchange_mono: Optional[float] = None
        self._min_lag_ms: Optional[float] = None
        self._lag_ms = 0.0
        self.reconnects = 0
        self.counts = {"book": 0, "trade": 0, "ticker": 0}
        self._seen_trades: dict[str, None] = {}

    # ---------------------------------------------------------------- freshness
    def book_age_s(self) -> float:
        return float("inf") if self.last_book_mono is None else time.monotonic() - self.last_book_mono

    def staleness(self) -> Optional[str]:
        """``None`` when the feed is fresh, otherwise a human-readable reason."""
        limit = self.cfg.stale_after_s
        age = self.book_age_s()
        if age > limit:
            return "nog geen orderboek ontvangen" if age == float("inf") else f"orderboek {age:.0f}s stil"
        if self._min_lag_ms is not None and self._lag_ms - self._min_lag_ms > limit * 1000:
            return f"exchange-timestamps lopen {(self._lag_ms - self._min_lag_ms) / 1000:.0f}s achter"
        if self.last_exchange_mono is not None and time.monotonic() - self.last_exchange_mono > 6 * limit:
            return f"geen exchange-timestamp sinds {time.monotonic() - self.last_exchange_mono:.0f}s"
        return None

    def is_fresh(self) -> bool:
        return self.staleness() is None

    def _note_exchange_ts(self, ts: Optional[int], recv_ms: int) -> None:
        if not ts:
            return
        self.last_exchange_mono = time.monotonic()
        if self.last_exchange_ts is None or ts >= self.last_exchange_ts:
            self.last_exchange_ts = int(ts)
            self._lag_ms = recv_ms - ts
            self._min_lag_ms = self._lag_ms if self._min_lag_ms is None else min(self._min_lag_ms, self._lag_ms)

    # ------------------------------------------------------------------ handlers
    def _handle_book(self, book: dict) -> None:
        if not book["bids"] or not book["asks"]:
            return
        recv = now_ms()
        bid, bid_size = float(book["bids"][0][0]), float(book["bids"][0][1])
        ask, ask_size = float(book["asks"][0][0]), float(book["asks"][0][1])
        self.last_book_mono = time.monotonic()
        self._note_exchange_ts(book.get("timestamp"), recv)
        if ask <= bid:
            return  # crossed snapshot mid-update: ignore
        changed = bid != self.bid or ask != self.ask
        self.bid, self.ask = bid, ask
        self.counts["book"] += 1
        if changed and self.on_book:
            self.on_book(bid, bid_size, ask, ask_size, recv)

    def _handle_trades(self, trades: list[dict]) -> None:
        recv = now_ms()
        for trade in trades:
            trade_id = trade.get("id")
            if trade_id is not None:
                if trade_id in self._seen_trades:
                    continue  # replayed after a reconnect
                self._seen_trades[trade_id] = None
            if len(self._seen_trades) > 5000:
                for key in list(self._seen_trades)[:2500]:
                    del self._seen_trades[key]
            self._note_exchange_ts(trade.get("timestamp"), recv)
            self.last_trade_price = float(trade["price"])
            self.counts["trade"] += 1
            if self.on_trade:
                self.on_trade(trade, recv)

    def _handle_ticker(self, ticker: dict) -> None:
        recv = now_ms()
        self._note_exchange_ts(ticker.get("timestamp"), recv)
        self.counts["ticker"] += 1
        if self.on_ticker:
            self.on_ticker(ticker, recv)

    # --------------------------------------------------------------------- loops
    async def _loop(self, name: str, watch: Callable[[], Any], handle: Callable[[Any], None],
                    timeout: Optional[float]) -> None:
        backoff = self.cfg.reconnect_backoff_min_s
        while not self.stop_event.is_set():
            try:
                data = await asyncio.wait_for(watch(), timeout) if timeout else await watch()
                backoff = self.cfg.reconnect_backoff_min_s
                handle(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, timeouts, exchange hiccups
                self.reconnects += 1
                reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
                log.warning("WebSocket %s onderbroken (%s); opnieuw verbinden over %.0fs", name, reason[:120], backoff)
                if isinstance(exc, asyncio.TimeoutError):
                    try:  # a silent socket: drop it so the next watch call reconnects
                        await self.ws.close()
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(self.stop_event.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.cfg.reconnect_backoff_max_s)

    async def run(self) -> None:
        """Run the three streams until :meth:`stop` is called."""
        silent = max(3 * self.cfg.stale_after_s, 15.0)
        tasks = [
            asyncio.create_task(self._loop("orderbook", lambda: self.ws.watch_order_book(self.symbol), self._handle_book, silent)),
            asyncio.create_task(self._loop("trades", lambda: self.ws.watch_trades(self.symbol), self._handle_trades, None)),
            asyncio.create_task(self._loop("ticker", lambda: self.ws.watch_ticker(self.symbol), self._handle_ticker, None)),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.ws.close()
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()


def install_signal_handlers(stop: Callable[[], None]) -> None:
    """Call ``stop`` on SIGINT/SIGTERM so sessions shut down gracefully."""
    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:  # Windows event loop
            signal.signal(sig, lambda *_: stop())
