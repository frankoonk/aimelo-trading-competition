# Opdracht: Bouw een Deterministische BTC/EUR Spot Grid Trading Bot & Backtest/Paper Sandbox (Python)

Bouw in Python (`3.11+`) een volledig modulaire, deterministische **Spot Grid Trading Bot op Bitvavo (`BTC/EUR`)**, inclusief historische backtester, live WebSocket paper-trading engine, WebSocket data-recorder en een automatisch gegenereerd **interactief HTML-dashboard** aan het eind van elke run.

---

## Fase 0 — Verplicht Vooronderzoek: Is `ccxt` voldoende voor Bitvavo WebSockets?

Voordat je de volledige codebase uitschrijft, voer je eerst een korte technische verificatie uit (en rapporteer je je bevindingen kort):
1. Controleer de geïnstalleerde `ccxt`-versie (`ccxt.pro` is tegenwoordig standaard onderdeel van `ccxt` via `import ccxt.pro as ccxtpro`).
2. Inspecteer de capabilities (`exchange.has`) van `ccxt.pro.bitvavo()` en `ccxt.bitvavo()` voor publieke endpoints op `BTC/EUR`:
   * WebSocket streaming (`watch_*`): `watch_ticker`, `watch_order_book`, `watch_trades`, `watch_ohlcv`
   * REST & market metadata: `load_markets` (voor `tickSize`, `stepSize` en minimale ordergrootte in EUR), `fetch_ohlcv`, en `fetch_trading_fee` / `fetch_trading_fees`
3. **Beslisregel:** Als `ccxt` (`ccxt.pro`) al deze publieke `watch_*`- en REST-functies stabiel ondersteunt voor Bitvavo, gebruik dan **100% `ccxt` / `ccxt.pro`** als enige exchange-library. Alleen als een specifiek Bitvavo WebSocket-kanaal ontbreekt of niet goed werkt in `ccxt.pro`, bouw je voor dat specifieke kanaal een lichte `websockets`-adapter tegen `wss://ws.bitvavo.com/v2/`.

---

## 1. Strategisch Concept & Grid-Mechanica

### A. Doelstelling (Volatility Harvesting / Shannon’s Demon)
* **Markt:** Spot-handel op `Bitvavo BTC/EUR`.
* **Kernprincipe:** De bot doet **geen enkele koersvoorspelling**. Het doel is om koersschommelingen stelselmatig te oogsten met passieve *limit orders*, zodat over een reeks schommelingen zowel de voorraad **BTC (satoshis)** als de voorraad **EUR** groeit.

### B. 25% / 75% Kapitaalarchitectuur
* **Startkapitaal:** Configureerbaar (standaard `€ 10.000,00`), bij aanvang verdeeld in **50% BTC** (`€ 5.000` aan BTC tegen startkoers) en **50% EUR** (`€ 5.000`).
* **25% Actief Grid (`€ 2.500`):** Staat op elk moment als open *maker limit orders* in het `BTC/EUR`-orderboek, verdeeld in `N` kooporders onder de huidige koers en `N` verkooporders boven de huidige koers (standaard `N = 10` per kant, totaal `20` orders, `€ 125` per grid-order).
* **75% Idle Reserve (`€ 7.500`, 50% BTC / 50% EUR):** Wordt buiten het directe orderboek aangehouden zodat het grid kan meeschuiven (*trailen*) als de koers uitbreekt, zonder dat de bot zonder voorraad of cash komt te zitten.

### C. Exacte Order- en Trailing-Logica
1. **Cycle Flip:**
   * Wordt **Buy-order `#1`** (dichtst onder de koers) gevuld op niveau $P_i$, dan plaatst de bot direct één grid-stap hoger ($P_{i+1}$) een nieuwe **Sell-order**.
   * Wordt **Sell-order `#1`** (dichtst boven de koers) gevuld op niveau $P_j$, dan plaatst de bot direct één grid-stap lager ($P_{j-1}$) een nieuwe **Buy-order**.
2. **Trailing / Sliding Window:**
   * Beweegt de koers voorbij de buitenste rand van het actieve grid, dan schuift het hele venster van `N` buy- en `N` sell-orders één stap mee met de koers: de verste order aan de overzijde wordt geannuleerd en de benodigde BTC of EUR wordt vanuit de **75%-reserve** vrijgegeven (mits binnen de inventory-drift limiet).
