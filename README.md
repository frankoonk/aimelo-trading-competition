# Grid Bot — BTC/EUR op Bitvavo (backtest & paper-trading)

Een deterministische **spot grid trading bot** voor `BTC/EUR` op Bitvavo, met een webinterface. Je kunt ermee:

- **backtesten** op echte historische koersen,
- **paper-traden**: live meelopen met de echte Bitvavo-koers, met nepgeld,
- de live marktdata **opnemen** en daar later op backtesten,
- na elke run een **interactief rapport** bekijken.

> **Er worden nooit echte orders geplaatst.** De bot leest alleen publieke marktdata. Je hebt geen Bitvavo-account en geen API-sleutel nodig. Dit is een simulatie en geen beleggingsadvies.

---

## Inhoud

1. [Snel starten](#1-snel-starten)
2. [Installatie](#2-installatie)
3. [Starten en stoppen](#3-starten-en-stoppen)
4. [De webinterface](#4-de-webinterface)
5. [Hoe de bot werkt](#5-hoe-de-bot-werkt)
6. [Veiligheidsregels (guardrails)](#6-veiligheidsregels-guardrails)
7. [Instellingen](#7-instellingen)
8. [Het rapport lezen](#8-het-rapport-lezen)
9. [Hoe de simulatie rekent](#9-hoe-de-simulatie-rekent)
10. [Bestanden en mappen](#10-bestanden-en-mappen)
11. [Commandoregel (optioneel)](#11-commandoregel-optioneel)
12. [Tests](#12-tests)
13. [Problemen oplossen](#13-problemen-oplossen)
14. [Beperkingen](#14-beperkingen)
15. [Begrippenlijst](#15-begrippenlijst)

---

## 1. Snel starten

1. Zorg dat `uv` geïnstalleerd is (zie [Installatie](#2-installatie)). Dat is eenmalig.
2. Dubbelklik in Finder op **`start.command`**.
3. Er opent een Terminal-venster (laat dat open staan) en je browser opent op `http://localhost:8642`.
4. Klik op **Backtest → Start backtest**.

Stoppen: **Instellingen → Server stoppen** in de browser.

---

## 2. Installatie

### Wat je nodig hebt

- macOS of Linux (Windows: zie [zonder uv](#alternatief-zonder-uv)).
- Een internetverbinding (voor koersdata van Bitvavo).
- **`uv`**: een klein hulpprogramma dat Python en alle benodigde pakketten zelf regelt.

### uv installeren (eenmalig)

Met Homebrew:

```bash
brew install uv
```

Of met het officiële installatiescript:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Controleren of het gelukt is:

```bash
uv --version
```

### Verder hoef je niets te installeren

Er is **geen aparte virtuele omgeving (`venv`)** in de projectmap en je hoeft geen `pip install` te draaien. Bij het starten zorgt `uv` zelf voor Python 3.13 en de pakketten uit `requirements.txt`. Die worden in de eigen cache van `uv` bewaard, buiten de projectmap. De allereerste start duurt daardoor wat langer; daarna gaat het direct.

De gebruikte pakketten:

| Pakket | Waarvoor |
|---|---|
| `ccxt` (inclusief `ccxt.pro`) | alle communicatie met Bitvavo: REST en WebSocket |
| `aiohttp` | de lokale webserver van de interface |
| `pandas`, `numpy` | rekenwerk en tijdreeksen |
| `plotly` | de interactieve grafieken |
| `pyarrow` | opslag van koersdata en opnames (Parquet) |
| `pytest` | de tests |

### Alternatief zonder uv

Als je liever je eigen Python (3.11 of nieuwer) gebruikt:

```bash
pip install -r requirements.txt
```

```bash
python server.py
```

---

## 3. Starten en stoppen

### Starten

**Dubbelklikken:** `start.command` in Finder.

**Of vanuit een terminal**, vanuit de map `bitvavo_grid_bot`:

```bash
./start.command
```

De server kiest zelf de juiste map: `data/` en `reports/` komen altijd in de projectmap terecht, waar je hem ook vandaan start.

### Opties

| Optie | Betekenis |
|---|---|
| `--port 9000` | gebruik een andere poort dan de standaard `8642` |
| `--no-browser` | open de browser niet automatisch |

Voorbeeld:

```bash
./start.command --port 9000 --no-browser
```

### Stoppen

- In de browser: **Instellingen → Server stoppen**, of
- in het Terminal-venster: `Ctrl+C`.

In beide gevallen wordt een lopende paper-sessie eerst netjes afgesloten en krijgt die nog een rapport. Een lopende opname wordt afgesloten en bewaard.

### Als de server al draait

Start je `start.command` een tweede keer, dan meldt hij dat de poort al in gebruik is en opent hij alleen de browser op de bestaande server.

### Veiligheid

De server luistert alleen op `127.0.0.1`: hij is bereikbaar vanaf je eigen computer en niet vanaf het netwerk. Wijzigende verzoeken vanaf andere websites worden geweigerd.

---

## 4. De webinterface

Links staat het menu. Een groen stipje naast een menu-item betekent dat daar iets loopt.

### Overzicht

Korte uitleg, de status van de verbinding met Bitvavo, de handelskosten die gebruikt worden, het bedrag per order en het resultaat van het laatste rapport in één zin.

### Backtest

Speelt een periode uit het verleden na.

1. Kies een **periode**: 1 week, 1 maand, 3 maanden, 6 maanden of 1 jaar (gerekend terug vanaf nu).
2. Kies de **nauwkeurigheid**: koers per minuut (aanbevolen), per 5 minuten of per uur.
3. Klik op **Start backtest**.

De koersdata wordt automatisch opgehaald en lokaal bewaard. Alleen wat nog ontbreekt wordt gedownload, dus een tweede keer gaat veel sneller. Eén jaar per minuut is bij de eerste keer enkele honderden verzoeken en duurt een paar minuten.

Na afloop zie je een samenvatting en de knop **Bekijk volledig rapport**.

### Paper-trading

Laat de bot live meelopen met de echte koers.

- **Start paper-trading** begint met het ingestelde startkapitaal: de helft in bitcoin, de helft in euro's, tegen de koers van dat moment.
- Tijdens het lopen zie je: de koers, de waarde van de portefeuille, het verschil met herbalanceren, het aantal transacties, de verhouding bitcoin/euro, het actieve raster, een live grafiek, de openstaande orders en de laatste transacties.
- **Stop en maak rapport** sluit de sessie af en maakt het rapport.

De server moet aan blijven staan zolang de sessie loopt; het browsertabblad mag je sluiten en later weer openen. Reken op uren tot dagen voordat er genoeg transacties zijn om iets van het resultaat te vinden: de koers moet eerst minstens één rasterstap bewegen voor er iets gebeurt.

Het label **Live verbonden** wordt geel als de datastroom hapert. Zolang dat zo is worden er geen transacties gesimuleerd (zie [guardrails](#6-veiligheidsregels-guardrails)).

### Opnemen

Optioneel. Neemt de live marktdata op: elke wijziging van de beste bied- en laatprijs, plus elke publieke transactie. Kies een duur (10 minuten tot 24 uur, of "tot ik stop").

Bij elke opname in de lijst staat de knop **Backtest hierop**. Zo'n backtest is nauwkeuriger dan een backtest op minuutkoersen, omdat hij precies dezelfde rekenregels gebruikt als paper-trading, inclusief het echte orderboek. Hij kan alleen de periode beslaan die je zelf hebt opgenomen.

### Rapporten

Alle eerdere runs, nieuwste bovenaan, met per rapport de periode, het resultaat van de bot, het verschil met herbalanceren en het aantal rondes. Klik op een regel om het rapport te openen; **Open in nieuw tabblad** toont het los van de interface. Met **Verwijder** gooi je een rapport definitief weg.

### Instellingen

Alle knoppen van de strategie (zie [Instellingen](#7-instellingen)), een **verbindingstest** met Bitvavo en de knop **Server stoppen**.

Instellingen gelden voor de *volgende* backtest of paper-sessie. Een sessie die al loopt, houdt de instellingen waarmee hij gestart is.

---

## 5. Hoe de bot werkt

### Het idee

De bot voorspelt de koers niet. Hij verdient aan *schommelingen*: hij koopt een klein beetje als de koers een stap daalt en verkoopt dat weer als de koers een stap stijgt. Elke afgeronde koop-en-verkoop heet een **ronde** (cyclus) en levert een kleine winst op. Dit principe heet *volatility harvesting*.

### De verdeling van het kapitaal

Met de standaardinstellingen en € 10.000:

| Deel | Bedrag | Wat het doet |
|---|---|---|
| Start | 50% bitcoin, 50% euro | de beginvoorraad |
| **Actief raster (25%)** | € 2.500 | staat als orders klaar: 10 kooporders onder de koers en 10 verkooporders erboven, elk € 125 |
| **Reserve (75%)** | € 7.500 | staat stil, half bitcoin en half euro, zodat het raster kan meeschuiven |

### Het raster

De orders liggen op vaste niveaus, steeds een vast percentage uit elkaar (standaard 0,5%). Het raster beslaat `2N + 1` opeenvolgende niveaus: `N` kooporders, één leeg niveau (waar de laatste transactie was) en `N` verkooporders.

### Omklappen (cycle flip)

- Wordt een **kooporder** uitgevoerd, dan legt de bot direct één niveau **hoger** een verkooporder.
- Wordt een **verkooporder** uitgevoerd, dan legt de bot direct één niveau **lager** een kooporder.

Zo staat er na elke transactie meteen een order klaar die de ronde afmaakt.

### Winstverdeling

De verkooporder van een ronde is iets kleiner dan de bijbehorende kooporder. Daardoor blijft een deel van de winst als bitcoin achter en een deel als euro's. Standaard is dat 50/50, zodat per afgeronde ronde **beide voorraden groeien**, na aftrek van de kosten.

Let op: dat geldt per ronde. Over een langere dalende periode koopt de bot per saldo bitcoin bij (meer bitcoin, minder euro's); in een stijgende periode andersom.

### Meeschuiven (trailing)

Loopt de koers tot voorbij de buitenste order van het raster, dan schuift het hele raster één niveau mee:

- bij een **daling**: er komt een nieuwe kooporder onder het raster (betaald uit de euro-reserve) en de verste verkooporder vervalt;
- bij een **stijging**: er komt een nieuwe verkooporder boven het raster (uit de bitcoin-reserve) en de verste kooporder vervalt.

Het meeschuiven stopt als een veiligheidsgrens bereikt is. Het raster blijft dan staan tot de koers terugkomt.

### Sprongen over meerdere niveaus

Beweegt de koers in één keer door meerdere niveaus, dan worden die netjes in prijsvolgorde afgehandeld: geen dubbele transacties en nooit een negatief saldo.

---

## 6. Veiligheidsregels (guardrails)

Deze regels zijn vast ingebouwd en werken altijd, in backtest én paper-trading.

### 1. Kostenvloer voor de rasterstap

Een ronde moet meer opleveren dan de twee keer handelskosten die hij kost. De kleinste toegestane stap is:

```
kleinste stap = max(2 × handelskosten + veiligheidsmarge, ondergrens)
```

Met de standaardwaarden (marge 0,05%, ondergrens 0,15%):

| Handelskosten (maker) | Kleinste stap |
|---|---|
| 0,00% | 0,15% |
| 0,15% (standaard Bitvavo) | 0,35% |
| 0,25% | 0,55% |

Stel je een kleinere stap in, dan wordt die automatisch opgetild naar deze vloer. De handelskosten worden bij Bitvavo opgevraagd en nooit vast in de code gezet.

### 2. Alleen "maker"-orders (postOnly)

Alle orders zijn limietorders die in het orderboek *wachten*. De bot legt nooit een kooporder op of boven de beste laatprijs, en nooit een verkooporder op of onder de beste biedprijs. Zo'n order zou in het echt direct uitgevoerd worden tegen het duurdere "taker"-tarief. Kan een order op dat moment niet geplaatst worden, dan wacht de bot tot het wel kan.

### 3. Grens op de verhouding bitcoin/euro

Het aandeel bitcoin in de totale portefeuille moet tussen een minimum en een maximum blijven (standaard **25% – 75%**). De bot plaatst geen order, en haalt een wachtende order weg, als uitvoering daarvan de grens zou doorbreken. Dat geldt voor *alle* orders, niet alleen bij het meeschuiven.

Daarnaast mag het meeschuiven hooguit een ingesteld deel van de reserve gebruiken (standaard **80%**).

Samen voorkomen deze regels dat de bot in een langdurige daling al zijn euro's opkoopt, of in een langdurige stijging al zijn bitcoin verkoopt.

Kanttekening: de koers zelf kan de verhouding ook verschuiven zonder dat de bot iets doet. De regel garandeert dat geen *order* de grens doorbreekt.

### 4. Eerlijke vergelijking

Elk resultaat wordt naast drie alternatieven gezet, die met exact hetzelfde startkapitaal op exact dezelfde koers beginnen:

| Vergelijking | Wat het is |
|---|---|
| **50/50 Stil** | dezelfde beginvoorraad, niets doen |
| **50/50 Herbalanceren** | elke 24 uur terug naar 50/50, zonder raster |
| **100% BTC Buy & Hold** | alles in bitcoin |

Herbalanceren profiteert zelf ook al van schommelingen. Daarom telt alleen wat de bot **boven herbalanceren** verdient als echte meerwaarde van het raster. Dat getal heet in het rapport *Grid-Alpha* en in de interface *Extra t.o.v. herbalanceren*.

### 5. Bewaking van de live datastroom

Tijdens paper-trading en opnemen:

- **Opnieuw verbinden:** valt de verbinding weg, dan probeert de bot het opnieuw met steeds langere wachttijd (1, 2, 4 … tot maximaal 60 seconden).
- **Verouderde data:** komt er 10 seconden geen update van het orderboek, of lopen de tijdstempels van Bitvavo ver achter, dan geldt de datastroom als verouderd. Er worden dan geen transacties gesimuleerd tot hij weer vers is.
- **Netjes afsluiten:** bij stoppen wordt de staat opgeslagen en het rapport gemaakt. Tijdens een sessie wordt de staat elke 30 seconden weggeschreven naar `data/paper_state_<tijdstip>.json`.

---

## 7. Instellingen

Alle instellingen staan op de pagina **Instellingen** en worden bewaard in `data/settings.json`.

| Instelling | Standaard | Uitleg |
|---|---|---|
| Startkapitaal | € 10.000 | het nepgeld waarmee elke run begint |
| Orders per kant | 10 | aantal kooporders onder en verkooporders boven de koers |
| Deel dat in orders staat | 25% | de rest is reserve |
| Afstand tussen orders | 0,5% | de rasterstap; wordt opgetild naar de kostenvloer |
| Handelskosten | automatisch | leeg = tarief van Bitvavo; vul bijvoorbeeld `0` in voor een 0%-account |
| Winst bewaren als bitcoin | 50% | de rest van de winst per ronde blijft in euro's |
| Minimaal aandeel bitcoin | 25% | ondergrens van de verhouding |
| Maximaal aandeel bitcoin | 75% | bovengrens van de verhouding |
| Maximaal gebruik van de reserve | 80% | hoeveel reserve het meeschuiven mag aanspreken |
| Vergelijking: herbalanceer elke | 24 uur | alleen voor de vergelijkingslijn |
| Soepel vulmodel | uit | zie [Hoe de simulatie rekent](#9-hoe-de-simulatie-rekent) |

Onder het formulier staat wat de opgeslagen instellingen betekenen: het bedrag per order, de handelskosten, de kleinste zinvolle stap en de stap die werkelijk gebruikt wordt.

**Bedrag per order** = startkapitaal × deel in orders ÷ (2 × orders per kant). Dat moet boven het Bitvavo-minimum van € 5 liggen; anders krijg je een waarschuwing en start een run niet.

### Handelskosten: waar komt het tarief vandaan?

In deze volgorde:

1. wat je zelf invult bij **Handelskosten**;
2. je persoonlijke tarief via de Bitvavo-API, als de omgevingsvariabelen `BITVAVO_API_KEY` en `BITVAVO_API_SECRET` gezet zijn (alleen-lezen gebruik, er wordt niets verhandeld);
3. het standaardtarief dat Bitvavo publiek opgeeft (nu 0,15% maker).

Zonder API-sleutel kan de bot je persoonlijke tarief niet zien. Betaal je minder dan het standaardtarief, vul het dan zelf in.

---

## 8. Het rapport lezen

Elke run levert één HTML-bestand op in `reports/`. Het is zelfstandig: je kunt het los openen, bewaren of doorsturen, ook zonder internet en zonder dat de server draait.

### Kaarten bovenaan

| Kaart | Betekenis |
|---|---|
| Eindwaarde Grid Bot | waarde aan het eind, met winst of verlies in € en % |
| Grid-Alpha | verschil met 50/50 herbalanceren — het eerlijke getal |
| BTC-voorraad / EUR-voorraad | groei of krimp van elke voorraad sinds de start |
| Afgeronde grid-cycli | aantal complete koop-en-verkoop rondes, plus het totaal aantal transacties |
| Gem. winst per cyclus | gemiddelde nettowinst van een ronde |
| Betaalde maker-fees | totale handelskosten |
| Sharpe ratio | rendement afgezet tegen de schommelingen ervan (hoger is beter) |
| Maximale drawdown | de grootste daling vanaf een eerder hoogste punt |
| BTC-weging nu | het huidige aandeel bitcoin en het bereik tijdens de run |

### Grafieken

1. **Equity curves** — de waarde van de bot en van de drie vergelijkingen over de tijd, met daaronder een tabel met eindwaarde, rendement, drawdown en Sharpe per lijn.
2. **Koers, transacties en raster** — de koers met ▲ voor elke aankoop, ▼ voor elke verkoop, en de boven- en ondergrens van het raster (paars). Hier zie je het meeschuiven.
3. **Voorraad en verhouding** — drie panelen onder elkaar: de bitcoin-voorraad, de euro-voorraad en het aandeel bitcoin met de twee grenslijnen (geel).
4. **Drawdown** — hoe ver de waarde op elk moment onder de eerdere top stond.

Je kunt in elke grafiek inzoomen door te slepen; dubbelklik om terug te gaan.

### Tabellen

- **Trailing en guardrails** — elke keer dat het raster verschoof en elk moment dat een veiligheidsgrens het meeschuiven stopte of weer vrijgaf.
- **Fills en afgeronde cycli** — alle transacties met tijd, soort, prijs, hoeveelheid, waarde, kosten en (bij een afgeronde ronde) de winst. Doorzoekbaar, met filters voor aankopen, verkopen en alleen afgeronde rondes. Bij meer dan 5.000 transacties worden de laatste 5.000 getoond.
- **Instellingen van deze run** — waarmee dit rapport gemaakt is.

Alle tijden in het rapport zijn in UTC.

---

## 9. Hoe de simulatie rekent

Het is goed om te weten welke aannames er onder de cijfers liggen.

### Wanneer telt een order als uitgevoerd?

- **Standaard (voorzichtig):** een order telt pas als de koers er echt *doorheen* gaat. Een kooporder op € 57.000 is uitgevoerd als er lager dan € 57.000 gehandeld wordt.
- **Soepel vulmodel (aan):** ook als de koers de orderprijs precies *raakt*. Dat is optimistischer, omdat je in het echt achteraan in de rij kunt staan.

Uitvoering gebeurt altijd tegen de eigen limietprijs van de order.

### Backtest op koersdata (candles)

Een candle geeft per periode de open-, hoogste, laagste en slotkoers, maar niet de volgorde daartussen. De bot neemt aan:

- stijgende candle: open → laag → hoog → slot;
- dalende candle: open → hoog → laag → slot.

Dat is de gebruikelijke aanname. Hoe korter de candles, hoe minder ze uitmaakt; daarom is "per minuut" de aanbevolen nauwkeurigheid.

### Backtest op een opname en paper-trading

Deze twee gebruiken exact dezelfde rekenregels:

- een **publieke transactie** die door de prijs van een wachtende order heen gaat, voert die order uit;
- een **orderboek-update** waarbij de beste laatprijs op of onder een wachtende kooporder komt (of de beste biedprijs op of boven een verkooporder), voert die order ook uit;
- orders die als reactie op een transactie geplaatst worden, kunnen niet door diezelfde transactie uitgevoerd worden.

### Kosten

Handelskosten worden bij elke transactie in euro's gerekend, zoals Bitvavo dat doet: een aankoop kost het bedrag plus kosten, een verkoop levert het bedrag min kosten op. De vergelijking "herbalanceren" betaalt hetzelfde tarief over wat zij verhandelt.

### Deterministisch

De bot bevat geen toeval. Dezelfde data met dezelfde instellingen geeft altijd exact hetzelfde resultaat. Een backtest "vanaf nu terug" verschuift wel met de tijd: een paar minuten later starten geeft een iets andere startkoers en dus een iets ander raster.

---

## 10. Bestanden en mappen

```text
bitvavo_grid_bot/
├── start.command          Starter: dubbelklik om de server te starten
├── server.py              Lokale webserver en API achter de interface
├── gui.html               De webinterface (één pagina)
├── main.py                Commandoregel-variant (optioneel)
│
├── config.py              Alle instellingen en de marktregels (tick, minimum, fees)
├── grid_engine.py         De strategie zelf: pure, deterministische rekenkern
├── benchmarks.py          De drie vergelijkingen
├── backtester.py          Naspelen van candles en van opnames
├── exchange_ws.py         Bitvavo via ccxt: REST (marktregels, fee, candles) en WebSocket
├── paper_runner.py        Live paper-trading
├── recorder.py            Opnemen van de live datastroom
├── results.py             Resultaat van een run en alle kerncijfers
├── dashboard.py           Maakt het HTML-rapport
│
├── requirements.txt       Benodigde pakketten
├── conftest.py            Hulpbestand voor de tests
├── tests/                 Geautomatiseerde tests
│
├── data/                  Wordt automatisch aangemaakt
│   ├── settings.json            je instellingen
│   ├── market_BTC-EUR.json      bewaarde marktregels van Bitvavo
│   ├── ohlcv_BTC-EUR_1m.parquet bewaarde koersdata (per nauwkeurigheid één bestand)
│   ├── paper_state_*.json       tussentijdse staat van een paper-sessie
│   └── recordings/              je opnames
│
└── reports/               Wordt automatisch aangemaakt
    ├── dashboard_<soort>_<tijdstip>.html   de rapporten
    └── index.json                          kerncijfers voor de rapportenlijst
```

### Wat kun je veilig weggooien?

- `data/ohlcv_*.parquet` en `data/market_*.json`: worden opnieuw opgehaald.
- `data/paper_state_*.json`: alleen een tussentijdse momentopname.
- `reports/` en `data/recordings/`: je eigen resultaten; weg is weg.
- `data/settings.json`: de bot valt dan terug op de standaardwaarden.

### Hoe de onderdelen samenhangen

`grid_engine.py` is de kern en weet niets van internet, bestanden of de klok. Backtester en paper-runner voeren hem marktdata; daardoor gedraagt de strategie zich in een backtest exact zoals live. `exchange_ws.py` is de enige plek die met Bitvavo praat, en doet dat volledig via `ccxt` / `ccxt.pro` (geen eigen WebSocket-code).

---

## 11. Commandoregel (optioneel)

Alles kan via de interface. Wie liever commando's gebruikt, kan `main.py` aanroepen. Vanuit de map `bitvavo_grid_bot`:

```bash
uv run --no-project --python 3.13 --with-requirements requirements.txt python main.py --help
```

Om dat korter te maken kun je in je terminal een afkorting zetten:

```bash
alias gridbot='uv run --no-project --python 3.13 --with-requirements requirements.txt python main.py'
```

De voorbeelden hieronder gebruiken die afkorting.

| Commando | Wat het doet |
|---|---|
| `gridbot check-ccxt` | controleert wat `ccxt` voor Bitvavo ondersteunt en test de live verbinding |
| `gridbot fetch-data --days 90` | haalt koersdata op en bewaart die lokaal |
| `gridbot backtest --days 90 --open` | backtest op koersdata en opent het rapport |
| `gridbot backtest --ticks data/recordings/<bestand>` | backtest op een opname |
| `gridbot record-ws --duration 3600` | neemt een uur lang de live datastroom op |
| `gridbot paper --open` | live paper-trading; `Ctrl+C` stopt en maakt het rapport |

Veelgebruikte opties bij `backtest` en `paper`:

| Optie | Betekenis |
|---|---|
| `--capital 10000` | startkapitaal in euro's |
| `--orders-per-side 10` | orders per kant |
| `--active-fraction 0.25` | deel in het orderboek |
| `--step 0.005` | rasterstap als fractie (0.005 = 0,5%) |
| `--maker-fee 0` | handelskosten als fractie |
| `--profit-split-btc 0.5` | deel van de winst in bitcoin |
| `--min-btc-weight 0.25` / `--max-btc-weight 0.75` | grenzen van de verhouding |
| `--max-reserve-usage 0.8` | maximaal reservegebruik |
| `--rebalance-hours 24` | interval van de herbalanceer-vergelijking |
| `--fill-on-touch` | soepel vulmodel |
| `--open` | open het rapport direct in de browser |
| `--cdn` | kleiner rapportbestand; grafieken vereisen dan internet |

Alleen bij `backtest`: `--start 2026-01-01`, `--end 2026-03-01`, `--timeframe 1m` en `--offline` (alleen lokale data gebruiken). Alleen bij `record-ws`: `--format sqlite|parquet` en `--out <pad>`.

Let op: de commandoregel gebruikt zijn eigen opties en leest `data/settings.json` van de interface **niet**.

---

## 12. Tests

De rekenkern en de veiligheidsregels zijn afgedekt met 47 geautomatiseerde tests. Draaien, vanuit de map `bitvavo_grid_bot`:

```bash
uv run --no-project --python 3.13 --with-requirements requirements.txt python -m pytest -q
```

Wat er getest wordt:

- de kostenvloer en het optillen van een te kleine stap;
- de beginverdeling (50/50, 25% actief, 75% reserve) en afronding op de Bitvavo-regels;
- omklappen na een aankoop en na een verkoop;
- dat een afgeronde ronde beide voorraden laat groeien, bij 0%, 0,15% en 0,25% kosten;
- sprongen over meerdere niveaus, zonder dubbele transacties of negatief saldo;
- meeschuiven omhoog en omlaag;
- de grens op de verhouding en op het reservegebruik, bij een langdurige daling en stijging;
- de postOnly-regel;
- determinisme en sluitende boekhouding over willekeurige koersreeksen;
- de vergelijkingen, beide backtest-vormen en het aanmaken van het rapport.

De tests hebben geen internet nodig.

---

## 13. Problemen oplossen

**"uv is niet gevonden" bij het starten**
Installeer `uv` (zie [Installatie](#2-installatie)) en start opnieuw. Open eventueel een nieuw Terminal-venster, zodat de installatie gevonden wordt.

**macOS weigert `start.command` te openen**
Rechtsklik op het bestand en kies **Open**, en bevestig. Dat hoeft maar één keer.

**"Permission denied" bij `./start.command`**
Het bestand is niet meer uitvoerbaar. Herstel dat met:

```bash
chmod +x start.command
```

**"Poort 8642 is al in gebruik"**
De server draait waarschijnlijk al; de browser wordt daarop geopend. Wil je een tweede, start dan met `--port 9000`.

**De pagina zegt "server niet bereikbaar"**
Het Terminal-venster is gesloten of de server is gestopt. Start `start.command` opnieuw en ververs de pagina.

**Overzicht toont "Geen verbinding" met Bitvavo**
Controleer je internetverbinding en gebruik **Instellingen → Test verbinding**. De interface heeft voor een backtest altijd verbinding nodig, ook als de koersdata al lokaal bewaard is. Zonder internet kan het alleen via de commandoregel: `gridbot backtest --offline`.

**Een run start niet: "order … is below the exchange minimum"**
Het bedrag per order is kleiner dan € 5. Verhoog het startkapitaal, verhoog het deel dat in orders staat, of verlaag het aantal orders per kant.

**Paper-trading doet niets**
Dat is meestal normaal. Er gebeurt pas iets als de koers één rasterstap beweegt (bij 0,5% en een koers rond € 57.000 is dat ongeveer € 285). Op een rustig moment kan dat lang duren.

**Paper-trading meldt "Feed hapert"**
De datastroom is tijdelijk verouderd; de bot verbindt zelf opnieuw en gaat daarna verder. Blijft het zo, controleer dan je internetverbinding.

**De eerste backtest over een lange periode duurt lang**
De koersdata wordt dan voor het eerst gedownload. De voortgang staat in beeld. Daarna is het bewaard.

**Twee backtests met dezelfde instellingen geven iets andere cijfers**
De periode loopt "vanaf nu terug" en schuift dus mee met de klok. Zie [Deterministisch](#deterministisch).

---

## 14. Beperkingen

- **Geen echte handel.** De bot kan geen orders plaatsen. Dat is een bewuste keuze.
- **Eén markt.** Alles is ingericht op `BTC/EUR` bij Bitvavo.
- **Een sessie hervatten kan niet.** Stopt de server tijdens paper-trading, dan begint een volgende sessie opnieuw. Het opgeslagen staatsbestand is alleen ter inzage.
- **Geen wachtrij-simulatie.** De bot weet niet op welke plek in de rij een order zou staan. Het voorzichtige vulmodel vangt dat grotendeels op.
- **Sprongen voorbij het raster.** Springt de koers in één klap voorbij het volgende niveau buiten het raster, of heeft een veiligheidsgrens het meeschuiven gestopt, dan blijft het raster staan tot de koers terugkomt. De bot haalt de koers dan niet in.
- **De verhouding kan door de koers alleen verschuiven.** De grens van 25–75% geldt voor wat orders doen.
- **Resultaten uit het verleden** zeggen niets over de toekomst. In de eerste backtest over drie maanden met 0,15% kosten bleef de bot net onder simpel dagelijks herbalanceren.

---

## 15. Begrippenlijst

| Begrip | Betekenis |
|---|---|
| **Spot** | je koopt en verkoopt de bitcoin zelf, zonder geleend geld |
| **Limietorder** | een order met een vaste prijs, die wacht tot de markt daar komt |
| **Maker / taker** | een *maker* legt een wachtende order in het boek (goedkoper); een *taker* handelt direct tegen een bestaande order (duurder) |
| **postOnly** | een order die alleen als maker geplaatst mag worden |
| **Orderboek** | de lijst met alle wachtende koop- en verkooporders |
| **Bied / laat (bid / ask)** | de hoogste prijs die een koper biedt en de laagste prijs die een verkoper vraagt |
| **Raster (grid)** | de reeks prijsniveaus waarop de bot orders legt |
| **Rasterstap** | de afstand tussen twee niveaus, in procenten |
| **Fill** | een uitgevoerde order |
| **Ronde (cyclus)** | een aankoop plus de bijbehorende verkoop één niveau hoger |
| **Meeschuiven (trailing)** | het raster verplaatst zich met de koers mee |
| **Reserve** | het deel van het kapitaal dat niet in orders staat |
| **Verhouding (inventory drift)** | het aandeel bitcoin in de totale waarde, en hoe dat verschuift |
| **Herbalanceren** | periodiek terug naar de beginverhouding van 50/50 |
| **Grid-Alpha** | wat de bot meer of minder opleverde dan herbalanceren |
| **Drawdown** | de daling vanaf een eerder hoogste punt |
| **Sharpe ratio** | rendement gedeeld door de schommeling ervan, op jaarbasis |
| **Candle** | de open-, hoogste, laagste en slotkoers van één tijdvak |
| **Tick** | één afzonderlijke marktgebeurtenis: een transactie of een orderboek-wijziging |
| **Backtest** | een strategie naspelen op gegevens uit het verleden |
| **Paper-trading** | live meelopen met echte koersen, met nepgeld |
| **WebSocket** | een doorlopende verbinding waarover Bitvavo live updates stuurt |
| **UTC** | de wereldtijd die in de rapporten gebruikt wordt (Nederland is 1 of 2 uur later) |
