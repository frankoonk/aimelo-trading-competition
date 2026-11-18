"""Tests for the deterministic grid engine and its guardrails."""
from __future__ import annotations

import math
import random

import pytest

from config import GridConfig, MarketSpec
from grid_engine import GridEngine, Side

START = 60_000.0
FEE = 0.0015


def make(fee: float = FEE, price: float = START, market: MarketSpec | None = None, **overrides) -> GridEngine:
    return GridEngine(GridConfig(**overrides), market or MarketSpec(), fee, price)


def buys(engine: GridEngine):
    return [o for o in engine.open_orders() if o.side is Side.BUY]


def sells(engine: GridEngine):
    return [o for o in engine.open_orders() if o.side is Side.SELL]


def assert_invariants(engine: GridEngine) -> None:
    """Properties that must hold after every single event."""
    assert engine.btc >= -1e-12 and engine.eur >= -1e-6
    assert engine.free_btc >= -1e-9 and engine.free_eur >= -1e-6
    assert len(engine.orders) <= 2 * engine.n
    b, s = buys(engine), sells(engine)
    if b and s:
        assert max(o.price for o in b) < min(o.price for o in s)
    for o in engine.orders.values():
        assert engine.lo <= o.level <= engine.hi and o.level != engine.gap
        assert o.amount * o.price >= engine.market.min_cost
    assert [f.seq for f in engine.fills] == list(range(1, len(engine.fills) + 1))


# --------------------------------------------------------------------- fee floor
@pytest.mark.parametrize(
    "fee, expected",
    [(0.0, 0.0015), (0.0005, 0.0015), (0.0015, 0.0035), (0.0025, 0.0055)],
)
def test_fee_floor_formula(fee, expected):
    assert GridConfig().min_step_pct(fee) == pytest.approx(expected)


def test_step_is_lifted_to_fee_floor():
    engine = make(fee=0.0015, grid_step_pct=0.0001)
    assert engine.step == pytest.approx(0.0035)
    assert engine.cycle_edge > 0


def test_zero_maker_fee_still_respects_15bps_floor():
    engine = make(fee=0.0, grid_step_pct=0.0001)
    assert engine.step == pytest.approx(0.0015)


def test_requested_step_above_floor_is_kept():
    assert make(grid_step_pct=0.01).step == pytest.approx(0.01)


# ----------------------------------------------------------------- initial setup
def test_initial_capital_split_and_grid_layout():
    engine = make()
    assert engine.btc * START == pytest.approx(5_000, abs=0.01)
    assert engine.eur == pytest.approx(5_000, abs=0.01)
    assert len(buys(engine)) == 10 and len(sells(engine)) == 10
    assert all(o.price < START for o in buys(engine))
    assert all(o.price > START for o in sells(engine))
    for o in buys(engine):
        assert o.amount * o.price == pytest.approx(125, abs=0.01)
    # 25% rests in the book, 75% stays idle, each half BTC / half EUR.
    assert engine.locked_eur == pytest.approx(1_250, rel=0.01)
    assert engine.locked_btc * START == pytest.approx(1_250, rel=0.06)
    assert engine.free_eur == pytest.approx(3_750, rel=0.01)
    assert engine.free_btc * START == pytest.approx(3_750, rel=0.03)
    assert_invariants(engine)


def test_levels_are_geometric_and_respect_tick_and_lot():
    market = MarketSpec(tick_size=1.0, amount_step=1e-8)
    engine = make(market=market)
    for o in engine.orders.values():
        assert o.price == round(o.price)
        assert abs(o.amount * 1e8 - round(o.amount * 1e8)) < 1e-6
    prices = [o.price for o in engine.open_orders()]
    assert prices == sorted(set(prices))
    assert engine.level_price(1) / engine.level_price(0) == pytest.approx(1 + engine.step, rel=1e-4)


