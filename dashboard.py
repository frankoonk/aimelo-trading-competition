"""Self-contained interactive HTML report, generated after every run.

One file, no server: Plotly is embedded (or loaded from a CDN with
``inline_js=False``), all data sits inside the page. Dark theme; the palette is
a colourblind-validated set, and buy/sell fills differ in marker shape as well
as colour.
"""
from __future__ import annotations

import html
import json
import math
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

from results import RunResult, compute_metrics, drawdown

# Palette (dark surface). Series colours follow the entity, in fixed order.
SURFACE, PAGE, INK, INK2, MUTED, GRID = "#1a1a19", "#0d0d0d", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a"
C_GRID, C_REBAL, C_IDLE, C_HOLD = "#3987e5", "#d95926", "#199e70", "#c98500"
C_BAND = "#9085e9"
C_BUY, C_SELL, C_WARN = "#0ca30c", "#e66767", "#fab219"
MAX_POINTS = 3000
MAX_CANDLES = 1200
MAX_TABLE_ROWS = 5000

SERIES = [
    ("equity", "25/75 Grid Bot", C_GRID, 2.5),
    ("bench_rebal", "50/50 Herbalanceren", C_REBAL, 1.75),
    ("bench_idle", "50/50 Stil", C_IDLE, 1.75),
    ("bench_hold", "100% BTC Buy & Hold", C_HOLD, 1.75),
]
MODE_LABEL = {"backtest-candles": "Backtest · candles", "backtest-ticks": "Backtest · WebSocket-opname", "paper": "Paper-trading · live"}


