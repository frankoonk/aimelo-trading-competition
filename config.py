"""Central configuration for the Bitvavo BTC/EUR spot grid bot.

Everything that influences the behaviour of the bot lives in :class:`GridConfig`.
Exchange metadata (tick size, lot size, minimum order, fees) lives in
:class:`MarketSpec` so the grid engine never needs to import ``ccxt``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class MarketSpec:
    """Exchange trading rules for one market.

    ``price_fn`` / ``amount_fn`` can be injected with ccxt's
    ``price_to_precision`` / ``amount_to_precision``. Without them a pure
    implementation with identical semantics is used (round-half-up to the tick
    for prices, truncate to the lot step for amounts), which keeps backtests
    and unit tests fully offline.
    """

    symbol: str = "BTC/EUR"
    tick_size: float = 1.0
    amount_step: float = 1e-8
    min_cost: float = 5.0
    min_amount: float = 0.0
    maker_fee: float = 0.0015
    taker_fee: float = 0.0025
    price_fn: Optional[Callable[[float], float]] = field(default=None, compare=False, repr=False)
    amount_fn: Optional[Callable[[float], float]] = field(default=None, compare=False, repr=False)

    def round_price(self, price: float) -> float:
        """Round a price to the exchange tick size."""
        if self.price_fn is not None:
            return float(self.price_fn(price))
        tick = Decimal(str(self.tick_size))
        return float((Decimal(repr(float(price))) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick)

    def round_amount(self, amount: float) -> float:
        """Truncate a base amount to the exchange lot step."""
        if self.amount_fn is not None:
            return float(self.amount_fn(amount))
        step = Decimal(str(self.amount_step))
        return float((Decimal(repr(float(amount))) / step).quantize(Decimal(1), rounding=ROUND_DOWN) * step)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_size": self.tick_size,
            "amount_step": self.amount_step,
            "min_cost": self.min_cost,
            "min_amount": self.min_amount,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketSpec":
        return cls(**{k: data[k] for k in cls().to_dict() if k in data})


@dataclass
class GridConfig:
    """All tunable settings of the bot."""

    # --- market & capital -------------------------------------------------
    symbol: str = "BTC/EUR"
    initial_capital_eur: float = 10_000.0
    #: Share of the starting capital held as BTC (rest is EUR).
    initial_btc_fraction: float = 0.5
    #: Share of the capital that rests in the order book (25% grid / 75% reserve).
    active_fraction: float = 0.25

    # --- grid geometry ----------------------------------------------------
    #: Number of buy orders below and sell orders above the price.
    orders_per_side: int = 10
    #: Requested geometric distance between two grid levels (0.005 = 0.5%).
    grid_step_pct: float = 0.005
    #: Share of each cycle's net profit that is kept in BTC (rest in EUR).
    #: 0.5 lets both the satoshi stack and the EUR stack grow per cycle.
    profit_split_btc: float = 0.5

    # --- fees & fee floor -------------------------------------------------
    #: Force a maker fee (e.g. 0.0 for a 0.00% maker tier). ``None`` = read from the API.
    maker_fee_override: Optional[float] = None
    #: Used only when the API gives no maker fee at all.
    maker_fee_fallback: float = 0.0015
    safety_margin_pct: float = 0.0005
    min_step_floor_pct: float = 0.0015

    # --- inventory drift guardrails ---------------------------------------
    min_btc_weight: float = 0.25
    max_btc_weight: float = 0.75
    #: Maximum share of the initial idle reserve (per currency) trailing may consume.
    max_reserve_usage: float = 0.80

    # --- fill model -------------------------------------------------------
    #: False (conservative): a trade must print *through* our limit price.
    #: True: a trade *at* our limit price also fills (ignores queue position).
    fill_on_touch: bool = False

    # --- benchmarks -------------------------------------------------------
    rebalance_interval_hours: float = 24.0
    #: Fee charged on the rebalancing benchmark's trades. ``None`` = same maker fee as the grid.
    benchmark_fee_pct: Optional[float] = None

    # --- live stream hygiene ---------------------------------------------
    stale_after_s: float = 10.0
    reconnect_backoff_min_s: float = 1.0
    reconnect_backoff_max_s: float = 60.0
    snapshot_interval_s: float = 5.0
    status_interval_s: float = 30.0
    state_save_interval_s: float = 30.0

    # --- storage ----------------------------------------------------------
    data_dir: str = "data"
    reports_dir: str = "reports"

    def __post_init__(self) -> None:
        if self.initial_capital_eur <= 0:
            raise ValueError("initial_capital_eur must be positive")
        if not 0 < self.active_fraction < 1:
            raise ValueError("active_fraction must be in (0, 1)")
        if not 0 < self.initial_btc_fraction < 1:
            raise ValueError("initial_btc_fraction must be in (0, 1)")
        if self.orders_per_side < 1:
            raise ValueError("orders_per_side must be >= 1")
        if not 0 <= self.profit_split_btc <= 1:
            raise ValueError("profit_split_btc must be in [0, 1]")
        if not 0 <= self.min_btc_weight < self.max_btc_weight <= 1:
            raise ValueError("need 0 <= min_btc_weight < max_btc_weight <= 1")
        if not 0 < self.max_reserve_usage <= 1:
            raise ValueError("max_reserve_usage must be in (0, 1]")

    @property
    def order_size_eur(self) -> float:
        """EUR value of a single grid order (default 10000 * 0.25 / 20 = 125)."""
        return self.initial_capital_eur * self.active_fraction / (2 * self.orders_per_side)

    def min_step_pct(self, maker_fee: float) -> float:
        """Fee floor: the smallest grid step that is still profitable after fees."""
        return max(2.0 * maker_fee + self.safety_margin_pct, self.min_step_floor_pct)

    def effective_step_pct(self, maker_fee: float) -> float:
        """The grid step actually used: the requested step, lifted to the fee floor."""
        return max(self.grid_step_pct, self.min_step_pct(maker_fee))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
