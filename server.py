"""Local web interface for the grid bot.

Start it once (``python server.py`` or double-click ``start.command``) and do
everything else in the browser: settings, backtests, live paper trading,
recording the WebSocket stream and viewing the reports.

The server listens on 127.0.0.1 only and needs no API keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # data/ and reports/ are relative to the project, wherever the server is started from

from aiohttp import web  # noqa: E402
from plotly.offline import get_plotlyjs  # noqa: E402

from backtester import TIMEFRAME_MS, load_recording, run_candle_backtest, run_tick_backtest  # noqa: E402
from config import GridConfig, MarketSpec  # noqa: E402
from dashboard import write_dashboard  # noqa: E402
from exchange_ws import BitvavoRest, MarketStream, install_signal_handlers  # noqa: E402
from paper_runner import PaperSession  # noqa: E402
from recorder import default_path, record  # noqa: E402
from results import RunResult, compute_metrics  # noqa: E402

DEFAULT_PORT = 8642
DATA, REPORTS, RECORDINGS = Path("data"), Path("reports"), Path("data") / "recordings"
SETTINGS_FILE, INDEX_FILE = DATA / "settings.json", REPORTS / "index.json"

#: Settings editable in the GUI: name -> (type, minimum, maximum).
EDITABLE: dict[str, tuple[type, float, float]] = {
    "initial_capital_eur": (float, 100, 100_000_000),
    "orders_per_side": (int, 1, 100),
    "active_fraction": (float, 0.01, 0.99),
    "grid_step_pct": (float, 0.0001, 0.2),
    "profit_split_btc": (float, 0, 1),
    "min_btc_weight": (float, 0, 0.99),
    "max_btc_weight": (float, 0.01, 1),
    "max_reserve_usage": (float, 0.01, 1),
    "rebalance_interval_hours": (float, 1, 24 * 30),
}


def clean_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate GUI input and return only known, well-typed settings."""
    out: dict[str, Any] = {}
    for name, (kind, lo, hi) in EDITABLE.items():
        if name in raw and raw[name] is not None:
            value = kind(raw[name])
            if not lo <= value <= hi:
                raise ValueError(f"{name} moet tussen {lo:g} en {hi:g} liggen")
            out[name] = value
    fee = raw.get("maker_fee_override")
    if fee is not None and fee != "":
        fee = float(fee)
        if not 0 <= fee <= 0.01:
            raise ValueError("maker fee moet tussen 0% en 1% liggen")
        out["maker_fee_override"] = fee
    else:
        out["maker_fee_override"] = None
    out["fill_on_touch"] = bool(raw.get("fill_on_touch", False))
    GridConfig(**out)  # cross-field validation
    return out


def summarize(result: RunResult, file: str) -> dict[str, Any]:
    """The handful of numbers the GUI shows for a finished run."""
    m = compute_metrics(result)
    return {
        "file": file, "mode": result.mode, "created": datetime.now(timezone.utc).isoformat(),
        "start": m["start"].isoformat(), "end": m["end"].isoformat(),
        "start_capital": m["start_capital"], "final_equity": m["final_equity"], "pnl": m["pnl"],
        "return_pct": m["return_pct"], "alpha_eur": m["alpha_eur"], "alpha_pct": m["alpha_pct"],
        "rebalance_return_pct": m["lines"]["rebalance"]["return_pct"], "idle_return_pct": m["lines"]["idle"]["return_pct"],
        "hold_return_pct": m["lines"]["hold"]["return_pct"], "price_change_pct": m["price_change_pct"],
        "n_cycles": m["n_cycles"], "n_fills": m["n_fills"], "fees_eur": m["fees_eur"],
        "max_drawdown_pct": m["max_drawdown_pct"], "btc_growth_pct": m["btc_growth_pct"], "eur_growth_pct": m["eur_growth_pct"],
        "maker_fee": result.maker_fee, "step_pct": result.step_pct, "source": result.meta.get("source", ""),
    }


