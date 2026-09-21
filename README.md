# Supply Chain Risk Radar

> A supply chain planner at Sika is tracking 50–200 active shipments across
> road, rail and sea. The world produces thousands of external events a day.
> Almost none of them matter. **Tell the planner which ones do, early enough to
> act, and what to do about it.**

Built for the Sika Innovathon 2026 — *"How can supply chain planners identify
and act on external risk before it hits their delivery reliability?"*

### Run it on your laptop

```bash
git clone https://github.com/hashsignn/A-Team.git
cd A-Team

python3 -m venv .venv                       # Python 3.11+
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt   # ~6 packages, no toolchain

.venv/bin/python run.py serve               # -> http://localhost:8000
```

Windows: use `.venv\Scripts\python` and `.venv\Scripts\pip` instead.

### Run it on GitHub Codespaces

The repo ships a devcontainer, so there is nothing to install by hand:

1. **Code → Codespaces → Create codespace on this branch**
2. Wait for the container to build (it creates `.venv` and installs for you)
3. In the terminal: `.venv/bin/python run.py serve --host 0.0.0.0`
4. Click the forwarded **port 8000** notification, or open the **Ports** tab

`--host 0.0.0.0` matters in Codespaces — bound to `127.0.0.1` the port is not
reachable from the forwarded URL.

### Everything else

```bash
.venv/bin/python run.py demo          # the whole pipeline, in the terminal
.venv/bin/python run.py inputs        # what is real / standing in / absent
.venv/bin/python -m pytest -q         # 78 tests

# any instant you like — the board is reproducible from it
.venv/bin/python run.py demo --as-of 2026-09-19T12:00:00+00:00
```

The same instant works in the URL, and is where the Critical rung shows up:

```
http://localhost:8000/?as_of=2026-09-19T12:00:00%2B00:00
```

Everything runs **offline**. No API key, no network, no model required.

---

## What this is

Nine pipeline stages behind a four-pane dashboard:

```
1. ingest     shipments + gauges + feeds
2. resolve    many reports -> one event
3. gate       does it touch our freight at all?   <- ~95% die here
4. variables  which of the 45 are active
5. reason     extraction + relevance, with evidence
6. simulate   delay distribution -> P(late)
7. score      CHF, lead time, matrix cell
8. act        options, contacts, decision deadline
9. export     store, CSV, per-event report
```

Stages 1–8 need no LLM. The Rhine demo runs end to end on numeric feeds, so if
the model is unavailable on the day the board still fills.

### The output is a sentence, not a score

> *"Switch barge leg to rail: costs CHF 2,361, avoids CHF 26,484 of expected
> loss. Net benefit CHF 24,123. Decide by Fri 25 Sep 13:43 UTC (7 days)."*

---

## The three failures the client named, and where each is answered

| Their words | Where |
|---|---|
| *"No filter for what is relevant on which freight lane"* | `engine/gate/` — spatial ∧ temporal ∧ modal, **zero parameters** |
| *"Disruptions are taken serious when carriers call — response options have already narrowed"* | `engine/score/severity.py` — every rung of the ladder is a deadline; `engine/portfolio/convene.py` — the pre-agreed convene rule |
| *"Even when a crisis is identified, it is unclear what to do and who to involve"* | `engine/act/playbook.py` — options, owners, named contacts, deadline |

---

## The five-level ladder

The client's scale:

| | Level | Meaning |
|---|---|---|
| 🟢 | **Normal** | No action required |
| ⚪ | **Bias** | Monitor closely — watch for changes |
| 🔵 | **Watch** | Determine action within 3–7 days |
| 🟡 | **Alert** | Take action within 24–48 hours |
| 🔴 | **Critical** | Take action within 6 hours |

**Every rung is a deadline, not a damage band.** So the level is not "how bad
is this" — it is "how soon must somebody decide", which is exactly
`lead_time_hours`, already computed from
`decision_deadline = impact_time − action_duration`. No new machinery: the
ladder is a relabelling of a quantity that was already there.

The hour cutoffs are the client's own numbers. Two judgement calls are ours
and are marked `ASSUMED` in `config.example/scoring.yaml`: the CHF floor below
which a touched route is Bias rather than Watch, and the ordering *within* a
level. Ranking is lexicographic — level first, then exposure — so a Bias route
can never outrank a Watch one however much money is on it. Blending the two
into a weighted score would allow exactly that.

