"""Run results shared by the backtester, the paper runner and the dashboard."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from benchmarks import BenchmarkTracker
from config import GridConfig, MarketSpec
from grid_engine import Cycle, Fill, GridEngine, GridEvent

EQUITY_COLUMNS = [
    "ts", "price", "btc", "eur", "equity", "btc_weight", "grid_lo", "grid_hi",
    "bench_idle", "bench_rebal", "bench_hold",
]


@dataclass
class RunResult:
    """Everything a finished (or interrupted) run produced."""

    mode: str  # backtest-candles | backtest-ticks | paper
    config: GridConfig
    market: MarketSpec
    maker_fee: float
    fee_source: str
    step_pct: float
    start_price: float
    initial_btc: float
    initial_eur: float
    equity: pd.DataFrame  # UTC DatetimeIndex + EQUITY_COLUMNS[1:]
    fills: list[Fill]
    cycles: list[Cycle]
    events: list[GridEvent]
    ohlc: Optional[pd.DataFrame] = None  # UTC DatetimeIndex, open/high/low/close
    meta: dict[str, Any] = field(default_factory=dict)


class EquityRecorder:
    """Samples the engine and the benchmarks on one shared timeline."""

    def __init__(self, cfg: GridConfig, engine: GridEngine, start_ts_ms: int, start_price: float) -> None:
        fee = engine.fee if cfg.benchmark_fee_pct is None else cfg.benchmark_fee_pct
        self.engine = engine
        self.start_price = start_price
        self.benchmarks = BenchmarkTracker(
            engine.initial_btc, engine.initial_eur, start_price, start_ts_ms, cfg.rebalance_interval_hours, fee
        )
        self.rows: list[tuple] = []
        self.record(start_ts_ms, start_price)

    def record(self, ts_ms: int, price: float) -> None:
        e = self.engine
        bench = self.benchmarks.update(ts_ms, price)
        lo, hi = e.window
        value = e.btc * price
        self.rows.append(
            (ts_ms, price, e.btc, e.eur, value + e.eur, value / (value + e.eur), lo, hi, bench.idle, bench.rebalance, bench.hold)
        )

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.rows, columns=EQUITY_COLUMNS)
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        return df[~df.index.duplicated(keep="last")]

    def result(self, mode: str, cfg: GridConfig, market: MarketSpec, fee_source: str,
               ohlc: Optional[pd.DataFrame] = None, meta: Optional[dict[str, Any]] = None) -> RunResult:
        e = self.engine
        info = {"rebalances": self.benchmarks.rebalances, "rebalance_fees": self.benchmarks.rebalance_fees,
                "final_state": e.state_dict(self.rows[-1][1]), "rejected_orders": e.rejected}
        info.update(meta or {})
        return RunResult(
            mode=mode, config=cfg, market=market, maker_fee=e.fee, fee_source=fee_source, step_pct=e.step,
            start_price=self.start_price, initial_btc=e.initial_btc, initial_eur=e.initial_eur,
            equity=self.frame(), fills=list(e.fills), cycles=list(e.cycles), events=list(e.events),
            ohlc=ohlc, meta=info,
        )


def drawdown(series: pd.Series) -> pd.Series:
    """Underwater curve as a fraction (0 at a new high, negative below it)."""
    return series / series.cummax() - 1.0


def sharpe_ratio(series: pd.Series) -> Optional[float]:
    """Annualised Sharpe ratio (risk-free rate 0) on a bar size that fits the run length.

    Daily bars for runs of a month or more, hourly for two days or more, else
    minute bars. Returns ``None`` when there is too little data to be meaningful.
    """
    if len(series) < 3:
        return None
    span = series.index[-1] - series.index[0]
    if span >= pd.Timedelta(days=30):
        rule, per_year = "1D", 365.0
    elif span >= pd.Timedelta(days=2):
        rule, per_year = "1h", 365.0 * 24
    else:
        rule, per_year = "1min", 365.0 * 24 * 60
    returns = series.resample(rule).last().dropna().pct_change().dropna()
    if len(returns) < 10 or returns.std() == 0 or math.isnan(returns.std()):
        return None
    return float(returns.mean() / returns.std() * math.sqrt(per_year))


def compute_metrics(result: RunResult) -> dict[str, Any]:
    """All KPI numbers shown in the terminal summary and the dashboard."""
    eq = result.equity
    start = result.initial_btc * result.start_price + result.initial_eur
    last = eq.iloc[-1]
    final = float(last["equity"])
    fees = sum(f.fee for f in result.fills)
    cycle_profit = sum(c.profit_total_eur for c in result.cycles)
    n_cycles = len(result.cycles)

    def line(column: str) -> dict[str, Any]:
        value = float(last[column])
        return {
            "final": value,
            "pnl": value - start,
            "return_pct": (value / start - 1) * 100,
            "max_drawdown_pct": float(drawdown(eq[column]).min() * 100),
            "sharpe": sharpe_ratio(eq[column]),
        }

    lines = {name: line(col) for name, col in
             [("grid", "equity"), ("rebalance", "bench_rebal"), ("idle", "bench_idle"), ("hold", "bench_hold")]}
    return {
        "start_capital": start,
        "final_equity": final,
        "pnl": final - start,
        "return_pct": (final / start - 1) * 100,
        "alpha_eur": final - lines["rebalance"]["final"],
        "alpha_pct": (final - lines["rebalance"]["final"]) / start * 100,
        "alpha_vs_idle_eur": final - lines["idle"]["final"],
        "btc_start": result.initial_btc,
        "btc_end": float(last["btc"]),
        "btc_growth_pct": (float(last["btc"]) / result.initial_btc - 1) * 100,
        "eur_start": result.initial_eur,
        "eur_end": float(last["eur"]),
        "eur_growth_pct": (float(last["eur"]) / result.initial_eur - 1) * 100,
        "n_cycles": n_cycles,
        "n_fills": len(result.fills),
        "n_buys": sum(1 for f in result.fills if f.side.value == "buy"),
        "n_sells": sum(1 for f in result.fills if f.side.value == "sell"),
        "fees_eur": fees,
        "cycle_profit_eur": cycle_profit,
        "avg_cycle_profit_eur": cycle_profit / n_cycles if n_cycles else None,
        "sharpe": lines["grid"]["sharpe"],
        "max_drawdown_pct": lines["grid"]["max_drawdown_pct"],
        "trail_up": sum(1 for e in result.events if e.kind == "trail_up"),
        "trail_down": sum(1 for e in result.events if e.kind == "trail_down"),
        "halts": sum(1 for e in result.events if e.kind == "halt" and "post_only" not in e.detail),
        "btc_weight_end": float(last["btc_weight"]) * 100,
        "btc_weight_min": float(eq["btc_weight"].min()) * 100,
        "btc_weight_max": float(eq["btc_weight"].max()) * 100,
        "start_price": result.start_price,
        "end_price": float(last["price"]),
        "price_change_pct": (float(last["price"]) / result.start_price - 1) * 100,
        "start": eq.index[0],
        "end": eq.index[-1],
        "lines": lines,
    }


def format_summary(result: RunResult, metrics: Optional[dict[str, Any]] = None) -> str:
    """Compact terminal summary of a run."""
    m = metrics or compute_metrics(result)

    def eur(v: float) -> str:
        return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    sharpe = "n.v.t." if m["sharpe"] is None else f"{m['sharpe']:.2f}"
    avg = "n.v.t." if m["avg_cycle_profit_eur"] is None else eur(m["avg_cycle_profit_eur"])
    rows = [
        ("Periode", f"{m['start']:%Y-%m-%d %H:%M} → {m['end']:%Y-%m-%d %H:%M} UTC"),
        ("BTC/EUR", f"{eur(m['start_price'])[:-3]} → {eur(m['end_price'])[:-3]} ({m['price_change_pct']:+.2f}%)"),
        ("Maker fee / grid-stap", f"{result.maker_fee * 100:.3f}% ({result.fee_source}) / {result.step_pct * 100:.3f}%"),
        ("Grid Bot", f"{eur(m['start_capital'])} → {eur(m['final_equity'])} ({m['return_pct']:+.2f}%)"),
        ("50/50 Herbalanceren", f"{eur(m['lines']['rebalance']['final'])} ({m['lines']['rebalance']['return_pct']:+.2f}%)"),
        ("50/50 Stil", f"{eur(m['lines']['idle']['final'])} ({m['lines']['idle']['return_pct']:+.2f}%)"),
        ("100% BTC", f"{eur(m['lines']['hold']['final'])} ({m['lines']['hold']['return_pct']:+.2f}%)"),
        ("Grid-Alpha vs herbalanceren", f"{eur(m['alpha_eur'])} ({m['alpha_pct']:+.2f}%-punt)"),
        ("BTC-voorraad", f"{m['btc_start']:.8f} → {m['btc_end']:.8f} ({m['btc_growth_pct']:+.2f}%)"),
        ("EUR-voorraad", f"{eur(m['eur_start'])} → {eur(m['eur_end'])} ({m['eur_growth_pct']:+.2f}%)"),
        ("Cycli / fills", f"{m['n_cycles']} / {m['n_fills']} ({m['n_buys']} buy, {m['n_sells']} sell)"),
        ("Fees / gem. winst per cyclus", f"{eur(m['fees_eur'])} / {avg}"),
        ("Sharpe / max drawdown", f"{sharpe} / {m['max_drawdown_pct']:.2f}%"),
        ("Trailing ↑/↓ / guard-stops", f"{m['trail_up']} / {m['trail_down']} / {m['halts']}"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)