def test_pure_rounding_matches_ccxt_semantics():
    market = MarketSpec(tick_size=1.0, amount_step=1e-8)
    assert market.round_price(78_123.456) == 78_123.0
    assert market.round_price(101_234.56) == 101_235.0
    assert market.round_amount(0.001234567891) == 0.00123456
    assert MarketSpec(tick_size=0.01).round_price(1.005) == 1.01


def test_injected_precision_functions_are_used():
    market = MarketSpec(price_fn=lambda p: math.floor(p / 10) * 10, amount_fn=lambda a: round(a, 4))
    engine = make(market=market)
    assert all(o.price % 10 == 0 for o in engine.orders.values())
    assert all(round(o.amount, 4) == o.amount for o in engine.orders.values())


def test_order_below_exchange_minimum_is_refused():
    with pytest.raises(ValueError, match="minimum"):
        make(initial_capital_eur=300)  # 300 * 25% / 20 = EUR 3.75 per order


# -------------------------------------------------------------------- cycle flip
def test_buy_fill_places_sell_one_level_higher():
    engine = make()
    fills = engine.on_path(1, engine.level_price(-1) - 1)
    assert [(f.side, f.level) for f in fills] == [(Side.BUY, -1)]
    assert engine.orders[0].side is Side.SELL
    assert engine.orders[0].price == engine.level_price(0)
    assert -1 not in engine.orders
    assert len(buys(engine)) == 9 and len(sells(engine)) == 11
    assert_invariants(engine)


def test_sell_fill_places_buy_one_level_lower():
    engine = make()
    fills = engine.on_path(1, engine.level_price(1) + 1)
    assert [(f.side, f.level) for f in fills] == [(Side.SELL, 1)]
    assert engine.orders[0].side is Side.BUY
    assert len(buys(engine)) == 11 and len(sells(engine)) == 9
    assert_invariants(engine)


@pytest.mark.parametrize("fee", [0.0, 0.0015, 0.0025])
def test_completed_cycle_grows_both_btc_and_eur(fee):
    engine = make(fee=fee)
    btc0, eur0 = engine.btc, engine.eur
    engine.on_path(1, engine.level_price(-1) - 1)  # buy at -1
    engine.on_path(2, engine.level_price(0) + 1)  # sell at 0
    assert len(engine.cycles) == 1
    cycle = engine.cycles[0]
    assert engine.btc > btc0 and engine.eur > eur0
    assert cycle.profit_btc > 0 and cycle.profit_eur > 0
    assert cycle.profit_eur == pytest.approx(engine.eur - eur0)
    assert cycle.profit_btc == pytest.approx(engine.btc - btc0)
    assert engine.fills[-1].cycle_pnl_eur == pytest.approx(cycle.profit_total_eur)
    # Net profit matches the theoretical edge of one cycle.
    assert cycle.profit_total_eur == pytest.approx(125 * engine.cycle_edge, rel=0.02)


def test_cycle_opened_by_a_sell_is_closed_by_the_buy_back():
    engine = make()
    btc0, eur0 = engine.btc, engine.eur
    engine.on_path(1, engine.level_price(1) + 1)
    engine.on_path(2, engine.level_price(0) - 1)
    assert len(engine.cycles) == 1
    assert engine.btc > btc0 and engine.eur > eur0


def test_profit_split_extremes():
    all_eur = make(profit_split_btc=0.0)
    all_eur.on_path(1, all_eur.level_price(-1) - 1)
    all_eur.on_path(2, all_eur.level_price(0) + 1)
    assert all_eur.cycles[0].profit_btc == pytest.approx(0, abs=1e-8)
    assert all_eur.cycles[0].profit_eur > 0

    all_btc = make(profit_split_btc=1.0)
    all_btc.on_path(1, all_btc.level_price(-1) - 1)
    all_btc.on_path(2, all_btc.level_price(0) + 1)
    assert all_btc.cycles[0].profit_btc > 0
    assert all_btc.cycles[0].profit_eur == pytest.approx(0, abs=0.01)