class App:
    """All server state: settings, the running jobs and their results."""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        if SETTINGS_FILE.exists():
            try:
                self.settings = clean_settings(json.loads(SETTINGS_FILE.read_text()))
            except (ValueError, TypeError, json.JSONDecodeError):
                self.settings = {}
        self.market: Optional[MarketSpec] = None
        self.market_fee: Optional[float] = None
        self.fee_source = ""
        self.market_error: Optional[str] = None
        self.check: dict[str, Any] = {"status": "idle"}

        self.backtest: dict[str, Any] = {"status": "idle", "messages": []}
        self.paper: Optional[PaperSession] = None
        self.paper_task: Optional[asyncio.Task] = None
        self.paper_info: dict[str, Any] = {"status": "idle"}
        self.paper_log: deque[str] = deque(maxlen=300)
        self.rec_stream: Optional[MarketStream] = None
        self.rec_task: Optional[asyncio.Task] = None
        self.rec_info: dict[str, Any] = {"status": "idle"}
        self.rec_log: deque[str] = deque(maxlen=50)
        self.stop_server = asyncio.Event()

    # ---------------------------------------------------------------- settings
    def config(self, **extra: Any) -> GridConfig:
        return GridConfig(**{**self.settings, **extra})

    def save_settings(self, raw: dict[str, Any]) -> None:
        self.settings = clean_settings(raw)
        DATA.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        if self.market is not None:
            self._set_fee(BitvavoRest(self.config()))

    def _set_fee(self, rest: BitvavoRest) -> None:
        assert self.market is not None
        self.market_fee, self.fee_source = rest.resolve_maker_fee(self.market)

    def _load_market(self) -> tuple[MarketSpec, float, str]:
        """Blocking: market rules and maker fee (runs in a worker thread)."""
        rest = BitvavoRest(self.config())
        market = rest.load_market()
        fee, source = rest.resolve_maker_fee(market)
        return market, fee, source

    async def refresh_market(self) -> None:
        try:
            self.market, self.market_fee, self.fee_source = await asyncio.to_thread(self._load_market)
            self.market_error = None
        except Exception as exc:
            self.market_error = str(exc)

    def derived(self) -> dict[str, Any]:
        cfg = self.config()
        fee = self.market_fee if self.market_fee is not None else cfg.maker_fee_fallback
        return {
            "order_size_eur": cfg.order_size_eur, "maker_fee": fee, "fee_source": self.fee_source,
            "min_step_pct": cfg.min_step_pct(fee), "effective_step_pct": cfg.effective_step_pct(fee),
            "min_cost": self.market.min_cost if self.market else 5.0,
            "defaults": {k: getattr(GridConfig(), k) for k in list(EDITABLE) + ["maker_fee_override", "fill_on_touch"]},
        }

    # ---------------------------------------------------------------- reports
    def reports(self) -> list[dict[str, Any]]:
        index: dict[str, Any] = {}
        if INDEX_FILE.exists():
            try:
                index = json.loads(INDEX_FILE.read_text())
            except json.JSONDecodeError:
                index = {}
        out = []
        for path in REPORTS.glob("dashboard_*.html"):
            entry = dict(index.get(path.name, {}))
            entry.update(file=path.name, mtime=path.stat().st_mtime, size=path.stat().st_size)
            if "mode" not in entry:
                entry["mode"] = "paper" if "_paper_" in path.name else ("backtest-ticks" if "_ticks_" in path.name else "backtest-candles")
            out.append(entry)
        return sorted(out, key=lambda e: -e["mtime"])

    def _index(self, summary: dict[str, Any]) -> None:
        index = json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else {}
        index[summary["file"]] = summary
        REPORTS.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(index, indent=1))

    def _publish(self, result: RunResult) -> dict[str, Any]:
        """Blocking: write the dashboard and index it."""
        path = write_dashboard(result)
        summary = summarize(result, path.name)
        self._index(summary)
        return summary

    def recordings(self) -> list[dict[str, Any]]:
        out = []
        if RECORDINGS.exists():
            for path in RECORDINGS.iterdir():
                if path.suffix == ".sqlite" or path.is_dir():
                    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size
                    out.append({"name": path.name, "size": size, "mtime": path.stat().st_mtime,
                                "format": "parquet" if path.is_dir() else "sqlite",
                                "active": self.rec_info.get("status") == "running" and self.rec_info.get("name") == path.name})
        return sorted(out, key=lambda e: -e["mtime"])

    # ---------------------------------------------------------------- backtest
    def _backtest_job(self, cfg: GridConfig, params: dict[str, Any]) -> dict[str, Any]:
        """Blocking: download what is missing, replay, write the dashboard."""
        say = self.backtest["messages"].append
        rest = BitvavoRest(cfg)
        market = rest.load_market()
        fee, source = rest.resolve_maker_fee(market)
        if params.get("recording"):
            path = RECORDINGS / Path(params["recording"]).name
            say("Opname inlezen…")
            events = load_recording(path)
            say(f"{len(events)} opgenomen gebeurtenissen afspelen…")
            result = run_tick_backtest(cfg, market, fee, events, source, source=path.name)
        else:
            timeframe = params.get("timeframe", "1m")
            end = int(time.time() * 1000)
            start = end - int(float(params.get("days", 90)) * 86_400_000)
            say("Koersdata ophalen (alleen wat nog niet lokaal staat)…")
            candles = rest.fetch_ohlcv_cached(timeframe, start, end, progress=lambda line: say(line.strip()))
            if len(candles) < 2:
                raise ValueError("Te weinig koersdata voor deze periode.")
            say(f"Grid simuleren over {len(candles):,} candles…".replace(",", "."))
            result = run_candle_backtest(cfg, market, fee, candles, source, timeframe)
        say("Rapport maken…")
        return self._publish(result)

    async def start_backtest(self, params: dict[str, Any]) -> None:
        if self.backtest["status"] == "running":
            raise ValueError("Er loopt al een backtest.")
        if params.get("timeframe", "1m") not in TIMEFRAME_MS:
            raise ValueError("Onbekend interval.")
        if not 0.01 <= float(params.get("days", 90)) <= 1500:
            raise ValueError("Periode moet tussen 0,01 en 1500 dagen liggen.")
        cfg = self.config()
        self.backtest = {"status": "running", "messages": [], "started": time.time(), "params": params}

        async def run() -> None:
            job = self.backtest
            try:
                job["result"] = await asyncio.to_thread(self._backtest_job, cfg, params)
                job["status"] = "done"
            except Exception as exc:
                job["status"], job["error"] = "error", str(exc) or type(exc).__name__
            job["finished"] = time.time()

        asyncio.create_task(run())

    # ------------------------------------------------------------------- paper
    async def start_paper(self) -> None:
        if self.paper_info["status"] in ("starting", "running", "stopping"):
            raise ValueError("Paper-trading loopt al.")
        self.paper_info = {"status": "starting", "started": time.time()}
        self.paper_log.clear()
        try:
            cfg = self.config(status_interval_s=1e12)  # the GUI shows live status itself
            market, fee, source = await asyncio.to_thread(self._load_market)
            self.market, self.market_fee, self.fee_source = market, fee, source
            # Fail early (e.g. order size below the exchange minimum) instead of inside the stream.
            from grid_engine import GridEngine
            GridEngine(cfg, market, fee, 50_000.0)
            session = PaperSession(cfg, market=market, maker_fee=fee, fee_source=source,
                                   log=lambda line: self.paper_log.append(line.strip()), install_signals=False)
        except Exception as exc:
            self.paper_info = {"status": "error", "error": str(exc)}
            raise ValueError(str(exc)) from exc
        self.paper = session
        self.paper_info["status"] = "running"
        self.paper_task = asyncio.create_task(self._paper_run(session))

    async def _paper_run(self, session: PaperSession) -> None:
        info = self.paper_info
        try:
            result = await session.run()
            if result is None:
                info.update(status="error", error="Geen marktdata ontvangen van Bitvavo.")
            else:
                info["result"] = await asyncio.to_thread(self._publish, result)
                info["status"] = "done"
        except Exception as exc:
            info.update(status="error", error=str(exc) or type(exc).__name__)
        info["finished"] = time.time()

    async def stop_paper(self) -> None:
        if self.paper is not None and self.paper_info["status"] == "running":
            self.paper_info["status"] = "stopping"
            self.paper.stream.stop()

    def paper_state(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.paper_info)
        out["log"] = list(self.paper_log)[-80:]
        s = self.paper
        if s is None or out["status"] not in ("running", "stopping"):
            return out
        out["feed"] = {"stale": s.stream.staleness(), "reconnects": s.stream.reconnects, "counts": dict(s.stream.counts),
                       "bid": s.stream.bid, "ask": s.stream.ask, "stale_seconds": round(s.stale_seconds)}
        e, rec = s.engine, s.recorder
        if e is None or rec is None or s.stream.bid is None or s.stream.ask is None:
            return out
        mid = (s.stream.bid + s.stream.ask) / 2
        bench = rec.benchmarks.last
        usage_eur, usage_btc = e.reserve_usage()
        start = e.initial_btc * rec.start_price + e.initial_eur
        out["live"] = {
            "price": mid, "start_price": rec.start_price, "equity": e.equity(mid), "start_capital": start,
            "bench_rebal": bench.rebalance, "bench_idle": bench.idle, "bench_hold": bench.hold,
            "btc": e.btc, "eur": e.eur, "btc_start": e.initial_btc, "eur_start": e.initial_eur,
            "btc_weight": e.btc_weight(mid), "n_fills": len(e.fills), "n_cycles": len(e.cycles), "fees": e.fees_paid,
            "cycle_profit": sum(c.profit_total_eur for c in e.cycles), "halted": e.halted,
            "window": list(e.window), "usage_eur": usage_eur, "usage_btc": usage_btc,
            "step_pct": e.step, "maker_fee": e.fee, "order_size": e.order_size,
            "min_weight": s.cfg.min_btc_weight, "max_weight": s.cfg.max_btc_weight,
        }
        out["orders"] = [{"side": o.side.value, "price": o.price, "amount": o.amount} for o in reversed(e.open_orders())]
        out["fills"] = [{"ts": f.ts, "side": f.side.value, "price": f.price, "amount": f.amount, "cost": f.cost,
                         "fee": f.fee, "pnl": f.cycle_pnl_eur} for f in e.fills[-60:]][::-1]
        rows = rec.rows
        stride = max(1, len(rows) // 400)
        picked = rows[::stride] + ([rows[-1]] if (len(rows) - 1) % stride else [])
        out["series"] = {"ts": [r[0] for r in picked], "price": [r[1] for r in picked], "equity": [r[4] for r in picked],
                         "rebal": [r[9] for r in picked], "lo": [r[6] for r in picked], "hi": [r[7] for r in picked]}
        return out

    # ---------------------------------------------------------------- recorder
    async def start_recording(self, params: dict[str, Any]) -> None:
        if self.rec_info["status"] == "running":
            raise ValueError("Er loopt al een opname.")
        fmt = "parquet" if params.get("format") == "parquet" else "sqlite"
        minutes = params.get("minutes")
        duration = float(minutes) * 60 if minutes not in (None, "", 0) else None
        cfg = self.config(status_interval_s=1e12)
        path = default_path(cfg, fmt)
        self.rec_log.clear()
        self.rec_info = {"status": "running", "started": time.time(), "name": path.name, "duration": duration}

        async def run() -> None:
            info = self.rec_info
            try:
                await record(cfg, fmt=fmt, out=str(path), duration_s=duration, log=lambda line: self.rec_log.append(line.strip()),
                             install_signals=False, on_start=lambda stream: setattr(self, "rec_stream", stream))
                info["status"] = "done"
            except Exception as exc:
                info.update(status="error", error=str(exc) or type(exc).__name__)
            info["finished"] = time.time()
            self.rec_stream = None

        self.rec_task = asyncio.create_task(run())

    def recorder_state(self) -> dict[str, Any]:
        out = dict(self.rec_info)
        s = self.rec_stream
        if s is not None and out["status"] == "running":
            out.update(counts=dict(s.counts), bid=s.bid, ask=s.ask, stale=s.staleness(), reconnects=s.reconnects)
        return out

    # ------------------------------------------------------------------- check
    async def run_check(self) -> None:
        if self.check.get("status") == "running":
            return
        self.check = {"status": "running"}
        try:
            rest = BitvavoRest(self.config())
            caps = await asyncio.to_thread(rest.capabilities)
            await self.refresh_market()
            if self.market_error:
                raise RuntimeError(self.market_error)
            stream = MarketStream(self.config())
            task = asyncio.create_task(stream.run())
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not (stream.counts["book"] and stream.counts["ticker"]):
                await asyncio.sleep(0.25)
            await asyncio.sleep(2)
            stream.stop()
            await task
            self.check = {"status": "done", "ccxt_version": caps["ccxt_version"], "ws": caps["ws"], "rest": caps["rest"],
                          "live": dict(stream.counts), "bid": stream.bid, "ask": stream.ask,
                          "ok": bool(all(caps["ws"].values()) and stream.counts["book"] and stream.counts["ticker"])}
        except Exception as exc:
            self.check = {"status": "error", "error": str(exc) or type(exc).__name__}

    # ---------------------------------------------------------------- shutdown
    async def shutdown(self) -> None:
        """Stop running sessions cleanly; a live paper session still gets its dashboard."""
        await self.stop_paper()
        if self.rec_stream is not None:
            self.rec_stream.stop()
        for task in (self.paper_task, self.rec_task):
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, 30)
                except Exception:
                    pass

    def state(self) -> dict[str, Any]:
        market = self.market.to_dict() if self.market else None
        return {
            "settings": self.config().to_dict(), "derived": self.derived(), "market": market, "market_error": self.market_error,
            "backtest": self.backtest, "paper": self.paper_state(), "recorder": self.recorder_state(),
            "rec_log": list(self.rec_log)[-5:], "reports": self.reports(), "recordings": self.recordings(),
            "check": self.check, "now": time.time(),
        }