The whole classification is one function, `engine/score/severity.py::classify`.
**When the real severity formula arrives it replaces that function and nothing
else moves.**

---

## What Sika's answers changed

Six questions went to Sika. Four of the answers changed the build.

**Q1 — no risk ledger exists.** So `config.example/variables.yaml` *is* the
proposed ledger, written to be read and argued with by the planning team.
**45 fully specified variables, not 100 half-specified ones** — §5.0's claim
that every parameter is "something a planner already knows" only survives if
every entry is genuinely specified.

**Q3 — economics/logistics backgrounds; can read a matrix and play with the
assumptions; will not read the compute.** So every number has a plain-language
form, the formula sits behind a *"show the arithmetic"* toggle, and the
assumption table is **editable live in the sidebar**. That turns the weakest
point in the design — numbers we invented — into the most engaging moment in
the demo: hand them the laptop and let them set *"Antwerp strike = 4 days, not
3"*.

**Q5 — factor in social media.** Tier-3 sources are carried but cannot trip the
convene rule until corroborated. The point worth making out loud: social media
is not a *better* signal, it is an *earlier* one. Its whole value is arriving
while there is still time to act — a lead-time argument, so it belongs on the
ladder rather than in the accuracy story.

**Q6 — the answer that reframed the product.**

> *"In crisis, established teams that meet on a weekly schedule (Procurement,
> Manufacturing, Supply Chain, Controlling) increase meeting frequency. They
> have full authority to decide on mitigation. **The real problem is that we
> declare a crisis too late and lose on available options.**"*

Authority is not the bottleneck. Analysis is not the bottleneck. **The decision
to convene is.** So the board gained a portfolio state strip and a convene rule
the group owns — see below.

---

## The convene rule

Sika's answer to Q6 is the one that reframed the product:

> *"In crisis, established teams that meet on a weekly schedule (e.g.
> Procurement, Manufacturing, Supply Chain, Controlling) increase meeting
> frequency e.g. from weekly to every day. They have full authority to decide
> on mitigation. **The real problem is that we declare a crisis too late and
> lose on available options.**"*

Authority is not the bottleneck. Analysis is not the bottleneck. **The decision
to convene is.** So the portfolio layer answers exactly one question: has a
threshold the group pre-agreed in calm conditions been crossed?

Three triggers, in `config.example/scoring.yaml`:

| Trigger | What it counts |
|---|---|
| `exposure_chf` | expected loss across the book, out of the Monte Carlo |
| `contracts_exposed` | distinct customers with expected loss above zero |
| `shipments_needing_decision` | shipments inside 48 h of their decision deadline |

Every one is something a planner can check. Nothing rests on the summed value
of acting, which is built on our invented action costs and is not a number
anyone can stand behind.

### What was removed, and why

There was briefly a fourth quantity: the CHF value of mitigations lapsing
before the next meeting, plotted as a decay curve, with the convene rule keyed
to it. It is gone.

The framing was borrowed from finance and does not belong in a freight
planner's hands. A planner has a cutoff to get a container to the terminal, or
a last train path to book — not an option with a strike price and a decay rate.
Dressing a booking deadline up as optionality makes the tool sound like it is
trading the freight rather than moving it.

The timing dimension it was reaching for is already carried, properly, by the
five-level ladder: **every rung IS a deadline**. Nothing was lost but the
jargon. Removing it also took the last display of `recoverable_chf` off the
board, which is the right outcome — it was the least defensible arithmetic in
the system and it had been sitting in the convene headline.

### The part that changes behaviour is not technical

Teams do not declare late because they lack a number. They declare late because
declaring is socially expensive — somebody has to stick their neck out and risk
crying wolf. So the tool does not argue for a crisis. It reports that a
threshold **the group pre-agreed in calm conditions** has been crossed:

> *"Convene rule (agreed 12 Mar): expected loss above CHF 150k, and more than
> four customer contracts exposed. Both crossed at 06:14 today."*

That moves the decision from a judgement one person owns to a rule the group
already owns. It costs about forty lines of YAML and it is the whole mechanism.

Until the group has actually agreed it, the board says *"proposed convene rule
would be crossed (not yet agreed with the team)"* — and the risk profile page
refuses to record an agreement date that was left blank, because claiming an
agreement it does not have is the one way to break the mechanism outright.