def test_fees_are_charged_in_eur_on_both_sides():
    engine = make()
    eur0 = engine.eur
    fill = engine.on_path(1, engine.level_price(-1) - 1)[0]
    assert fill.fee == pytest.approx(fill.cost * FEE)
    assert engine.eur == pytest.approx(eur0 - fill.cost - fill.fee)
    assert engine.fees_paid == pytest.approx(fill.fee)


# ---------------------------------------------------------------- multi-level gap
def test_gap_down_through_several_levels_fills_in_price_order():
    engine = make()
    fills = engine.on_path(1, engine.level_price(-4) - 1)
    assert [f.level for f in fills] == [-1, -2, -3, -4]
    assert all(f.side is Side.BUY for f in fills)
    assert len({f.seq for f in fills}) == 4
    # Each filled buy re-armed the level above it with a sell.
    assert all(engine.orders[k].side is Side.SELL for k in (-3, -2, -1, 0))
    assert engine.gap == -4
    assert_invariants(engine)


def test_gap_up_through_several_levels_fills_in_price_order():
    engine = make()
    fills = engine.on_path(1, engine.level_price(3) + 1)
    assert [f.level for f in fills] == [1, 2, 3]
    assert all(engine.orders[k].side is Side.BUY for k in (0, 1, 2))
    assert_invariants(engine)


def test_trade_print_gap_does_not_fill_orders_created_by_that_print():
    engine = make()
    lowest = engine.level_price(-10)
    fills = engine.on_trade(1, lowest - 500)  # one print through the whole buy side
    assert [f.level for f in fills] == list(range(-1, -11, -1))
    # The trailing buy would sit above the market, so postOnly keeps it out.
    assert not buys(engine)
    assert engine.halted["down"] == "post_only"
    assert (engine.lo, engine.hi) == (-10, 10)
    # The same print again cannot fill anything twice.
    assert engine.on_trade(2, lowest - 500) == []
    assert_invariants(engine)


def test_whipsaw_within_one_candle_path():
    engine = make()
    engine.on_path(1, engine.level_price(-3) - 1)
    fills = engine.on_path(1, engine.level_price(2) + 1)
    assert [(f.side.value, f.level) for f in fills] == [("sell", -2), ("sell", -1), ("sell", 0), ("sell", 1), ("sell", 2)]
    assert len(engine.cycles) == 3
    assert_invariants(engine)


# ---------------------------------------------------------------------- trailing
def test_window_trails_down_and_cancels_furthest_sell():
    engine = make()
    top = engine.level_price(10)
    free_eur0, free_btc0 = engine.free_eur, engine.free_btc
    engine.on_path(1, engine.level_price(-10) - 1)  # outermost buy filled
    assert (engine.lo, engine.hi) == (-11, 9)
    assert top not in [o.price for o in engine.orders.values()]
    assert len(buys(engine)) == 1 and len(sells(engine)) == 19
    assert buys(engine)[0].level == -11
    assert engine.events[-1].kind == "trail_down"
    # EUR came out of the reserve, BTC went back into it.
    assert engine.free_eur < free_eur0 and engine.free_btc > free_btc0
    assert_invariants(engine)


def test_window_trails_up_and_cancels_furthest_buy():
    engine = make()
    free_eur0, free_btc0 = engine.free_eur, engine.free_btc
    engine.on_path(1, engine.level_price(10) + 1)
    assert (engine.lo, engine.hi) == (-9, 11)
    assert len(sells(engine)) == 1 and len(buys(engine)) == 19
    assert sells(engine)[0].level == 11
    assert engine.free_btc < free_btc0 and engine.free_eur > free_eur0
    assert_invariants(engine)


def test_window_keeps_trailing_step_by_step_within_limits():
    engine = make(min_btc_weight=0.0, max_btc_weight=1.0, max_reserve_usage=1.0)
    engine.on_path(1, engine.level_price(-14) - 1)
    assert (engine.lo, engine.hi) == (-15, 5)
    assert [f.level for f in engine.fills] == list(range(-1, -15, -1))
    assert len(engine.orders) == 20
    assert_invariants(engine)


