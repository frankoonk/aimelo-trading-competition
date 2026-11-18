"""Pure, deterministic grid state machine.

No I/O, no ccxt, no clocks, no randomness: the same sequence of inputs always
produces the same sequence of fills. Backtester and paper runner both drive
this one class.

Model
-----
Grid levels form a geometric lattice ``P_k = anchor * (1 + step)^k`` (rounded to
the exchange tick), indexed by an integer ``k``. The active window covers
``2N + 1`` consecutive levels ``[lo, hi]``. One level in it, the *gap*, carries
no order (it is where the last fill happened); every level below the gap wants
a buy order and every level above it a sell order.

* **Cycle flip** - a fill at level ``k`` moves the gap to ``k``. The level the
  gap just left is therefore re-armed with the opposite side: a buy filled at
  ``P_i`` produces a sell at ``P_{i+1}``, a sell filled at ``P_j`` a buy at
  ``P_{j-1}``.
* **Trailing** - when the gap reaches the edge of the window (the outermost
  order filled), the whole window slides one level with the price: the order
  furthest away on the other side is cancelled and a new order is opened
  beyond the edge, funded from the idle reserve - but only if the inventory
  guardrails allow it.
* **Sizing** - a buy at level ``k`` is worth ``order_size_eur``. The sell one
  level higher is sized so that the cycle's *net-of-fee* profit is split between
  BTC and EUR according to ``profit_split_btc``; with a step above the fee floor
  both stacks therefore grow on every completed cycle.

Fees are charged in EUR on both sides (as Bitvavo does): a buy costs
``notional * (1 + fee)``, a sell yields ``notional * (1 - fee)``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional

from config import GridConfig, MarketSpec

_EPS = 1e-9


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    """A resting maker limit order."""

    level: int
    side: Side
    price: float
    amount: float
    placed_ts: int


@dataclass
class Fill:
    """An executed grid order."""

    seq: int
    ts: int
    side: Side
    level: int
    price: float
    amount: float
    cost: float  # EUR notional (amount * price), before fee
    fee: float  # EUR
    btc_after: float
    eur_after: float
    cycle_id: Optional[int] = None  # set when this fill completed a cycle
    cycle_pnl_eur: Optional[float] = None  # total cycle profit valued in EUR


@dataclass
class Cycle:
    """A completed buy+sell pair on two adjacent levels."""

    id: int
    opened_ts: int
    closed_ts: int
    buy_price: float
    sell_price: float
    buy_amount: float
    sell_amount: float
    profit_eur: float  # net EUR left over after both fees
    profit_btc: float  # BTC left over
    profit_total_eur: float  # profit_eur + profit_btc valued at the closing fill


@dataclass
class GridEvent:
    """Trailing moves and guardrail state changes, for the audit trail."""

    ts: int
    kind: str  # trail_up | trail_down | halt | resume
    detail: str
    price: float


class GridEngine:
    """Deterministic spot grid with trailing window and inventory guardrails.

    Feed it market data with one of:

    * :meth:`on_path`  - a continuous price move (candle replay),
    * :meth:`on_trade` - a public trade print (tick replay / live),
    * :meth:`on_quote` - a top-of-book update (tick replay / live).
    """

    def __init__(
        self,
        cfg: GridConfig,
        market: MarketSpec,
        maker_fee: float,
        start_price: float,
        start_ts: int = 0,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> None:
        if start_price <= 0:
            raise ValueError("start_price must be positive")
        if maker_fee < 0:
            raise ValueError("maker_fee cannot be negative")
        self.cfg = cfg
        self.market = market
        self.fee = float(maker_fee)
        self.step = cfg.effective_step_pct(self.fee)
        self.n = cfg.orders_per_side
        self.order_size = cfg.order_size_eur
        self.anchor = market.round_price(start_price)

        if self.anchor * self.step < 2 * market.tick_size:
            raise ValueError("grid step is smaller than two price ticks; increase grid_step_pct")
        if self.order_size < market.min_cost * 1.05:
            raise ValueError(
                f"grid order of EUR {self.order_size:.2f} is below the exchange minimum "
                f"of EUR {market.min_cost:.2f}; raise capital/active_fraction or lower orders_per_side"
            )
        # Net profit factor of one cycle; > 0 is guaranteed by the fee floor.
        self.cycle_edge = (1 + self.step) * (1 - self.fee) - (1 + self.fee)
        if self.cycle_edge <= 0:
            raise ValueError("grid step does not cover two maker fees")

        # Balances: the full portfolio (grid + reserve).
        self.btc = market.round_amount(cfg.initial_capital_eur * cfg.initial_btc_fraction / start_price)
        self.eur = cfg.initial_capital_eur - self.btc * start_price
        self.initial_btc = self.btc
        self.initial_eur = self.eur

        # Window state.
        self.lo = -self.n
        self.hi = self.n
        self.gap = 0
        self.orders: dict[int, Order] = {}

        # History.
        self.fills: list[Fill] = []
        self.cycles: list[Cycle] = []
        self.events: list[GridEvent] = []
        self.fees_paid = 0.0
        self.rejected = 0

        # Internals.
        self._price_cache: dict[int, float] = {}
        self._buy_cache: dict[int, float] = {}
        self._sell_cache: dict[int, float] = {}
        self._open_legs: dict[int, Fill] = {}  # pair (lower level) -> unmatched fill
        self._halt: dict[str, Optional[str]] = {"down": None, "up": None}
        self._top_buy = -math.inf
        self._low_sell = math.inf
        self._pending = False
        self.bid: Optional[float] = bid
        self.ask: Optional[float] = ask
        self.last_price = float(start_price)
        self.last_ts = start_ts

        self.initial_free_eur = math.inf
        self.initial_free_btc = math.inf
        self._reconcile(start_ts, bid if bid is not None else start_price, ask if ask is not None else start_price)
        #: Idle reserve right after the initial grid is placed (the "75%").
        self.initial_free_eur = self.free_eur
        self.initial_free_btc = self.free_btc

    # ------------------------------------------------------------------ levels
    def level_price(self, k: int) -> float:
        """Tick-rounded price of grid level ``k``."""
        price = self._price_cache.get(k)
        if price is None:
            price = self.market.round_price(self.anchor * (1 + self.step) ** k)
            self._price_cache[k] = price
        return price

    def buy_amount(self, k: int) -> float:
        """BTC amount of a buy order at level ``k`` (worth ``order_size`` EUR)."""
        amount = self._buy_cache.get(k)
        if amount is None:
            amount = self.market.round_amount(self.order_size / self.level_price(k))
            self._buy_cache[k] = amount
        return amount

    def sell_amount(self, k: int) -> float:
        """BTC amount of a sell order at level ``k``, paired with the buy at ``k - 1``.

        ``break_even`` is the amount whose net proceeds exactly repay the paired
        buy including both fees. Selling that keeps the whole profit in BTC;
        selling the full bought amount keeps it all in EUR. We blend the two.
        """
        amount = self._sell_cache.get(k)
        if amount is None:
            bought = self.buy_amount(k - 1)
            p_buy, p_sell = self.level_price(k - 1), self.level_price(k)
            break_even = bought * p_buy * (1 + self.fee) / (p_sell * (1 - self.fee))
            w = self.cfg.profit_split_btc
            amount = min(bought, self.market.round_amount((1 - w) * bought + w * break_even))
            self._sell_cache[k] = amount
        return amount

    # ---------------------------------------------------------------- balances
    @property
    def locked_eur(self) -> float:
        """EUR committed to resting buy orders (including their fees)."""
        return sum(o.amount * o.price for o in self.orders.values() if o.side is Side.BUY) * (1 + self.fee)

    @property
    def locked_btc(self) -> float:
        """BTC committed to resting sell orders."""
        return sum(o.amount for o in self.orders.values() if o.side is Side.SELL)

    @property
    def free_eur(self) -> float:
        return self.eur - self.locked_eur

    @property
    def free_btc(self) -> float:
        return self.btc - self.locked_btc

    def equity(self, price: float) -> float:
        return self.btc * price + self.eur

    def btc_weight(self, price: float) -> float:
        total = self.equity(price)
        return self.btc * price / total if total > 0 else 0.0

    def reserve_usage(self) -> tuple[float, float]:
        """Share of the initial idle reserve consumed, as ``(eur, btc)`` in [0, 1]."""
        eur = 1 - self.free_eur / self.initial_free_eur if self.initial_free_eur > 0 else 0.0
        btc = 1 - self.free_btc / self.initial_free_btc if self.initial_free_btc > 0 else 0.0
        return max(0.0, eur), max(0.0, btc)

    @property
    def halted(self) -> dict[str, Optional[str]]:
        """Why placing/trailing on a side is currently blocked (``None`` = not blocked)."""
        return dict(self._halt)

    @property
    def window(self) -> tuple[float, float]:
        """Lower and upper price bound of the active grid window."""
        return self.level_price(self.lo), self.level_price(self.hi)

    def open_orders(self) -> list[Order]:
        return sorted(self.orders.values(), key=lambda o: o.price)

    # ------------------------------------------------------------ market input
    def on_path(self, ts: int, price: float) -> list[Fill]:
        """Process a *continuous* price move from the last price to ``price``.

        Used for candle replay. Every level crossed on the way is handled in
        price order, and orders created during the move (flips, trailing) are
        live for the remainder of the move, exactly as on a continuous path.
        """
        fills: list[Fill] = []
        touch = self.cfg.fill_on_touch
        if price < self.last_price:
            while price < self._top_buy or (touch and price == self._top_buy):
                order = max((o for o in self.orders.values() if o.side is Side.BUY), key=lambda o: o.price)
                fills.append(self._fill(order, ts))
                self._reconcile(ts, order.price, order.price)
        elif price > self.last_price:
            while price > self._low_sell or (touch and price == self._low_sell):
                order = min((o for o in self.orders.values() if o.side is Side.SELL), key=lambda o: o.price)
                fills.append(self._fill(order, ts))
                self._reconcile(ts, order.price, order.price)
        if self._pending:
            self._reconcile(ts, price, price)
        self.last_price, self.last_ts = price, ts
        return fills

    def on_trade(self, ts: int, price: float) -> list[Fill]:
        """Process one public trade print.

        Resting orders the trade printed through are filled in price order at
        their own limit price. Orders created in response are *not* eligible
        for this same print (they were not resting when it happened).
        """
        touch = self.cfg.fill_on_touch
        hit: list[Order] = []
        if price < self._top_buy or (touch and price == self._top_buy):
            hit = sorted(
                (o for o in self.orders.values() if o.side is Side.BUY and (o.price > price or (touch and o.price == price))),
                key=lambda o: -o.price,
            )
        elif price > self._low_sell or (touch and price == self._low_sell):
            hit = sorted(
                (o for o in self.orders.values() if o.side is Side.SELL and (o.price < price or (touch and o.price == price))),
                key=lambda o: o.price,
            )
        fills = [self._fill(o, ts) for o in hit]
        if fills or self._pending:
            # The print itself is evidence of where the market is: never place
            # a buy at/above it or a sell at/below it.
            bid = price if self.bid is None else max(self.bid, price)
            ask = price if self.ask is None else min(self.ask, price)
            self._reconcile(ts, bid, ask)
        self.last_price, self.last_ts = price, ts
        return fills

    def on_quote(self, ts: int, bid: float, ask: float) -> list[Fill]:
        """Process a top-of-book update.

        If the best ask sits at or below one of our resting buys, that buy must
        have been matched (and vice versa for sells), so it is filled.
        """
        self.bid, self.ask = bid, ask
        hit: list[Order] = []
        if ask <= self._top_buy:
            hit = sorted((o for o in self.orders.values() if o.side is Side.BUY and o.price >= ask), key=lambda o: -o.price)
        elif bid >= self._low_sell:
            hit = sorted((o for o in self.orders.values() if o.side is Side.SELL and o.price <= bid), key=lambda o: o.price)
        fills = [self._fill(o, ts) for o in hit]
        if fills or self._pending:
            self._reconcile(ts, bid, ask)
        self.last_price, self.last_ts = (bid + ask) / 2, ts
        return fills

    # --------------------------------------------------------------- internals
    def _fill(self, order: Order, ts: int) -> Fill:
        """Execute a resting order, update balances and cycle bookkeeping."""
        del self.orders[order.level]
        notional = order.amount * order.price
        fee = notional * self.fee
        if order.side is Side.BUY:
            self.eur -= notional + fee
            self.btc += order.amount
        else:
            self.btc -= order.amount
            self.eur += notional - fee
        if self.btc < -_EPS or self.eur < -1e-6:
            raise AssertionError(f"negative balance after fill: btc={self.btc}, eur={self.eur}")
        self.fees_paid += fee
        self.gap = order.level

        fill = Fill(
            seq=len(self.fills) + 1,
            ts=ts,
            side=order.side,
            level=order.level,
            price=order.price,
            amount=order.amount,
            cost=notional,
            fee=fee,
            btc_after=self.btc,
            eur_after=self.eur,
        )
        # A cycle is a buy at level k matched with a sell at level k + 1.
        pair = order.level if order.side is Side.BUY else order.level - 1
        other = self._open_legs.get(pair)
        if other is not None and other.side is not order.side:
            buy, sell = (fill, other) if order.side is Side.BUY else (other, fill)
            profit_eur = sell.cost - sell.fee - buy.cost - buy.fee
            profit_btc = buy.amount - sell.amount
            cycle = Cycle(
                id=len(self.cycles) + 1,
                opened_ts=other.ts,
                closed_ts=ts,
                buy_price=buy.price,
                sell_price=sell.price,
                buy_amount=buy.amount,
                sell_amount=sell.amount,
                profit_eur=profit_eur,
                profit_btc=profit_btc,
                profit_total_eur=profit_eur + profit_btc * order.price,
            )
            self.cycles.append(cycle)
            fill.cycle_id = cycle.id
            fill.cycle_pnl_eur = cycle.profit_total_eur
            del self._open_legs[pair]
        else:
            self._open_legs[pair] = fill
        self.fills.append(fill)
        return fill

    def _valid_size(self, price: float, amount: float) -> bool:
        return amount > 0 and amount * price >= self.market.min_cost and amount >= self.market.min_amount

    def _try_trail_down(self, ts: int, ask: float) -> Optional[str]:
        """Slide the window one level down. Returns the blocking reason, if any."""
        k = self.lo - 1
        price, amount = self.level_price(k), self.buy_amount(k)
        cost = amount * price * (1 + self.fee)
        if not self._valid_size(price, amount):
            return "min_order"
        value = (self.btc + amount) * price
        if value / (value + self.eur - cost) > self.cfg.max_btc_weight + _EPS:
            return "drift"
        if self.free_eur - cost < (1 - self.cfg.max_reserve_usage) * self.initial_free_eur - 1e-6:
            return "reserve"
        if not price < ask:
            return "post_only"
        self.orders.pop(self.hi, None)  # cancel the furthest sell; its BTC returns to the reserve
        self.lo -= 1
        self.hi -= 1
        self.events.append(GridEvent(ts, "trail_down", f"venster -> [{self.level_price(self.lo):.0f}, {self.level_price(self.hi):.0f}]", price))
        return None

    def _try_trail_up(self, ts: int, bid: float) -> Optional[str]:
        """Slide the window one level up. Returns the blocking reason, if any."""
        k = self.hi + 1
        price, amount = self.level_price(k), self.sell_amount(k)
        if not self._valid_size(price, amount):
            return "min_order"
        value = (self.btc - amount) * price
        if value / (value + self.eur + amount * price * (1 - self.fee)) < self.cfg.min_btc_weight - _EPS:
            return "drift"
        if self.free_btc - amount < (1 - self.cfg.max_reserve_usage) * self.initial_free_btc - _EPS:
            return "reserve"
        if not price > bid:
            return "post_only"
        self.orders.pop(self.lo, None)  # cancel the furthest buy; its EUR returns to the reserve
        self.lo += 1
        self.hi += 1
        self.events.append(GridEvent(ts, "trail_up", f"venster -> [{self.level_price(self.lo):.0f}, {self.level_price(self.hi):.0f}]", price))
        return None

    def _reconcile(self, ts: int, bid: float, ask: float) -> None:
        """Bring resting orders in line with the desired window.

        Runs after every state change. Applies, in this order: trailing, the
        cumulative inventory-drift breaker, the postOnly rule, affordability
        and the exchange minimum order size.
        """
        halt: dict[str, Optional[str]] = {"down": None, "up": None}
        pending = False

        if self.gap == self.lo:
            halt["down"] = self._try_trail_down(ts, ask)
            pending = pending or halt["down"] is not None
        elif self.gap == self.hi:
            halt["up"] = self._try_trail_up(ts, bid)
            pending = pending or halt["up"] is not None

        # Buys, nearest to the price first. The projection assumes every buy
        # above has filled, so no resting buy can push the BTC weight over the cap.
        proj_btc, proj_eur, free_eur, blocked = self.btc, self.eur, self.free_eur, False
        for k in range(self.gap - 1, self.lo - 1, -1):
            price, amount = self.level_price(k), self.buy_amount(k)
            cost = amount * price * (1 + self.fee)
            proj_btc += amount
            proj_eur -= cost
            value = proj_btc * price
            if not blocked and (proj_eur < -1e-6 or value / (value + proj_eur) > self.cfg.max_btc_weight + _EPS):
                blocked = True
                halt["down"] = "drift"
            existing = self.orders.get(k)
            if existing is not None and existing.side is not Side.BUY:
                del self.orders[k]
                existing = None
            if blocked:
                if existing is not None:
                    del self.orders[k]
                pending = True
                continue
            if existing is not None:
                continue
            if not self._valid_size(price, amount):
                self.rejected += 1
                continue
            if not price < ask or cost > free_eur + 1e-6:
                pending = True
                continue
            self.orders[k] = Order(k, Side.BUY, price, amount, ts)
            free_eur -= cost

        # Sells, nearest to the price first, mirrored logic for the lower bound.
        proj_btc, proj_eur, free_btc, blocked = self.btc, self.eur, self.free_btc, False
        for k in range(self.gap + 1, self.hi + 1):
            price, amount = self.level_price(k), self.sell_amount(k)
            proj_btc -= amount
            proj_eur += amount * price * (1 - self.fee)
            value = proj_btc * price
            if not blocked and (proj_btc < -_EPS or value / (value + proj_eur) < self.cfg.min_btc_weight - _EPS):
                blocked = True
                halt["up"] = "drift"
            existing = self.orders.get(k)
            if existing is not None and existing.side is not Side.SELL:
                del self.orders[k]
                existing = None
            if blocked:
                if existing is not None:
                    del self.orders[k]
                pending = True
                continue
            if existing is not None:
                continue
            if not self._valid_size(price, amount):
                self.rejected += 1
                continue
            if not price > bid or amount > free_btc + _EPS:
                pending = True
                continue
            self.orders[k] = Order(k, Side.SELL, price, amount, ts)
            free_btc -= amount

        for side in ("down", "up"):
            if halt[side] != self._halt[side]:
                mark = self.level_price(self.gap)
                if halt[side] is None:
                    self.events.append(GridEvent(ts, "resume", f"{side}: weer actief", mark))
                else:
                    self.events.append(GridEvent(ts, "halt", f"{side}: geblokkeerd door {halt[side]}", mark))
        self._halt = halt
        self._pending = pending
        self._top_buy = max((o.price for o in self.orders.values() if o.side is Side.BUY), default=-math.inf)
        self._low_sell = min((o.price for o in self.orders.values() if o.side is Side.SELL), default=math.inf)

    # ------------------------------------------------------------------ export
    def state_dict(self, price: Optional[float] = None) -> dict[str, Any]:
        """JSON-serialisable snapshot of the full engine state."""
        mark = self.last_price if price is None else price
        usage_eur, usage_btc = self.reserve_usage()
        return {
            "ts": self.last_ts,
            "price": mark,
            "btc": self.btc,
            "eur": self.eur,
            "equity": self.equity(mark),
            "btc_weight": self.btc_weight(mark),
            "free_btc": self.free_btc,
            "free_eur": self.free_eur,
            "reserve_usage_eur": usage_eur,
            "reserve_usage_btc": usage_btc,
            "window": {"lo": self.lo, "hi": self.hi, "gap": self.gap, "lo_price": self.window[0], "hi_price": self.window[1]},
            "halted": self.halted,
            "maker_fee": self.fee,
            "step_pct": self.step,
            "anchor": self.anchor,
            "fees_paid": self.fees_paid,
            "n_fills": len(self.fills),
            "n_cycles": len(self.cycles),
            "open_orders": [{**asdict(o), "side": o.side.value} for o in self.open_orders()],
        }