**This does not violate BRIEF §12.** There is no global P×I matrix, and the
per-event matrix stays hidden until a dot is clicked. A one-line portfolio
posture is not a matrix.

---

## Four corrections to the brief's maths

Each would have quietly hollowed out a headline number. Each has a test.

**1 · The baseline must not take the best alternate.** §5.2 says compute delay
on each feasible route and take the minimum; §5.4 then wants
`E[loss|nothing] − E[loss|act]`. Both cannot hold — if the baseline already
routes around the disruption, the baseline has already acted and the value of
acting collapses to zero on exactly the shipments that have alternatives. The
minimum belongs inside the *act* scenario, priced.

**2 · Lateness is not delay.** `E[delay]` is measured against the planned ETA;
penalties accrue against the date promised to the customer. Those differ by the
commitment slack, and charging across the gap invents money. Also `max(0, ·)`
is convex, so it must be evaluated inside the Monte Carlo, never applied to the
mean.

**3 · Event draws must be shared across shipments.** Drawn per shipment, forty
shipments calling at Antwerp experience forty *independent* strikes. That is
physically impossible and it flattens the tail — which is the entire quantity
the convene decision depends on. Durations are drawn once per iteration into an
`(n_draws × n_events)` matrix and shared. Summing across shipments *within* a
draw then gives the correctly correlated portfolio distribution for free.

**4 · `probability_unknown` had nowhere to go.** §8.1 forbids absences becoming
values, but §5.7's matrix has an x-axis of P(late) from 0 to 1 — so an event
whose probability cannot be sourced would land at 0.5, which is the sibling
project's exact bug reappearing in the UI. Unsourced events are drawn in a
separate band **outside** the axis.

---

## The Rhine, and the detail they will catch

**Low water is a payload derate, not a stoppage.** Vessels keep sailing; they
load to reduced draught. The same tonnage then needs more sailings, and
low-water surcharges apply. Modelling it as "barge blocked" is wrong and a Sika
logistics colleague will know it is wrong within one sentence.

**The OTIF consequence runs through "in full", not only "on time".** A derate
that splits one consignment across two sailings fails OTIF even when the first
part arrives early. That is the mechanism no generic weather alert would find,
and it is why this lane is worth demonstrating.

> ⚠️ The Kaub band thresholds in `config.example/thresholds.yaml` are marked
> `source: assumed`. They reproduce the published *shape* but the exact
> centimetre values have **not** been sourced against official WSV/BfG loading
> tables. Source and cite them before any client demo.

---

## Deliberate deviations from the brief

| Brief says | Here | Why |
|---|---|---|
| ~100 risk variables | **45**, fully specified | Q1: no ledger exists, so this file *is* the proposal. 100 half-specified entries contradict §5.0's own claim |
| OSMnx for road/rail routing | Buffered great-circle corridors | Overpass at runtime + geopandas/GEOS chain, for 10–15 named lanes. Ships as a visibly empty socket |
| Shipment-level `etd`/`eta` | **Per-leg** planned windows | The temporal gate asks when the shipment transits *that node* — unanswerable without them |
| MapLibre vendored | globe.gl + vendored topojson | A rotating globe is the ask; globe.gl is vendored from npm, not a CDN |
| No global view (§12) | One-line **state strip** | Q6. Not a matrix; the per-event matrix is untouched |

### The front end

FastAPI + vanilla JS + CSS. No build step, no framework, no CDN — `globe.gl`
and `topojson-client` are vendored from npm under `api/static/vendor/`, the
country geometry under `api/static/geo/`. The page renders with the network
cable pulled out.

**Section 1** is the full viewport: a rotating globe carrying the real route
geometry — the Rhine legs follow the river, the deep-sea legs run through
their chokepoints — with every line coloured by the five-level ladder.
Clicking a line opens its radar on the right.

**Section 2** ranks the *routes* by the severity of the effect on them.
Selecting one opens the response workspace beside it: the actions still open
with their deadlines, who to contact — route owner, standing teams, seniors,
alternate vendors and carriers, all filtered to that route — the escalation
step, and a one-click PDF convening pack plus a plain-text summary to paste
into mail.

### Themes