def test_trailing_resumes_when_price_returns_to_window_after_gap():
    engine = make()
    engine.on_trade(1, engine.level_price(-10) - 500)
    assert engine.halted["down"] == "post_only"
    engine.on_quote(2, engine.level_price(-10) - 2, engine.level_price(-10) - 1)
    assert (engine.lo, engine.hi) == (-11, 9)
    assert buys(engine)[0].level == -11
    assert engine.halted["down"] is None


# -------------------------------------------------------------- inventory guards
def test_drift_breaker_caps_btc_weight_during_crash():
    engine = make(max_reserve_usage=1.0)
    price = START
    for step in range(1, 400):
        price *= 0.997
        for fill in engine.on_path(step, price):
            total = fill.btc_after * fill.price + fill.eur_after
            assert fill.btc_after * fill.price / total <= 0.75 + 1e-9
        assert_invariants(engine)
    assert engine.halted["down"] == "drift"
    assert any(e.kind == "halt" and "drift" in e.detail for e in engine.events)
    assert engine.eur > 0.2 * engine.initial_eur  # the EUR reserve was not bought empty
    assert not buys(engine)
    n_fills = len(engine.fills)
    engine.on_path(1000, price * 0.5)  # falling further changes nothing
    assert len(engine.fills) == n_fills


def test_drift_breaker_caps_btc_weight_from_below_during_rally():
    engine = make(max_reserve_usage=1.0)
    price = START
    for step in range(1, 600):
        price *= 1.003
        for fill in engine.on_path(step, price):
            total = fill.btc_after * fill.price + fill.eur_after
            assert fill.btc_after * fill.price / total >= 0.25 - 1e-9
        assert_invariants(engine)
    assert engine.halted["up"] == "drift"
    assert engine.btc > 0.2 * engine.initial_btc
    assert not sells(engine)


def test_reserve_usage_cap_stops_trailing_down():
    engine = make(min_btc_weight=0.0, max_btc_weight=1.0, max_reserve_usage=0.5)
    price = START
    for step in range(1, 400):
        price *= 0.997
        engine.on_path(step, price)
        assert engine.reserve_usage()[0] <= 0.5 + 1e-6
        assert_invariants(engine)
    assert engine.halted["down"] == "reserve"
    assert engine.free_eur >= 0.5 * engine.initial_free_eur - 1e-6


def test_reserve_usage_cap_stops_trailing_up():
    engine = make(min_btc_weight=0.0, max_btc_weight=1.0, max_reserve_usage=0.5)
    price = START
    for step in range(1, 400):
        price *= 1.003
        engine.on_path(step, price)
        assert engine.reserve_usage()[1] <= 0.5 + 1e-6
    assert engine.halted["up"] == "reserve"


def test_grid_recovers_after_halt_when_price_returns():
    engine = make(max_reserve_usage=1.0)
    price = START
    for step in range(1, 400):
        price *= 0.997
        engine.on_path(step, price)
    assert engine.halted["down"] == "drift"
    fills_before = len(engine.fills)
    for step in range(400, 900):
        price *= 1.003
        engine.on_path(step, price)
        assert_invariants(engine)
    assert len(engine.fills) > fills_before
    assert any(e.kind == "resume" for e in engine.events)


# ---------------------------------------------------------------------- postOnly
def test_no_buy_at_or_above_best_ask_and_no_sell_at_or_below_best_bid():
    engine = make()
    engine.on_quote(1, START - 1, START)
    sell_price = engine.level_price(1)
    # Book jumps up through the first sell: it fills and its level is re-armed
    # with a buy only once that buy would rest below the best ask.
    engine.on_quote(2, sell_price + 5, sell_price + 6)
    assert engine.fills[-1].level == 1
    for o in buys(engine):
        assert o.price < sell_price + 6
    for o in sells(engine):
        assert o.price > sell_price + 5
    assert_invariants(engine)


