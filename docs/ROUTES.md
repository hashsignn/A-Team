# The five focus routes: where they start, which port, and how they get there

Sika's intercompany export says which country ships to which. It names the
supplying company by an internal code and never by place, and it has no
column for the port or the mode. So each of the three was chosen from public
sources, and the mode was chosen by price. Every choice below says where it
came from; none is confirmed by Sika.

| route | Sika flow | starts at | leaves by | reaches the port by |
|---|---|---|---|---|
| LANE_ASIA_08 | Switzerland → China | Düdingen | Rotterdam | truck to Basel, **Rhine barge** |
| LANE_US_01 | Switzerland → US | Düdingen | Antwerp | truck to Basel, **rail** (Schweizerzug) |
| LANE_MX_01 | Switzerland → Mexico | Düdingen | Antwerp | truck to Basel, **rail** (Schweizerzug) |
| LANE_IN_01 | Switzerland → India | Düdingen | Genoa | truck to Basel, **rail** (Frenkendorf–Genoa) |
| LANE_US_04 | Germany → US | Stuttgart | Rotterdam | **rail** from Kornwestheim |

## Where they start

- **Düdingen** — Sika Manufacturing AG: adhesives, membranes, joint tapes.
  Sika presents it as the plant delivering "to the world", with over
  40,000 t of adhesive and 16 million m² of membrane a year (2018).
  [Sika](https://www.sika.com/en/media/insights/sikanews/delivering-products-from-duedingen-to-the-world-for-over-50-year.html).
  The company's other site, Sarnen, is the alternative.
- **Stuttgart** — Sika Deutschland's head office, research centre and
  production site, next to the Kornwestheim rail terminal. Bad Urach is the
  other production site.
  [Sika Deutschland](https://deu.sika.com/de/ueber-sika/wie-sie-uns-finden/standorte.html).

## Which port

*"Swiss exports and imports usually transit through North Sea, secondarily
through Mediterranean ports."* The Swiss Rhine ports carried 10% of Swiss
imports and 5% of exports in 2019 (LAE white paper,
[preview](https://cuvillier.de/uploads/preview/public_file/12318/9783736973831-Leseprobe.pdf)).
So:
- the Swiss routes to China, the US and Mexico leave by the North Sea:
  Rotterdam, the Rhine's port, and Antwerp, which has a direct train to
  Basel;
- India leaves by Genoa, the Mediterranean gateway, which is the shorter
  sailing through Suez.

## How it reaches the port: priced

`engine/fast/precarriage.py` prices every chain with published figures, all
in `config.example/fast.yaml → precarriage`:

| input | value | source |
|---|---|---|
| operator cost per tonne-km | truck 0.117, rail 0.026, barge 0.019 CHF | ASNAV, Transportkostenvergleich Strasse–Schiene–Wasser (2021) |
| Swiss heavy-vehicle fee | 2.39 Rp per tonne-km (Euro 6) | [BAZG](https://www.bazg.admin.ch/de/lsva-berechnung) |
| German truck toll | 34.8 ct/km (Euro VI, 5+ axles) | [Toll Collect](https://www.toll-collect.de/de/toll_collect/bezahlen/maut_tarife/p1745_mauttarife_07_2024.html) |
| Italian motorway toll | €0.19034/km (class 5) | [Autostrade per l'Italia](https://www.autostrade.it/it/servizi-al-cliente/pedaggio/come-si-calcola-il-pedaggio) |
| road and rail distance | great circle × 1.46 | Ballou, Rahardja & Sakai (2002), *Transportation Research A* 36(9) |
| barge distance | Rhine kilometres | Basel–Rotterdam 870 km, Basel–Antwerp 900 km, Mannheim–Rotterdam 610 km |
| payload, transfer | 18 t per container, CHF 420 per change of mode | assumed |

The cheapest chain is drawn, unless another is within 5% of it and faster.
At this precision a 1% difference isn't a difference, and two days less on
the water is worth more. `scripts/simulate_precarriage.py` prints the table
below. `tests/test_precarriage.py` fails if a lane stops matching its winner.

As recorded on 26 September 2026, with the Rhine at Kaub restricting loading
to 15% of payload (barge freight ×2.2):

| origin → port | truck | truck + barge | truck + rail | drawn | at today's Kaub |
|---|---:|---:|---:|---|---|
| Düdingen → Rotterdam | CHF 2,165 | **CHF 1,098** | CHF 1,174 | barge | **rail**: barge now CHF 1,455 |
| Düdingen → Antwerp | CHF 1,900 | CHF 1,108 | **CHF 1,123** | rail: a tie, and 54 h faster | rail |
| Düdingen → Genoa | CHF 1,116 | — | **CHF 1,051** | rail | rail |
| Stuttgart → Rotterdam | CHF 1,696 | CHF 965 | **CHF 788** | rail | rail |

The first row is the point of the prototype. The cheapest way from
Switzerland to the North Sea is the Rhine, and the Rhine is what low water
takes away. At today's reading the same container goes cheaper by rail, and
the board's Rhine event recommends that switch.

The rail services each route depends on, from their operators' own pages:
- **Schweizerzug** (Swissterminal): Antwerp to Frenkendorf and Niederglatt.
- **Frenkendorf–Genoa-Pra'** (PSA Italy, Swissterminal): three a week,
  arriving the next day.
- **Kornwestheim–Rotterdam shuttle** (Hupac).

See `config.example/focus.yaml → operators` for the full list and sources.

## Not modelled

- Belgian and Dutch truck charges. Leaving them out makes the truck look
  slightly cheaper than it is, so it can only have lost by more.
- Terminal handling in the seaport. It's the same for every chain into the
  same port.
- Rail from a siding at the plant itself. None is published.
- The ocean leg's price. Ports were chosen by where Swiss freight goes, not
  by comparing ocean rates, which nobody publishes per lane.
