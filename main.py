"""Command line interface.

    python main.py check-ccxt            # phase 0: what does ccxt support for Bitvavo?
    python main.py fetch-data --days 90  # download + cache historical candles
    python main.py backtest --days 90    # replay candles, write dashboard
    python main.py record-ws             # record the live WebSocket stream
    python main.py backtest --ticks data/recordings/<file>.sqlite
    python main.py paper                 # live paper trading (Ctrl+C = stop + dashboard)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import GridConfig


def _add_strategy_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("strategie")
    g.add_argument("--capital", type=float, help="startkapitaal in EUR (standaard 10000)")
    g.add_argument("--orders-per-side", type=int, help="aantal buy- en sell-orders per kant (standaard 10)")
    g.add_argument("--active-fraction", type=float, help="deel van het kapitaal in het orderboek (standaard 0.25)")
    g.add_argument("--step", type=float, help="grid-stap als fractie, bv. 0.005 = 0,5%% (wordt opgetild tot de fee-vloer)")
    g.add_argument("--maker-fee", type=float, help="override maker fee als fractie, bv. 0 of 0.0015")
    g.add_argument("--profit-split-btc", type=float, help="deel van de cycluswinst dat in BTC blijft (standaard 0.5)")
    g.add_argument("--min-btc-weight", type=float, help="ondergrens BTC-weging (standaard 0.25)")
    g.add_argument("--max-btc-weight", type=float, help="bovengrens BTC-weging (standaard 0.75)")
    g.add_argument("--max-reserve-usage", type=float, help="max. deel van de reserve dat trailing mag gebruiken (standaard 0.8)")
    g.add_argument("--rebalance-hours", type=float, help="interval van de herbalanceer-benchmark (standaard 24)")
    g.add_argument("--fill-on-touch", action="store_true", help="vul ook als een trade precies óp de limietprijs print")
    o = p.add_argument_group("rapport")
    o.add_argument("--open", action="store_true", help="open het dashboard direct in de browser")
    o.add_argument("--cdn", action="store_true", help="laad Plotly via CDN i.p.v. inline (kleiner bestand, vereist internet)")


def _config(args: argparse.Namespace) -> GridConfig:
    mapping = {
        "capital": "initial_capital_eur", "orders_per_side": "orders_per_side", "active_fraction": "active_fraction",
        "step": "grid_step_pct", "maker_fee": "maker_fee_override", "profit_split_btc": "profit_split_btc",
        "min_btc_weight": "min_btc_weight", "max_btc_weight": "max_btc_weight", "max_reserve_usage": "max_reserve_usage",
        "rebalance_hours": "rebalance_interval_hours", "stale_after": "stale_after_s",
    }
    values = {field: getattr(args, arg) for arg, field in mapping.items() if getattr(args, arg, None) is not None}
    if getattr(args, "fill_on_touch", False):
        values["fill_on_touch"] = True
    return GridConfig(**values)


def _parse_date(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _range(args: argparse.Namespace) -> tuple[int, int]:
    end = _parse_date(args.end) or int(time.time() * 1000)
    start = _parse_date(args.start) or end - int(args.days * 86_400_000)
    return start, end


def _finish(result, args: argparse.Namespace) -> None:
    from dashboard import write_dashboard
    from results import format_summary

    print()
    print(format_summary(result))
    path = write_dashboard(result, open_browser=args.open, inline_js=not args.cdn)
    print(f"\nDashboard: {path.resolve()}")


# -------------------------------------------------------------------- commands
def cmd_check(args: argparse.Namespace) -> int:
    from exchange_ws import BitvavoRest, MarketStream

    cfg = GridConfig()
    rest = BitvavoRest(cfg)
    caps = rest.capabilities()
    print(f"ccxt {caps['ccxt_version']} (ccxt.pro inbegrepen)\n\nWebSocket (ccxt.pro.bitvavo):")
    for name, ok in caps["ws"].items():
        print(f"  {'✓' if ok else '✗'} {name}")
    print("REST (ccxt.bitvavo):")
    for name, ok in caps["rest"].items():
        print(f"  {'✓' if ok else '✗'} {name}")
    market = rest.load_market()
    fee, source = rest.resolve_maker_fee(market)
    print(f"\nMarkt {market.symbol}: tick € {market.tick_size:g}, stap {market.amount_step:g} BTC, "
          f"min. order € {market.min_cost:.2f}, maker {market.maker_fee * 100:.2f}% / taker {market.taker_fee * 100:.2f}%")
    print(f"Maker fee in gebruik: {fee * 100:.3f}% ({source}) → fee-vloer grid-stap {cfg.min_step_pct(fee) * 100:.3f}%")
    print(f"Precisie: price_to_precision(61234.56) = {market.round_price(61234.56):g}, "
          f"amount_to_precision(0.00123456789) = {market.round_amount(0.00123456789):.8f}")

    if not args.no_live:
        async def probe() -> MarketStream:
            stream = MarketStream(cfg)
            task = asyncio.create_task(stream.run())
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and not all(stream.counts[k] for k in ("book", "ticker")):
                await asyncio.sleep(0.25)
            await asyncio.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
            stream.stop()
            await task
            return stream

        print(f"\nLive WebSocket-test (max {args.seconds:.0f}s)…")
        stream = asyncio.run(probe())
        for key, label in (("book", "watch_order_book"), ("ticker", "watch_ticker"), ("trades", "watch_trades")):
            n = stream.counts["trade" if key == "trades" else key]
            note = "" if n or key != "trades" else " (geen trade in dit venster; kanaal is wel geabonneerd)"
            print(f"  {'✓' if n else '·'} {label}: {n} berichten{note}")
        print(f"  best bid/ask: {stream.bid} / {stream.ask}")
        ok = bool(stream.counts["book"] and stream.counts["ticker"])
    else:
        ok = True
    supported = all(caps["ws"].values()) and all(caps["rest"].values()) and ok
    print("\nConclusie: " + ("ccxt/ccxt.pro dekt alles; geen eigen websockets-adapter nodig." if supported
                             else "niet alles werkt via ccxt; zie de regels hierboven."))
    return 0 if supported else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    from exchange_ws import BitvavoRest

    rest = BitvavoRest(GridConfig())
    rest.load_market()
    start, end = _range(args)
    df = rest.fetch_ohlcv_cached(args.timeframe, start, end, progress=print)
    if df.empty:
        print("Geen candles ontvangen.")
        return 1
    first, last = (datetime.fromtimestamp(t / 1000, timezone.utc) for t in (df["ts"].iloc[0], df["ts"].iloc[-1]))
    print(f"{len(df)} candles ({args.timeframe}) van {first:%Y-%m-%d %H:%M} t/m {last:%Y-%m-%d %H:%M} UTC in {rest.data_dir}/")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from backtester import load_recording, run_candle_backtest, run_tick_backtest
    from exchange_ws import BitvavoRest

    cfg = _config(args)
    rest = BitvavoRest(cfg)
    market = rest.load_market(offline=args.offline)
    fee, source = rest.resolve_maker_fee(market)
    started = time.monotonic()
    if args.ticks:
        events = load_recording(args.ticks)
        print(f"Replay van {len(events)} opgenomen WebSocket-events uit {args.ticks}…")
        result = run_tick_backtest(cfg, market, fee, events, source, source=Path(args.ticks).name)
    else:
        start, end = _range(args)
        candles = rest.fetch_ohlcv_cached(args.timeframe, start, end, offline=args.offline, progress=print)
        if len(candles) < 2:
            print("Te weinig candles voor een backtest. Draai eerst `fetch-data` of vergroot de periode.")
            return 1
        print(f"Backtest op {len(candles)} {args.timeframe}-candles…")
        result = run_candle_backtest(cfg, market, fee, candles, source, args.timeframe)
    print(f"Klaar in {time.monotonic() - started:.1f}s")
    _finish(result, args)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from recorder import record

    asyncio.run(record(_config(args), fmt=args.format, out=args.out, duration_s=args.duration))
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    from paper_runner import PaperSession

    session = PaperSession(_config(args), duration_s=args.duration)
    result = asyncio.run(session.run())
    if result is None:
        print("Geen marktdata ontvangen; geen dashboard gemaakt.")
        return 1
    print(f"\nSessie gestopt. Staat opgeslagen in {session.state_path}")
    _finish(result, args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministische BTC/EUR spot grid bot voor Bitvavo (backtest + paper)")
    parser.add_argument("-v", "--verbose", action="store_true", help="toon debug-logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-ccxt", help="fase 0: controleer ccxt/ccxt.pro-ondersteuning voor Bitvavo")
    p.add_argument("--no-live", action="store_true", help="sla de live WebSocket-test over")
    p.add_argument("--seconds", type=float, default=15.0, help="maximale duur van de live test")
    p.set_defaults(func=cmd_check)

    def add_range(q: argparse.ArgumentParser) -> None:
        q.add_argument("--days", type=float, default=90.0, help="aantal dagen terug vanaf --end (standaard 90)")
        q.add_argument("--start", help="startdatum UTC, bv. 2026-01-01 (gaat vóór --days)")
        q.add_argument("--end", help="einddatum UTC (standaard nu)")
        q.add_argument("--timeframe", default="1m", help="candle-interval (standaard 1m)")

    p = sub.add_parser("fetch-data", help="download historische candles naar de lokale cache")
    add_range(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("backtest", help="backtest op candles of op een WebSocket-opname")
    add_range(p)
    p.add_argument("--ticks", help="pad naar een opname van `record-ws` (SQLite of Parquet) i.p.v. candles")
    p.add_argument("--offline", action="store_true", help="gebruik alleen de lokale cache, geen netwerk")
    _add_strategy_args(p)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("record-ws", help="neem de live WebSocket-stream op (top-of-book + trades)")
    p.add_argument("--format", choices=["sqlite", "parquet"], default="sqlite")
    p.add_argument("--out", help="uitvoerpad (standaard data/recordings/…)")
    p.add_argument("--duration", type=float, help="stop automatisch na zoveel seconden")
    p.add_argument("--stale-after", type=float, help="seconden stilte waarna de feed als verouderd geldt")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("paper", help="live paper-trading op de Bitvavo WebSocket")
    p.add_argument("--duration", type=float, help="stop automatisch na zoveel seconden (standaard: tot Ctrl+C)")
    p.add_argument("--stale-after", type=float, help="seconden stilte waarna fills pauzeren (standaard 10)")
    _add_strategy_args(p)
    p.set_defaults(func=cmd_paper)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"Fout: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