def test_flip_order_that_would_cross_is_deferred_until_valid():
    engine = make()
    buy_price = engine.level_price(-1)
    sell_back = engine.level_price(0)
    engine.on_quote(1, buy_price - 2, buy_price - 1)  # ask through our buy -> filled
    assert engine.fills[-1].side is Side.BUY
    assert engine.orders[0].side is Side.SELL  # sell at level 0 is above the bid: fine

    # Stale book: the stored bid is still above the flip level when the print arrives.
    engine2 = make()
    engine2.on_quote(1, sell_back + 100, sell_back + 101)
    assert engine2.fills == []
    engine2.on_trade(2, buy_price - 1)  # buy at -1 fills on the print
    assert engine2.fills[-1].side is Side.BUY
    assert 0 not in engine2.orders  # a sell at/below the known bid would be a taker: not placed
    engine2.on_quote(3, sell_back - 5, sell_back - 4)
    assert engine2.orders[0].side is Side.SELL  # placed as soon as it is passive again


def test_quote_at_our_price_fills_but_trade_at_our_price_needs_touch_flag():
    price = make().level_price(-1)
    strict = make()
    assert strict.on_trade(1, price) == []
    assert len(strict.on_trade(2, price - 1)) == 1

    touch = make(fill_on_touch=True)
    assert len(touch.on_trade(1, price)) == 1

    book = make()
    assert len(book.on_quote(1, price - 1, price)) == 1  # best ask at our bid => we were matched


# ------------------------------------------------------- determinism & accounting
def random_walk(seed: int, n: int = 3_000):
    rng = random.Random(seed)
    price = START
    for i in range(n):
        price *= 1 + rng.gauss(0, 0.004)
        yield i + 1, price


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_walk_invariants_and_accounting(seed):
    engine = make()
    for ts, price in random_walk(seed):
        engine.on_path(ts, price)
        assert_invariants(engine)
    btc = engine.initial_btc + sum(f.amount if f.side is Side.BUY else -f.amount for f in engine.fills)
    eur = engine.initial_eur + sum(
        -(f.cost + f.fee) if f.side is Side.BUY else f.cost - f.fee for f in engine.fills
    )
    assert engine.btc == pytest.approx(btc, abs=1e-9)
    assert engine.eur == pytest.approx(eur, abs=1e-6)
    assert engine.fees_paid == pytest.approx(sum(f.fee for f in engine.fills))
    assert len(engine.cycles) > 0
    assert all(c.profit_total_eur > 0 for c in engine.cycles)
    assert len({(f.ts, f.level, f.side) for f in engine.fills}) == len(engine.fills)


def test_engine_is_deterministic():
    def run():
        engine = make()
        for ts, price in random_walk(99):
            engine.on_path(ts, price)
        return [(f.seq, f.ts, f.side, f.level, f.price, f.amount) for f in engine.fills], engine.btc, engine.eur

    assert run() == run()


def test_tick_mode_random_stream_invariants():
    rng = random.Random(5)
    engine = make()
    price = START
    for ts in range(1, 5_000):
        price *= 1 + rng.gauss(0, 0.0015)
        spread = rng.choice([1, 2, 5])
        if rng.random() < 0.5:
            engine.on_quote(ts, round(price) - spread, round(price))
        else:
            engine.on_trade(ts, round(price))
        assert_invariants(engine)
        if engine.bid is not None:
            for o in engine.orders.values():
                if o.placed_ts == ts:  # postOnly holds at the moment of placement
                    assert o.price < engine.ask if o.side is Side.BUY else o.price > engine.bid
    assert engine.fills


def test_state_dict_is_json_serialisable():
    import json

    engine = make()
    engine.on_path(1, engine.level_price(-2) - 1)
    state = json.loads(json.dumps(engine.state_dict()))
    assert state["n_fills"] == 2 and len(state["open_orders"]) == 20


def test_rounding_accepts_numpy_floats():
    import numpy as np

    market = MarketSpec()
    assert market.round_price(np.float64(57085.5)) == 57086.0
    assert market.round_amount(np.float64(0.001234567891)) == 0.00123456
