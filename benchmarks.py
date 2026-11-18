"""Reference portfolios the grid bot is measured against.

All three start from exactly the same capital at exactly the same price as the
grid bot, so differences are attributable to behaviour, not to the start.

* **A - 50/50 idle**: the starting BTC and EUR are simply left alone.
* **B - 50/50 periodic rebalance**: the same split, brought back to target every
  ``rebalance_interval_hours``. This already harvests volatility without any
  grid, so only what the bot earns *above* B is real grid alpha.
* **C - 100% BTC buy & hold**: shown for context (cash drag in a straight rally).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BenchmarkValues:
    idle: float
    rebalance: float
    hold: float


class BenchmarkTracker:
    """Incrementally tracks the three benchmarks along a price series.

    The same tracker is used by the backtester and the live paper runner, so
    both report benchmarks computed by identical code.
    """

    def __init__(
        self,
        start_btc: float,
        start_eur: float,
        start_price: float,
        start_ts_ms: int,
        rebalance_interval_hours: float = 24.0,
        fee_pct: float = 0.0,
    ) -> None:
        capital = start_btc * start_price + start_eur
        self.target_weight = start_btc * start_price / capital
        self.fee_pct = fee_pct
        self.interval_ms = int(rebalance_interval_hours * 3_600_000)
        self.next_rebalance_ms = start_ts_ms + self.interval_ms

        self.idle_btc, self.idle_eur = start_btc, start_eur
        self.rebal_btc, self.rebal_eur = start_btc, start_eur
        self.hold_btc = capital / start_price

        self.rebalances = 0
        self.rebalance_fees = 0.0
        self.last = BenchmarkValues(capital, capital, capital)

    def update(self, ts_ms: int, price: float) -> BenchmarkValues:
        """Advance to ``ts_ms`` at ``price`` and return the three portfolio values."""
        if self.interval_ms > 0 and ts_ms >= self.next_rebalance_ms:
            while ts_ms >= self.next_rebalance_ms:
                self.next_rebalance_ms += self.interval_ms
            self._rebalance(price)
        self.last = BenchmarkValues(
            idle=self.idle_btc * price + self.idle_eur,
            rebalance=self.rebal_btc * price + self.rebal_eur,
            hold=self.hold_btc * price,
        )
        return self.last

    def _rebalance(self, price: float) -> None:
        """Trade back to the target weight, paying ``fee_pct`` on the traded notional."""
        total = self.rebal_btc * price + self.rebal_eur
        excess_eur = self.rebal_btc * price - total * self.target_weight  # > 0: sell BTC
        fee = abs(excess_eur) * self.fee_pct
        self.rebal_btc -= excess_eur / price
        self.rebal_eur += excess_eur - fee
        self.rebalances += 1
        self.rebalance_fees += fee