3. **Multi-Level Gap Afhandeling:**
   * Als de koers in één candle of snelle WebSocket-tick door meerdere grid-niveaus tegelijk beweegt, worden alle doorkruiste niveaus netjes in prijsvolgorde afgehandeld zonder dubbele fills of negatieve saldi.
4. **Bitvavo Market Precision:**
   * Gebruik `exchange.price_to_precision('BTC/EUR', price)` en `exchange.amount_to_precision('BTC/EUR', amount)` en controleer bij elke order of de waarde boven Bitvavo’s minimale ordergrootte (ca. `€ 5,00`) ligt.

---

## 2. Verplichte Quant Guardrails (Deterministisch)

1. **Dynamische Fee-Vloer:**
   * Hardcode nooit een fee-percentage. Lees het actuele `maker`-tarief uit via de Bitvavo-API (met een configureerbare override/fallback parameter voor accounts tussen `0,00%` maker-tier en `0,15%` standaard retail-tier).
   * Bereken de minimale toegestane grid-stapgrootte wiskundig: `min_step_pct = max(2 * maker_fee_pct + safety_margin_pct, min_floor_pct)` (bijvoorbeeld `min_floor_pct = 0.0015` / `15 bps` bij een `0,00%` maker-account, zodat de bot niet op 1 basispunt ruis kapotdraait).
2. **Strikte `postOnly` Maker-Simulatie:**
   * Alle grid-orders zijn uitsluitend *maker limit orders*.
   * In live paper-trading tegen het WebSocket-orderboek mag een gesimuleerde kooporder nooit op of boven de huidige `best_ask` worden gelegd, en een verkooporder nooit op of onder de huidige `best_bid` (om te voorkomen dat een order in de echte wereld als dure *taker* zou vullen).
3. **Harde Inventory-Drift Begrenzing:**
   * Tijdens een langdurige crash of downtrend mag het meeschuivende grid de EUR-reserve niet volledig leegkopen.
   * Bouw een deterministische circuit-breaker in op de totale portfolio-verhouding (bijv. totale BTC-weging mag nooit buiten `[25%, 75%]` van de totale portfoliowaarde komen) én een maximum op het percentage van de 75%-reserve dat mag worden aangesproken. Zodra deze grens geraakt wordt, stopt het meeschuiven naar die kant automatisch.
4. **Eerlijke Benchmarks (geen appels met peren):**
   * Vergelijk de Grid Bot continu met drie referentielijnen:
     * **Benchmark A — 50/50 Stil (Idle):** Dezelfde startvoorraad (`50% BTC` + `50% EUR`) niets laten doen.
     * **Benchmark B — 50/50 Periodiek Herbalanceren:** Dezelfde 50/50-verhouding periodiek (bijv. elke 24 uur) terugbrengen naar 50/50 zonder grid. **Alleen wat hierboven uitkomt is echte *Grid-Alpha*.**
     * **Referentie C — 100% BTC Buy & Hold:** Ter context ernaast tonen (om *cash drag* in een rechte bullrun eerlijk zichtbaar te maken).

---

## 3. WebSocket-First Architectuur (`watch_*` Functies)

Voor alles wat live of streaming gebeurt, gebruiken we **zoveel mogelijk de Bitvavo WebSocket via `ccxt.pro` (`asyncio`)**:
* **Live Paper-Trading (`paper_runner.py`):**
  * Draai parallelle `asyncio`-taken op `exchange.watch_order_book('BTC/EUR')` (voor realistische `best_bid`/`best_ask` en `postOnly`-validatie), `exchange.watch_trades('BTC/EUR')` (voor echte markttransacties die onze rustende limit orders kruisen) en `exchange.watch_ticker('BTC/EUR')`.
  * Geen REST-polling in de hoofdloop: REST wordt alleen bij opstart gebruikt voor `load_markets()`, fee-informatie en eventueel het aanvullen van historische candles.
* **WebSocket Hygiene & Resilience:**
  * Automatische reconnect met exponential backoff bij verbindingsverlies.
  * Staleness-bewaking op exchange-timestamps: markeer data als verouderd als de feed langer dan X seconden stilvalt en pauzeer nieuwe gesimuleerde fills totdat de stream weer vers is.
  * Graceful shutdown op `SIGINT`/`SIGTERM` (Ctrl+C), waarbij lopende sessies direct hun tussentijdse staat opslaan én het eind-dashboard genereren.