# ------------------------------------------------------------------ formatting
def _nl(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eur(value: float, decimals: int = 2, sign: bool = False) -> str:
    prefix = ("+" if value >= 0 else "−") if sign else ("−" if value < 0 else "")
    return f"{prefix}€ {_nl(abs(value), decimals)}"


def _pct(value: float, decimals: int = 2, sign: bool = True) -> str:
    prefix = ("+" if value >= 0 else "−") if sign else ("−" if value < 0 else "")
    return f"{prefix}{_nl(abs(value), decimals)}%"


def _naive(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_convert("UTC").tz_localize(None)


def _downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Thin a time series to at most ``max_points`` rows (last value per time bucket)."""
    if len(df) <= max_points:
        return df
    seconds = max(1, math.ceil((df.index[-1] - df.index[0]).total_seconds() / max_points))
    return df.resample(f"{seconds}s").last().dropna(how="all")


def _candles(result: RunResult) -> Optional[pd.DataFrame]:
    """Candles for the price chart, aggregated so the browser stays fast."""
    ohlc = result.ohlc
    if ohlc is None or len(ohlc) < 2:
        return None
    if len(ohlc) > MAX_CANDLES:
        seconds = math.ceil((ohlc.index[-1] - ohlc.index[0]).total_seconds() / MAX_CANDLES)
        minutes = next((m for m in (1, 5, 15, 30, 60, 120, 240, 360, 720, 1440, 4320, 10080) if m * 60 >= seconds), 10080)
        ohlc = ohlc.resample(f"{minutes}min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return ohlc


# --------------------------------------------------------------------- figures
def _layout(fig: go.Figure, height: int, y_title: str = "", **extra: Any) -> go.Figure:
    fig.update_layout(
        height=height, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="Inter, -apple-system, 'Segoe UI', Roboto, sans-serif", color=INK2, size=12),
        margin=dict(l=64, r=24, t=16, b=40), hovermode="x unified", separators=",.",
        hoverlabel=dict(bgcolor="#262624", bordercolor=GRID, font=dict(color=INK, size=12)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=INK2), bgcolor="rgba(0,0,0,0)"),
        dragmode="zoom", **extra,
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=GRID, zeroline=False, tickfont=dict(color=MUTED), showspikes=True,
                     spikemode="across", spikethickness=1, spikecolor=MUTED, spikedash="dot")
    fig.update_yaxes(gridcolor=GRID, linecolor=GRID, zeroline=False, tickfont=dict(color=MUTED))
    if y_title:
        fig.update_yaxes(title=dict(text=y_title, font=dict(color=MUTED, size=11)))
    return fig


def fig_equity(result: RunResult) -> go.Figure:
    eq = _downsample(result.equity[[s[0] for s in SERIES]], MAX_POINTS)
    x = _naive(eq.index)
    fig = go.Figure()
    for column, name, color, width in SERIES:
        fig.add_trace(go.Scatter(x=x, y=eq[column], name=name, mode="lines", line=dict(color=color, width=width),
                                 hovertemplate="€ %{y:,.2f}<extra>" + name + "</extra>"))
    return _layout(fig, 420, "Portefeuillewaarde (€)")


def fig_price(result: RunResult) -> go.Figure:
    fig = go.Figure()
    candles = _candles(result)
    if candles is not None:
        fig.add_trace(go.Candlestick(
            x=_naive(candles.index), open=candles["open"], high=candles["high"], low=candles["low"], close=candles["close"],
            name="BTC/EUR", increasing=dict(line=dict(color="#a8a79e", width=1), fillcolor="#a8a79e"),
            decreasing=dict(line=dict(color="#5f5e59", width=1), fillcolor="#5f5e59"), hoverinfo="skip"))
    line = _downsample(result.equity[["price", "grid_lo", "grid_hi"]], MAX_POINTS)
    x = _naive(line.index)
    if candles is None:
        fig.add_trace(go.Scatter(x=x, y=line["price"], name="BTC/EUR", mode="lines", line=dict(color=INK2, width=1.5),
                                 hovertemplate="€ %{y:,.0f}<extra>BTC/EUR</extra>"))
    else:  # invisible carrier so the unified hover still shows the price
        fig.add_trace(go.Scatter(x=x, y=line["price"], name="Koers", mode="lines", line=dict(color="rgba(0,0,0,0)", width=0),
                                 showlegend=False, hovertemplate="€ %{y:,.0f}<extra>Koers</extra>"))
    fig.add_trace(go.Scatter(x=x, y=line["grid_hi"], name="Grid-bovengrens", mode="lines",
                             line=dict(color=C_BAND, width=1.25, shape="hv"), hovertemplate="€ %{y:,.0f}<extra>Grid boven</extra>"))
    fig.add_trace(go.Scatter(x=x, y=line["grid_lo"], name="Grid-ondergrens", mode="lines", fill="tonexty",
                             fillcolor="rgba(144,133,233,0.07)", line=dict(color=C_BAND, width=1.25, shape="hv", dash="dot"),
                             hovertemplate="€ %{y:,.0f}<extra>Grid onder</extra>"))
    for side, name, color, symbol in (("buy", "Buy fill", C_BUY, "triangle-up"), ("sell", "Sell fill", C_SELL, "triangle-down")):
        fills = [f for f in result.fills if f.side.value == side]
        fig.add_trace(go.Scattergl(
            x=[datetime.fromtimestamp(f.ts / 1000, timezone.utc).replace(tzinfo=None) for f in fills],
            y=[f.price for f in fills], name=f"{name} ({len(fills)})", mode="markers",
            marker=dict(symbol=symbol, size=9, color=color, line=dict(color=SURFACE, width=1)),
            customdata=[[f.amount, f.cost] for f in fills],
            hovertemplate=name + " € %{y:,.0f} · %{customdata[0]:.8f} BTC<extra></extra>"))
    fig = _layout(fig, 480, "BTC/EUR (€)")
    fig.update_layout(hovermode="closest", xaxis_rangeslider_visible=False)
    return fig


def fig_inventory(result: RunResult) -> go.Figure:
    """BTC stock, EUR stock and BTC weight as three stacked panels (no dual axis)."""
    cfg = result.config
    eq = _downsample(result.equity[["btc", "eur", "btc_weight"]], MAX_POINTS)
    x = _naive(eq.index)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=("BTC-voorraad", "EUR-voorraad", "BTC-weging van de portefeuille (inventory drift)"))
    fig.add_trace(go.Scatter(x=x, y=eq["btc"], name="BTC", mode="lines", line=dict(color=C_HOLD, width=2),
                             hovertemplate="%{y:.8f} BTC<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=eq["eur"], name="EUR", mode="lines", line=dict(color=C_IDLE, width=2),
                             hovertemplate="€ %{y:,.2f}<extra></extra>"), row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=eq["btc_weight"] * 100, name="BTC-weging", mode="lines", line=dict(color=C_GRID, width=2),
                             hovertemplate="%{y:.1f}%<extra></extra>"), row=3, col=1)
    fig.add_hline(y=result.initial_btc, line=dict(color=MUTED, width=1, dash="dot"), row=1, col=1)
    fig.add_hline(y=result.initial_eur, line=dict(color=MUTED, width=1, dash="dot"), row=2, col=1)
    for level, label in ((cfg.max_btc_weight, "max"), (cfg.min_btc_weight, "min")):
        fig.add_hline(y=level * 100, line=dict(color=C_WARN, width=1.25, dash="dash"), row=3, col=1,
                      annotation_text=f"drift-limiet {label} {level * 100:.0f}%", annotation_font=dict(color=INK2, size=11),
                      annotation_position="top left" if label == "max" else "bottom left")
    fig = _layout(fig, 640, showlegend=False)
    fig.update_layout(margin=dict(l=64, r=24, t=36, b=40))
    fig.update_annotations(font=dict(color=INK2, size=12))
    lo, hi = min(cfg.min_btc_weight * 100, eq["btc_weight"].min() * 100), max(cfg.max_btc_weight * 100, eq["btc_weight"].max() * 100)
    fig.update_yaxes(range=[max(0, lo - 8), min(100, hi + 8)], ticksuffix="%", row=3, col=1)
    fig.update_yaxes(tickformat=".4f", row=1, col=1)
    return fig


def fig_drawdown(result: RunResult) -> go.Figure:
    eq = _downsample(result.equity[["equity", "bench_rebal"]], MAX_POINTS)
    x = _naive(eq.index)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=drawdown(eq["equity"]) * 100, name="25/75 Grid Bot", mode="lines", fill="tozeroy",
                             fillcolor="rgba(57,135,229,0.18)", line=dict(color=C_GRID, width=2),
                             hovertemplate="%{y:.2f}%<extra>Grid Bot</extra>"))
    fig.add_trace(go.Scatter(x=x, y=drawdown(eq["bench_rebal"]) * 100, name="50/50 Herbalanceren", mode="lines",
                             line=dict(color=C_REBAL, width=1.5), hovertemplate="%{y:.2f}%<extra>Herbalanceren</extra>"))
    fig = _layout(fig, 300, "Drawdown t.o.v. vorige top")
    fig.update_yaxes(ticksuffix="%")
    return fig


# ------------------------------------------------------------------------ html
def _card(label: str, value: str, sub: str = "", trend: Optional[float] = None, hero: bool = False) -> str:
    arrow = ""
    if trend is not None and abs(trend) > 1e-12:
        arrow = f'<span class="arrow {"up" if trend > 0 else "down"}">{"▲" if trend > 0 else "▼"}</span>'
    return (f'<div class="card{" hero" if hero else ""}"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{arrow}{html.escape(value)}</div><div class="sub">{html.escape(sub)}</div></div>')


def _cards(m: dict[str, Any]) -> str:
    na = "n.v.t."
    return "".join([
        _card("Eindwaarde Grid Bot", _eur(m["final_equity"]),
              f"start {_eur(m['start_capital'])} · {_eur(m['pnl'], sign=True)} ({_pct(m['return_pct'])})", m["pnl"], hero=True),
        _card("Grid-Alpha vs 50/50 herbalanceren", _eur(m["alpha_eur"], sign=True),
              f"{_pct(m['alpha_pct'])}-punt van startkapitaal · vs stil {_eur(m['alpha_vs_idle_eur'], sign=True)}", m["alpha_eur"], hero=True),
        _card("BTC-voorraad", _pct(m["btc_growth_pct"]), f"{m['btc_start']:.8f} → {m['btc_end']:.8f} BTC", m["btc_growth_pct"]),
        _card("EUR-voorraad", _pct(m["eur_growth_pct"]), f"{_eur(m['eur_start'])} → {_eur(m['eur_end'])}", m["eur_growth_pct"]),
        _card("Afgeronde grid-cycli", _nl(m["n_cycles"], 0), f"{_nl(m['n_fills'], 0)} fills · {m['n_buys']} buy / {m['n_sells']} sell"),
        _card("Gem. winst per cyclus", na if m["avg_cycle_profit_eur"] is None else _eur(m["avg_cycle_profit_eur"], 3),
              f"totaal uit cycli {_eur(m['cycle_profit_eur'])}"),
        _card("Betaalde maker-fees", _eur(m["fees_eur"]), f"over {_nl(m['n_fills'], 0)} fills"),
        _card("Sharpe ratio", na if m["sharpe"] is None else _nl(m["sharpe"]), "geannualiseerd, risicovrij 0%"),
        _card("Maximale drawdown", _pct(m["max_drawdown_pct"], sign=False),
              f"herbalanceren {_pct(m['lines']['rebalance']['max_drawdown_pct'], sign=False)}"),
        _card("BTC-weging nu", _pct(m["btc_weight_end"], 1, sign=False),
              f"bereik {_nl(m['btc_weight_min'], 1)}% – {_nl(m['btc_weight_max'], 1)}%"),
    ])


def _comparison(m: dict[str, Any]) -> str:
    names = {"grid": ("25/75 Grid Bot", C_GRID), "rebalance": ("50/50 Herbalanceren", C_REBAL),
             "idle": ("50/50 Stil", C_IDLE), "hold": ("100% BTC Buy & Hold", C_HOLD)}
    rows = []
    for key, (name, color) in names.items():
        line = m["lines"][key]
        sharpe = "n.v.t." if line["sharpe"] is None else _nl(line["sharpe"])
        rows.append(
            f'<tr><td><span class="swatch" style="background:{color}"></span>{name}</td><td class="num">{_eur(line["final"])}</td>'
            f'<td class="num">{_eur(line["pnl"], sign=True)}</td><td class="num">{_pct(line["return_pct"])}</td>'
            f'<td class="num">{_pct(line["max_drawdown_pct"], sign=False)}</td><td class="num">{sharpe}</td>'
            f'<td class="num">{_eur(m["final_equity"] - line["final"], sign=True) if key != "grid" else "—"}</td></tr>')
    return ('<table class="plain"><thead><tr><th>Strategie</th><th class="num">Eindwaarde</th><th class="num">Resultaat</th>'
            '<th class="num">Rendement</th><th class="num">Max drawdown</th><th class="num">Sharpe</th>'
            '<th class="num">Grid Bot t.o.v.</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _events_html(result: RunResult) -> str:
    events = [e for e in result.events if "post_only" not in e.detail]
    if not events:
        return '<p class="muted">Geen trailing-stappen of guardrail-stops in deze run.</p>'
    label = {"trail_up": "Trailing ↑", "trail_down": "Trailing ↓", "halt": "Guard-stop", "resume": "Hervat"}
    rows = "".join(
        f"<tr><td>{datetime.fromtimestamp(e.ts / 1000, timezone.utc):%Y-%m-%d %H:%M:%S}</td><td>{label.get(e.kind, e.kind)}</td>"
        f'<td>{html.escape(e.detail)}</td><td class="num">{_eur(e.price, 0)}</td></tr>' for e in events[-300:])
    note = f" (laatste 300 van {len(events)})" if len(events) > 300 else ""
    return (f'<div class="scroll small"><table class="plain"><thead><tr><th>Tijd (UTC){note}</th><th>Gebeurtenis</th><th>Detail</th>'
            f'<th class="num">Niveau</th></tr></thead><tbody>{rows}</tbody></table></div>')


def _fills_json(result: RunResult) -> str:
    rows = [[f.ts, f.side.value, f.price, f.amount, round(f.cost, 2), round(f.fee, 4),
             None if f.cycle_pnl_eur is None else round(f.cycle_pnl_eur, 4)] for f in result.fills[-MAX_TABLE_ROWS:]]
    return json.dumps(rows[::-1], separators=(",", ":"))


CSS = """
:root{--page:#0d0d0d;--surface:#1a1a19;--raise:#222220;--line:#2c2c2a;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 Inter,-apple-system,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1320px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;flex-wrap:wrap;gap:8px 24px;align-items:baseline;justify-content:space-between;margin-bottom:20px}
h1{font-size:22px;font-weight:650;margin:0;letter-spacing:-.01em}
h2{font-size:15px;font-weight:600;margin:0 0 2px}
.meta{color:var(--muted);font-size:13px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 10px;color:var(--ink2);font-size:12px;margin-right:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.card.hero{grid-column:span 2}
.card .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;margin:4px 0 2px;white-space:nowrap}
.card.hero .value{font-size:32px}
.card .sub{color:var(--ink2);font-size:12.5px;font-variant-numeric:tabular-nums}
.arrow{font-size:.55em;vertical-align:middle;margin-right:7px}.arrow.up{color:#0ca30c}.arrow.down{color:#e66767}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 16px 8px;margin-bottom:16px;min-width:0}
.panel p.desc{color:var(--muted);font-size:12.5px;margin:0 0 6px}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:500;font-size:12px;text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--surface);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid #232321;color:var(--ink2);white-space:nowrap}
tbody tr:hover td{background:var(--raise);color:var(--ink)}
.num{text-align:right}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:8px}
.scroll{max-height:460px;overflow:auto;border-top:1px solid var(--line)}.scroll.small{max-height:260px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 10px}
input[type=search]{background:var(--page);border:1px solid var(--line);color:var(--ink);border-radius:7px;padding:7px 10px;min-width:240px;font:inherit}
button{background:var(--page);border:1px solid var(--line);color:var(--ink2);border-radius:7px;padding:6px 12px;font:inherit;cursor:pointer}
button.on{border-color:#3987e5;color:var(--ink)}
.side{font-weight:600;color:var(--ink)}.side i{font-style:normal;margin-right:6px}.side.buy i{color:#0ca30c}.side.sell i{color:#e66767}
.muted{color:var(--muted)} .count{color:var(--muted);font-size:12.5px;margin-left:auto}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:4px 24px;font-size:13px;padding-bottom:8px}
.settings div{display:flex;justify-content:space-between;border-bottom:1px solid #232321;padding:4px 0}
.settings span:first-child{color:var(--muted)}
footer{color:var(--muted);font-size:12px;margin-top:20px}
@media(max-width:900px){.two{grid-template-columns:minmax(0,1fr)}.card.hero{grid-column:span 1}.card.hero .value{font-size:26px}}
"""

JS = """
const FILLS = __FILLS__;
const nf = (d) => new Intl.NumberFormat('nl-NL', {minimumFractionDigits: d, maximumFractionDigits: d});
const f0 = nf(0), f2 = nf(2), f3 = nf(3), f4 = nf(4);
const rows = FILLS.map(r => {
  const t = new Date(r[0]).toISOString().replace('T', ' ').slice(0, 19);
  const side = r[1] === 'buy' ? 'BUY' : 'SELL';
  const cells = [t, side, '€ ' + f0.format(r[2]), r[3].toFixed(8), '€ ' + f2.format(r[4]), '€ ' + f4.format(r[5]),
                 r[6] === null ? '' : '+€ ' + f3.format(r[6])];
  return {side: r[1], cycle: r[6] !== null, text: cells.join(' ').toLowerCase(), cells};
});
let filter = 'all', shown = 200;
const body = document.getElementById('fills-body'), count = document.getElementById('fills-count');
function render() {
  const q = document.getElementById('fills-search').value.trim().toLowerCase();
  const hit = rows.filter(r => (filter === 'all' || (filter === 'cycle' ? r.cycle : r.side === filter)) && (!q || r.text.includes(q)));
  body.innerHTML = hit.slice(0, shown).map(r =>
    `<tr><td>${r.cells[0]}</td><td class="side ${r.side}"><i>${r.side === 'buy' ? '▲' : '▼'}</i>${r.cells[1]}</td>` +
    r.cells.slice(2).map(c => `<td class="num">${c}</td>`).join('') + '</tr>').join('');
  count.textContent = `${Math.min(shown, hit.length)} van ${hit.length} getoond` + (hit.length > shown ? ' · ' : '');
  document.getElementById('fills-more').style.display = hit.length > shown ? '' : 'none';
}
document.getElementById('fills-search').addEventListener('input', () => { shown = 200; render(); });
document.getElementById('fills-more').addEventListener('click', () => { shown = Infinity; render(); });
document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
  filter = b.dataset.filter; shown = 200;
  document.querySelectorAll('[data-filter]').forEach(x => x.classList.toggle('on', x === b));
  render();
}));
render();
"""


def build_html(result: RunResult, inline_js: bool = True) -> str:
    """Render the complete dashboard as one HTML string."""
    m = compute_metrics(result)
    cfg = result.config
    plot_config = {"displaylogo": False, "responsive": True, "modeBarButtonsToRemove": ["lasso2d", "select2d"]}

    def div(fig: go.Figure, name: str) -> str:
        return fig.to_html(full_html=False, include_plotlyjs=False, div_id=name, config=plot_config)

    plotly_tag = (f"<script>{get_plotlyjs()}</script>" if inline_js
                  else '<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>')
    settings = [
        ("Markt", cfg.symbol), ("Bron", str(result.meta.get("source", "-"))),
        ("Startkoers", _eur(result.start_price, 0)), ("Eindkoers", f"{_eur(m['end_price'], 0)} ({_pct(m['price_change_pct'])})"),
        ("Maker fee", f"{_nl(result.maker_fee * 100, 3)}% ({result.fee_source})"),
        ("Fee-vloer / gebruikte stap", f"{_nl(cfg.min_step_pct(result.maker_fee) * 100, 3)}% / {_nl(result.step_pct * 100, 3)}%"),
        ("Grid", f"{cfg.orders_per_side} buy + {cfg.orders_per_side} sell × {_eur(cfg.order_size_eur)}"),
        ("Actief / reserve", f"{_nl(cfg.active_fraction * 100, 0)}% / {_nl((1 - cfg.active_fraction) * 100, 0)}%"),
        ("Drift-limiet BTC-weging", f"{_nl(cfg.min_btc_weight * 100, 0)}% – {_nl(cfg.max_btc_weight * 100, 0)}%"),
        ("Max. reservegebruik", f"{_nl(cfg.max_reserve_usage * 100, 0)}%"),
        ("Winstverdeling BTC / EUR", f"{_nl(cfg.profit_split_btc * 100, 0)}% / {_nl((1 - cfg.profit_split_btc) * 100, 0)}%"),
        ("Fill-model", "trade op limietprijs" if cfg.fill_on_touch else "trade dóór limietprijs (conservatief)"),
        ("Trailing ↑ / ↓", f"{m['trail_up']} / {m['trail_down']}"), ("Guardrail-stops", str(m["halts"])),
        ("Herbalanceer-benchmark", f"elke {_nl(cfg.rebalance_interval_hours, 0)} uur · {result.meta.get('rebalances', 0)}×"),
    ]
    if result.mode == "paper":
        settings += [("Reconnects", str(result.meta.get("reconnects", 0))),
                     ("Feed verouderd", f"{result.meta.get('stale_seconds', 0)} s")]
    settings_html = "".join(f"<div><span>{html.escape(k)}</span><span>{html.escape(v)}</span></div>" for k, v in settings)
    table_note = f" (laatste {MAX_TABLE_ROWS})" if len(result.fills) > MAX_TABLE_ROWS else ""
    duration = m["end"] - m["start"]
    days = duration.total_seconds() / 86400
    span = f"{days:.1f} dagen" if days >= 1 else f"{duration.total_seconds() / 60:.0f} minuten"

    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grid Bot {cfg.symbol} · {MODE_LABEL.get(result.mode, result.mode)}</title><style>{CSS}</style>{plotly_tag}</head>
<body><div class="wrap">
<header><div><h1>Spot Grid Bot · {cfg.symbol} · Bitvavo</h1>
<div class="meta"><span class="tag">{MODE_LABEL.get(result.mode, result.mode)}</span>{m['start']:%d-%m-%Y %H:%M} → {m['end']:%d-%m-%Y %H:%M} UTC · {span}</div></div>
<div class="meta">gegenereerd {datetime.now(timezone.utc):%d-%m-%Y %H:%M} UTC</div></header>

<section class="cards">{_cards(m)}</section>

<section class="panel"><h2>Equity curves — Grid Bot vs. drie benchmarks</h2>
<p class="desc">Alle lijnen starten met hetzelfde kapitaal op dezelfde koers. Alleen wat boven 50/50 herbalanceren uitkomt is grid-alpha.</p>
{div(fig_equity(result), "chart-equity")}{_comparison(m)}</section>

<section class="panel"><h2>BTC/EUR koers, grid-fills en meeschuivende bandbreedte</h2>
<p class="desc">▲ buy fill · ▼ sell fill · paarse lijnen: boven- en ondergrens van het actieve grid-venster. Tijden in UTC.</p>
{div(fig_price(result), "chart-price")}</section>

<section class="panel"><h2>Voorraadontwikkeling en inventory drift</h2>
<p class="desc">Stippellijn = startvoorraad. Gele streeplijnen = harde drift-limieten; daarbuiten plaatst de bot geen orders meer aan die kant.</p>
{div(fig_inventory(result), "chart-inventory")}</section>

<div class="two">
<section class="panel"><h2>Drawdown (onderwater-curve)</h2><p class="desc">Daling t.o.v. de hoogste portefeuillewaarde tot dan toe.</p>
{div(fig_drawdown(result), "chart-drawdown")}</section>
<section class="panel"><h2>Trailing en guardrails</h2><p class="desc">Vensterverschuivingen en momenten waarop een limiet het meeschuiven stopte.</p>
{_events_html(result)}</section>
</div>

<section class="panel"><h2>Fills en afgeronde cycli{table_note}</h2>
<div class="tools"><input id="fills-search" type="search" placeholder="Zoek op tijd, prijs, bedrag…">
<button data-filter="all" class="on">Alles</button><button data-filter="buy">Buy</button><button data-filter="sell">Sell</button>
<button data-filter="cycle">Alleen afgeronde cycli</button>
<span class="count"><span id="fills-count"></span><button id="fills-more">toon alles</button></span></div>
<div class="scroll"><table><thead><tr><th>Tijd (UTC)</th><th>Zijde</th><th class="num">Prijs</th><th class="num">BTC</th>
<th class="num">EUR-waarde</th><th class="num">Fee</th><th class="num">Cycluswinst</th></tr></thead><tbody id="fills-body"></tbody></table></div></section>

<section class="panel"><h2>Instellingen van deze run</h2><div class="settings">{settings_html}</div></section>
<footer>Simulatie met gesimuleerde maker-fills; geen echte orders, geen beleggingsadvies. Resultaten uit het verleden bieden geen garantie voor de toekomst.</footer>
</div><script>{JS.replace("__FILLS__", _fills_json(result))}</script></body></html>"""


def write_dashboard(result: RunResult, open_browser: bool = False, inline_js: bool = True,
                    reports_dir: Optional[str] = None) -> Path:
    """Write ``reports/dashboard_<mode>_<timestamp>.html`` and return its path."""
    directory = Path(reports_dir or result.config.reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = directory / f"dashboard_{result.mode.replace('-', '_')}_{stamp}.html"
    path.write_text(build_html(result, inline_js=inline_js), encoding="utf-8")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())
    return path