Four, switched from the topbar and shared by both pages: **Light** (the
default), **Blue**, **Sika** (the client's yellow and red on white) and
**Dark**. The choice persists per browser.

Two things had to change to make a light default honest rather than just
inverted:

* **The White rung cannot be white on a white page.** "Bias — monitor
  closely" drawn in white on a white background is not a quiet level, it is no
  level. On the three light themes it becomes a neutral slate, which keeps its
  position in the ordering while the label beside it keeps carrying the
  meaning. Green and Yellow are darkened to pass contrast.
* **Route transparency has a floor.** The stroke/alpha weights encode the
  *urgency ordering*, which is a property of the ladder and does not change
  with the theme. What changes is how much transparency a background absorbs:
  alpha 0.24 reads as a line against near-black and as nothing against white,
  and a Normal route that renders invisible is indistinguishable from a route
  the gate never found. Each weight is remapped into `[floor, 1]`, so the
  ordering survives and the floor moves.

> **The Sika yellow and red are approximated.** `sika.com` and the brand asset
> hosts are blocked by this environment's egress proxy, so the two values in
> `styles.css` are a visual match, not the brand. They are marked
> `APPROXIMATED` in the file. Swap them for the official values before any
> client-facing use — it is two hex codes in one block.

Sika red is deliberately kept **off** the ladder and off the chrome, appearing
only as the brand rule under the header: on a page whose most urgent rung is
red, a red header would compete with the one colour that has to mean "six
hours". For the same reason `--accent` exists at all — buttons, focus rings
and active tabs used to borrow `--lvl-blue`, which quietly broke the rule that
level colours are reserved. The headless checker now asserts, per theme, that
the accent is not equal to any of the five rungs.

### The per-event risk matrix

A small translucent card, opened from the grid button on any event. There is
deliberately **no dashboard-level P×I scatter** — aggregating every event into
one grid throws away the only thing a planner needs from it, *which of my
shipments*. The event is the question; the points are the answer, and each
point is a shipment.

```
  x   P(this shipment is late)
  y   the bill IF it is late     ← not the expected loss
```

**Those two axes have to be independent, and they were not.** The impact axis
read `expected_loss_chf`, which already has the probability multiplied in —
the Monte Carlo masks each event's delay by its occurrence draw. Plotting it
against `P(late)` counted the same probability on both axes and pushed every
unlikely shipment into the bottom-left corner twice over, for a reason the
x-axis had already expressed.

The engine now computes the **conditional** loss — the mean over the draws
where the shipment actually went late — and the two axes multiply back:

```
p_late × E[loss | late]  ==  E[loss]
```

That identity is the proof the probability is counted once, and it is asserted
in `tests/test_matrix.py` to floating-point precision. (It holds exactly when
no always-on surcharge applies; a Rhine low-water surcharge is charged on every
draw, late or not, and sits outside the lateness decomposition by
construction — that case is tested separately.)

Lead time is a **ring**, not a third axis: solid means options remain, hollow
means they have run out. Four dimensions on a chart that stays readable.

Unsourced-probability events are drawn in a **dashed gutter outside the grid**,
never at a computed 0.5, with the card saying so in as many words.

### The assistant

A grounded chat panel, opened from the topbar for the whole board or from the
speech-bubble on an event for that event. The context is assembled in
`engine/reason/ask.py` from the board itself; the model gets no tools, no
search and no internet, and the system prompt asks for *"The board does not
carry that"* rather than a guess.

**Every answer is visibly marked as generated.** The numbers on the page were
computed by the engine and can be reproduced from the command line; an answer
here was written by a model from a context built out of those numbers. Those
are different kinds of claim, and a planner is entitled to tell them apart at
a glance.

### The risk profile

`/profile`, reached from the topbar. Six tabs:

| Tab | What it holds |
|---|---|
| **Desk** | corridors owned, modes carried, which config files are in force |
| **Network** | every node and lane, their alternatives, how much freight touches each |
| **Risk ledger** | all 45 variables by family, with their delay triples and which have no sourceable probability |
| **Appetite** | the ladder cutoffs and the convene rule — **editable** |
| **Response** | route owners, who is drawn in at each level, seniors, escalation, spend authority |
| **Sources** | every feed as connected / example stand-in / absent |

Three things make it more than a settings screen:

* **It is not a second store.** The profile *is* the config, rendered readable
  — which is what makes "onboarding a new customer is a profile swap" a true
  statement rather than a claim. A test asserts the cutoffs shown are the ones
  `classify()` actually reads, so an edit can never be decorative.
* **Provenance is visible per field.** 6 h / 48 h / 7 days are the client's own
  wording; the materiality floor and the convene thresholds are marked
  *assumed by us*. Somebody being asked to agree a threshold can see which
  numbers came from them.
* **Saving writes `config/scoring.yaml`**, which is gitignored and overrides
  the committed stand-in; Reset deletes it. The write is allow-listed, and a
  ladder that would make a rung unreachable (Critical later than Alert) is
  **refused** rather than saved — that failure is silent on screen, which is
  exactly why it cannot be allowed through. Action durations are deliberately
  read-only: changing one moves every deadline that depends on it.

This is the surface Q3 asked for — *"will also be able to play with the
assumptions"* — with the guard rails that answer implies.

Two design rules the UI holds to:

* **The level colours are reserved.** They mean one thing — how soon a
  decision is needed — and never carry that meaning alone; the directive is
  always beside them. But white on a dark globe is intrinsically the loudest
  thing on screen while White means "Bias: monitor", so **stroke and opacity
  carry the urgency ordering** and hue keeps its prescribed meaning. Red is
  thick and solid; Green is a whisper.
* **The radar's axes are every family the mask checked**, not only the ones
  firing. A family at zero means "checked, contributes nothing", which is
  true and useful. Families that cannot reach the route are excluded, because
  drawing them would imply an assessment that never happened.

---

## The reasoning layer, and the thing that keeps it honest

The sibling operational-risk radar pairs an LLM with a deterministic
challenger. This is the supply-chain version of that idea, and the challenger
half already existed: `engine/variables/rules.py` is a keyword router that has
always run alongside.

### Which local model, and why

**`qwen2.5:7b-instruct` via Ollama** — the default in `engine/reason/llm.py`,
overridable with `RADAR_LOCAL_MODEL`.

| | why it wins here |
|---|---|
| **Structured output** | Ollama's `format` parameter takes a JSON Schema, and Qwen 2.5 follows one without a grammar or a retry loop. The pipeline hands it `Extraction.model_json_schema()` and parses the reply straight into Pydantic. |
| **Multilingual** | Rhine gauge notices are German, Rotterdam and Antwerp port notices are Dutch, and a model that only reads English silently drops the tier-1 sources — which are the only tier allowed to move a date on its own. |
| **7B fits the laptop** | ~5 GB quantised. A planner runs this beside Excel and Teams, and a 14B that swaps is a 14B nobody keeps running. |
| **Licence** | Apache 2.0. Nothing to clear with legal before a pilot. |

```bash
ollama pull qwen2.5:7b-instruct   # ~4.7 GB
ollama serve                      # the radar auto-detects it
```

Worth trying if the machine has room: **`qwen2.5:14b-instruct`** (~9 GB) is
better on the conditional readings — "unless talks resume" — and
**`llama3.1:8b`** is a reasonable substitute where Qwen is unavailable, but it
is weaker on German. **Do not use a 3B**: at that size the schema gets
satisfied by invention, which is the one failure this whole design is built to
avoid.

**Why local first.** Freight data is commercially sensitive — the order book,
the customers, the penalties are exactly what a company will not post to a
third party to try a demo. It is also free, which matters because a hindcast
over two years of archived feeds is thousands of calls. The API path
(`ANTHROPIC_API_KEY`, `claude-opus-5`) exists because a frontier model is
genuinely better on the hard judgement calls; it is opt-in and never required.
`anthropic` is in `requirements-optional.txt` and imported lazily, so the base
install stays at seven packages.

**No model is a supported state, not a degraded one.** With nothing reachable
the router runs alone and the board is complete. The assistant says what is
missing and what connecting it would buy, in the same socket shape every other
absent input uses.

### The check mechanism

`engine/reason/challenge.py`. The obvious way to check a model is to ask
another model; it is also the worst way available here, because it costs a
second call per event and fails in the same places as the first — two
confident wrong answers look like corroboration.

So every check is **deterministic**, and a person can audit the file in one
sitting:

| check | question | blocking |
|---|---|---|
| `grounded` | is the verbatim quote actually in the source? | yes |
| `variables_known` | are the claimed variable ids in the 45-variable ledger? | yes |
| `nodes_known` | are the resolved nodes real? | yes |
| `probability_honest` | did it invent odds for something the ledger marks unsourceable? | yes |
| `variables_justified` | does each activation carry the sentence that caused it? | flag |
| `delay_plausible` | is the estimate anywhere near the family's own band? | flag |

**`grounded` is the one that matters.** A fabricated quote is the most
dangerous output this system can produce, because it is the thing a planner
will trust without checking — it *looks* like evidence. It is also trivially
detectable: the quote either appears in the source or it does not. Substring
matching catches it every time, costs nothing, and cannot itself hallucinate.

`delay_plausible` is deliberately wide, and compares against the family's
worst case across **every** severity rather than the band a keyword heuristic
guessed. Reading "until the weekend at the earliest" as nine days where the
minor band says one and a half is the reasoning layer doing its job; checking
it against the minor band would flag exactly the extractions worth having.

A rejected extraction is not a gap — the router's answer is used, and the
board is complete either way.

> Agreement between router and model is reported but **is not validation**.
> Both were written by the same people from the same variable list, so their
> errors correlate. The honest headline metric is still the hindcast: did we
> fire before the carrier called.

---

## Engineering rules that are load-bearing

**Nothing reads the wall clock.** Every stage takes an explicit `as_of`
(`engine/clock.py`). Three things fall out: the demo is reproducible, the
hindcast is nearly free (a past `as_of` plus archived feeds through the
identical code path), and every deadline on the board means something. A test
walks the AST of `engine/` to enforce it.

**An absence never becomes a value.** Unknown probability is `None` plus a
reason. An unconfigured action time is `None`, never a zero that would make the
action look permanently available. A node with no alternatives says so.

**Abstention is structurally valid.** `Extraction | Abstention` is a
discriminated union. "Every field required" plus "you must answer" is a
hallucination generator — the model invents values to satisfy the schema.

**Empty sockets are visibly empty.** The `/inputs` panel lists every input as
connected / example stand-in / absent, with one line on what each absence
costs. Nothing is silently defaulted.

**Provenance on every number**, extended to numeric config: a made-up wind
threshold is exactly as dangerous as a made-up quote, so every threshold
carries `source:` and assumed ones say so on screen.

**The funnel is measured, not asserted.** §3.2's 10,000 → 20 is a design
sketch; `/inputs` shows this run's real counts.

---

## Repository

```
engine/            institution-agnostic core — knows nothing about Sika
  clock.py         the as-of discipline
  schemas.py       every Pydantic model
  network/         nodes, corridors, geometry (no OSMnx)
  ingest/          shipments, gauges, feeds, socket contract
  variables/       the mask; rules.py is the CHALLENGER, not a fallback
  gate/            spatial ∧ temporal ∧ modal
  simulate/        portfolio draw matrix, buffer propagation
  score/           CHF, lead time, the five-level ladder
  act/             playbook, owners, contacts
  portfolio/       the convene rule
config.example/    public, synthetic, committed
  export/          board assembly, PDF pack, risk profile
config/            gitignored — the customer's real data
api/               thin FastAPI + the front end
  static/          index.html, profile.html, styles.css, app.js, profile.js
  static/vendor/   globe.gl, topojson-client (npm, not CDN)
  static/geo/      country geometry (world-atlas)
data/fixtures/     committed sample feeds, labelled
tests/             51 tests
```

`engine/` never imports from `api/` or reads `config/` directly.
Onboarding a new customer is a profile swap — that is true here, not a claim.

---

## Known gaps

- **Everything is synthetic and labelled synthetic.** Shipments are generated;
  the news corpus is written by us; the Kaub series is a shaped reconstruction.
- **The reasoning layer is specified, not wired.** The rules router runs as the
  challenger; `reason/` is the next build step.
- **Kaub thresholds need sourcing** (above).
- **No accuracy claim is made.** The honest metric is the hindcast — did we fire
  before the carrier called? — and it has not run yet. Agreement between rules
  and model is worth shipping as a diagnostic but is *not* validation: both were
  written by the same people from the same variable list, so their errors
  correlate.

## Open questions for Sika

1. Do your contracts actually carry per-day delay penalties, or is the real cost
   expediting plus customer escalation? (The cost model has three components so
   it survives either answer, but this decides which one carries the weight.)
2. Would you pre-agree a convene threshold in calm conditions?
3. Does the shipment data include actual vs. planned arrival dates, or only
   lanes and volumes? (Decides whether the hindcast produces a *measured*
   lead-time claim or a plausible one.)
4. In a Rhine low-water week, what actually happens today — partial loading,
   rail switch, surcharge acceptance, deferral?
5. Who *doesn't* have authority, and above what CHF?