# -------------------------------------------------------------------- http layer
def build_app(state: App) -> web.Application:
    routes = web.RouteTableDef()
    plotly_js = get_plotlyjs().encode()

    @web.middleware
    async def guard(request: web.Request, handler):
        # Only this page may change things: refuse cross-site POSTs to the local server.
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                return web.json_response({"error": "verzoek van een andere website geweigerd"}, status=403)
        try:
            return await handler(request)
        except (ValueError, TypeError, FileNotFoundError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("ongeldige invoer")
        return data

    def ok() -> web.Response:
        return web.json_response(state.state(), dumps=lambda o: json.dumps(o, default=str))

    @routes.get("/")
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=(ROOT / "gui.html").read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @routes.get("/plotly.js")
    async def plotly(_: web.Request) -> web.Response:
        return web.Response(body=plotly_js, content_type="application/javascript", headers={"Cache-Control": "max-age=86400"})

    @routes.get("/api/state")
    async def get_state(_: web.Request) -> web.Response:
        return ok()

    @routes.post("/api/settings")
    async def post_settings(request: web.Request) -> web.Response:
        state.save_settings(await body(request))
        return ok()

    @routes.post("/api/backtest")
    async def post_backtest(request: web.Request) -> web.Response:
        await state.start_backtest(await body(request))
        return ok()

    @routes.post("/api/paper/start")
    async def paper_start(_: web.Request) -> web.Response:
        await state.start_paper()
        return ok()

    @routes.post("/api/paper/stop")
    async def paper_stop(_: web.Request) -> web.Response:
        await state.stop_paper()
        return ok()

    @routes.post("/api/record/start")
    async def record_start(request: web.Request) -> web.Response:
        await state.start_recording(await body(request))
        return ok()

    @routes.post("/api/record/stop")
    async def record_stop(_: web.Request) -> web.Response:
        if state.rec_stream is not None:
            state.rec_stream.stop()
        return ok()

    @routes.post("/api/check")
    async def check(_: web.Request) -> web.Response:
        asyncio.create_task(state.run_check())
        await asyncio.sleep(0.05)
        return ok()

    @routes.post("/api/reports/delete")
    async def delete_report(request: web.Request) -> web.Response:
        name = Path(str((await body(request)).get("file", ""))).name
        path = REPORTS / name
        if not (name.startswith("dashboard_") and name.endswith(".html") and path.exists()):
            raise FileNotFoundError("rapport niet gevonden")
        path.unlink()
        return ok()

    @routes.post("/api/recordings/delete")
    async def delete_recording(request: web.Request) -> web.Response:
        name = Path(str((await body(request)).get("name", ""))).name
        path = RECORDINGS / name
        if not name.startswith("ws_") or not path.exists():
            raise FileNotFoundError("opname niet gevonden")
        if state.rec_info.get("status") == "running" and state.rec_info.get("name") == name:
            raise ValueError("deze opname loopt nog")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            for extra in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                extra.unlink(missing_ok=True)
        return ok()

    @routes.post("/api/shutdown")
    async def shutdown(_: web.Request) -> web.Response:
        state.stop_server.set()
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard])
    app.add_routes(routes)
    REPORTS.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/reports/", REPORTS, show_index=False)
    return app


async def serve(port: int, open_browser: bool) -> None:
    state = App()
    runner = web.AppRunner(build_app(state), access_log=None)
    await runner.setup()
    url = f"http://localhost:{port}"
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except OSError:
        print(f"Poort {port} is al in gebruik - waarschijnlijk draait de server al. Ik open {url}.")
        if open_browser:
            webbrowser.open(url)
        await runner.cleanup()
        return
    install_signal_handlers(state.stop_server.set)
    asyncio.create_task(state.refresh_market())
    print(f"\n  Grid Bot draait op {url}\n  Laat dit venster open. Stoppen: knop 'Server stoppen' in de browser, of Ctrl+C hier.\n")
    if open_browser:
        webbrowser.open(url)
    await state.stop_server.wait()
    print("Afsluiten… lopende sessies worden netjes gestopt en opgeslagen.")
    await state.shutdown()
    await runner.cleanup()
    print("Gestopt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start de webinterface van de grid bot")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="open de browser niet automatisch")
    args = parser.parse_args()
    asyncio.run(serve(args.port, not args.no_browser))


if __name__ == "__main__":
    main()
