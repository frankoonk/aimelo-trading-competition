"""Tests for the benchmarks, both replay modes and the dashboard generator."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtester import run_candle_backtest, run_tick_backtest
from benchmarks import BenchmarkTracker
from config import GridConfig, MarketSpec
from dashboard import build_html
from results import compute_metrics

HOUR = 3_600_000


def test_benchmarks_start_equal_and_follow_their_definitions():
    b = BenchmarkTracker(0.1, 5_000, 50_000, 0, rebalance_interval_hours=24, fee_pct=0.0)
    assert (b.last.idle, b.last.rebalance, b.last.hold) == (10_000, 10_000, 10_000)
    v = b.update(HOUR, 60_000)  # +20%, before the first rebalance
    assert v.idle == pytest.approx(11_000) and v.rebalance == pytest.approx(11_000) and v.hold == pytest.approx(12_000)
    b.update(24 * HOUR, 60_000)  # rebalance back to 50/50 at 60k
    assert b.rebalances == 1
    assert b.rebal_btc * 60_000 == pytest.approx(b.rebal_eur)
    v = b.update(25 * HOUR, 50_000)  # round trip: rebalancing harvested the swing, idle did not
    assert v.idle == pytest.approx(10_000)
    assert v.rebalance > v.idle
    assert v.hold == pytest.approx(10_000)


def test_rebalance_fee_is_charged_on_traded_notional():
    b = BenchmarkTracker(0.1, 5_000, 50_000, 0, rebalance_interval_hours=1, fee_pct=0.01)
    b.update(HOUR, 60_000)  # BTC worth 6000 vs target 5500 -> sells 500
    assert b.rebalance_fees == pytest.approx(5.0)


def synthetic_candles(n: int = 3_000, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 60_000 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    open_ = np.concatenate([[60_000], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.001, n)))
    return pd.DataFrame({"ts": np.arange(n) * 60_000, "open": open_, "high": high, "low": low, "close": close, "volume": 1.0})


def test_candle_backtest_is_deterministic_and_consistent():
    candles = synthetic_candles()
    a = run_candle_backtest(GridConfig(), MarketSpec(), 0.0015, candles)
    b = run_candle_backtest(GridConfig(), MarketSpec(), 0.0015, candles)
    assert [(f.ts, f.level, f.side) for f in a.fills] == [(f.ts, f.level, f.side) for f in b.fills]
    assert a.equity.equals(b.equity)
    m = compute_metrics(a)
    assert m["n_fills"] == len(a.fills) > 0 and m["n_cycles"] > 0
    assert m["start_capital"] == pytest.approx(10_000, abs=0.01)
    assert m["alpha_eur"] == pytest.approx(m["final_equity"] - m["lines"]["rebalance"]["final"])
    assert (a.equity["btc"] >= 0).all() and (a.equity["eur"] >= 0).all()
    assert a.equity["btc_weight"].between(0.2, 0.8).all()


def test_tick_backtest_and_dashboard_render():
    rng = np.random.default_rng(11)
    rows, price = [], 60_000.0
    for i in range(6_000):
        price *= float(np.exp(rng.normal(0, 0.0008)))
        p = round(price)
        if i % 2 == 0:
            rows.append((i * 1000, None, "book", p - 1, 1.0, p, 1.0, None, None, None, None))
        else:
            rows.append((i * 1000, i * 1000, "trade", None, None, None, None, p, 0.01, "buy", str(i)))
    events = pd.DataFrame(rows, columns=["recv_ms", "ts_ms", "kind", "bid", "bid_size", "ask", "ask_size", "price", "amount", "side", "trade_id"])
    result = run_tick_backtest(GridConfig(maker_fee_override=0.0), MarketSpec(), 0.0, events)
    assert result.fills and result.cycles
    page = build_html(result, inline_js=False)
    for marker in ("chart-equity", "chart-price", "chart-inventory", "chart-drawdown", "fills-body", "Grid-Alpha"):
        assert marker in page