* **WebSocket Stream Recorder (`recorder.py`):**
  * Een modus waarmee live Bitvavo WebSocket-data (top-of-book orderbook snapshots/updates + publieke trades) weggeschreven wordt naar lokaal **Parquet** of **SQLite**, zodat de backtester zowel op historische REST-candles (`fetch_ohlcv` met lokale cache) als op **opgenomen WebSocket tick/orderbook-data** kan draaien.

---

## 4. Interactief Post-Run HTML Dashboard (`dashboard.py`)

Aan het eind van **elke** backtest-run én **elke** (afgesloten) paper-trading sessie genereert de bot automatisch een zelfstandig, strak vormgegeven **HTML-dashboard** in `reports/dashboard_<mode>_<timestamp>.html` (en print het pad in de terminal, met optionele `--open` flag om hem direct in de browser te openen).

Het dashboard (dark/quant-thema, responsive, met interactieve Plotly- of Lightweight-Charts grafieken) moet minimaal bevatten:
1. **KPI-Overzichtskaarten bovenaan:**
   * Startkapitaal vs. Eindwaarde Grid Bot (`€` en `%`)
   * **Grid-Alpha** ten opzichte van *50/50 Periodiek Herbalanceren* (`€` en `%`)
   * Groei van de **BTC-voorraad (%)** en **EUR-voorraad (%)** t.o.v. de start
   * Aantal afgeronde grid-cycli (`Buy` + `Sell` paren), totaal aantal fills, betaalde maker-fees (`€`), gemiddelde winst per cyclus, Sharpe Ratio en Maximale Drawdown (`%`).
2. **Grafiek 1 — Equity Curves (Grid Bot vs. 3 Benchmarks):**
   * Interactieve tijdlijn met 4 lijnen: **25/75 Grid Bot**, **50/50 Herbalanceren**, **50/50 Stil** en **100% BTC Buy & Hold**.
3. **Grafiek 2 — BTC/EUR Koersverloop + Grid Fills & Trailing Bandbreedte:**
   * Candlesticks of prijslijn met daarop groene driehoekjes voor elke **Buy Fill**, rode driehoekjes voor elke **Sell Fill**, en de boven- en ondergrens van het meeschuivende grid.
4. **Grafiek 3 — Realtime BTC & EUR Voorraadontwikkeling + Inventory Drift:**
   * Dubbele weergave van de BTC-voorraad en EUR-voorraad over tijd, inclusief de harde drift-limietlijnen (`25%` – `75%` BTC-weging).
5. **Grafiek 4 & Tabel — Drawdown Curve + Laatste Fills / Afgeronde Cycli:**
   * Onderwater-grafiek (*drawdown %*) en een doorzoekbare/overzichtelijke tabel met de uitgevoerde trades (tijdstip, zijde, prijs, BTC-hoeveelheid, EUR-waarde, fee, gerealiseerde cycluswinst).

---

## 5. Modulaire Projectstructuur

Splits de code schoon op in overzichtelijke modules met Python type hints, dataclasses/Pydantic-modellen en docstrings:

```text
bitvavo_grid_bot/
├── requirements.txt       # ccxt, pandas, numpy, plotly, pyarrow, pytest
├── config.py              # Dataclass met alle instellingen (kapitaal, 25/75 split, grid_step, orders_per_side, drift-limieten, fee-override)
├── exchange_ws.py         # ccxt.pro Bitvavo WebSocket manager (watch_order_book, watch_trades, watch_ticker) + REST market/fee loader & cache
├── recorder.py            # Live WebSocket tick/orderbook recorder naar lokaal Parquet/SQLite voor realistische replay
├── grid_engine.py         # Pure, deterministische grid-state-machine (volledig los van I/O en ccxt, 100% unit-testbaar)
├── benchmarks.py          # Berekening van 50/50 Stil, 50/50 Periodiek Herbalanceren en 100% BTC Hold
├── backtester.py          # Replay engine voor zowel historische Bitvavo candles als opgenomen WebSocket tick-data
├── paper_runner.py        # Live async paper-trading runner op de Bitvavo WebSocket stream (geen API-keys nodig)
├── dashboard.py           # Generator voor het interactieve self-contained HTML-rapport na afloop van elke run
├── main.py                # CLI (argparse/typer) met subcommands: `check-ccxt`, `fetch-data`, `record-ws`, `backtest`, `paper`
└── tests/
    └── test_grid_engine.py # Uitgebreide pytest-suite voor de grid-engine en guardrails