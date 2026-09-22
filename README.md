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

## The severity formula

`engine/score/severity.py::classify` — the swap point, filled in.

### It is a time-to-act scale, so severity is a deadline

The client's own wording phrases every rung as one: *act within 6 hours*,
*within 24–48*, *determine action within 3–7 days*. So severity here is **how
soon must somebody decide**, never *how bad is this*. The raw answer is the
clock the engine already computes:

```
τ = decision_deadline − as_of        where  decision_deadline = impact − action_duration
τ_binding = min(τ) over shipments that still have an option open
```

### Magnitude compresses the clock, it does not replace it

On its own the clock under-serves the question: two routes both a week out
score identically whether CHF 900 or CHF 225 000 sits on them. That is
precisely the failure Sika named in Q6 — *the real problem is that we declare
a crisis too late* — arriving as an arithmetic property rather than a
cultural one.

$$\tau_{\text{eff}} = \frac{\tau_{\text{binding}}}{U}, \qquad U = 1 + 0.8M + 0.4L + 0.8D$$

and `τ_eff` is read against **the client's own 6 / 48 / 168**. Their
thresholds are never touched: re-tuning them would make the ladder ours
instead of theirs.

| term | is | why |
|---|---|---|
| $M$ | $\text{clamp}\!\left(\dfrac{\log_{10}(E/1000)}{\log_{10}(150000/1000)},0,1\right)$ | **Log.** Money here spans four orders of magnitude; a linear term saturates at the first big number, after which every large route looks identical. Anchored on the material floor and the convene threshold — both already in `scoring.yaml`, so no new parameter. |
| $L$ | $P(\text{late})$ of the binding shipment | At $P=0.95$ decide now; at $P=0.2$ waiting buys information. Straight from the Monte Carlo. |
| $D$ | 1 if an **irreversible** damage pathway is open | From the Layer 4 gate. Reversible degradation does *not* count — that is a cost, already carried by $E$, and counting it twice would make a chilled-but-recoverable adhesive read like scrapped polymer. |

**Additive, never multiplicative.** A product of [0,1] factors drives toward
zero as dimensions are added, so the model would get *quieter* the more it
was taught. A bounded sum cannot, and each term stays separately inspectable
— you can ask which one moved a route and get an answer.

**$U \ge 1$ always.** Compression may only make a deadline sooner. A term
that could push one further out would let a large exposure *hide* a real
clock, which is the opposite of the point.

### The one-rung bound is provable, and tested against the live config

$U_{\max} = 1 + \sum w = 3.0$. The adjacent threshold ratios are
$168/48 = 3.5$ and $48/6 = 8$. Since $3.0 < 3.5$:

> **A single compression can never advance more than one rung.**

Money may make you decide sooner; it can never manufacture a six-hour
emergency out of a week of slack. The test asserts the two sides *against
each other* rather than against constants, so changing either the weights or
the client's cutoffs fails loudly instead of quietly letting the bound lapse.

### A dead band, because boundaries are sticky

Near a threshold any continuous modifier tips — 176 h with CHF 2 000 still
crosses 168. Without a band a route churns between rungs on successive runs
as exposure wobbles, and **a level that flickers is a level nobody
believes**. `dead_band: 0.10` requires the compressed clock to clear the next
boundary by 10 % before re-levelling.

Measured over 22 instants of the demo board: **10 re-levellings, every one
exactly one rung**, concentrated on the highest-exposure route. Not silent,
not noisy.

### The deadline a planner works to is never changed

Compression moves the **level**. `lead_time_hours` stays the real clock, and
the reason string carries both:

> *5 shipment(s) on this route still have an option open; the first expires
> in 7 h. **Treated as 4 h rather than 7 h — CHF 62,803 at stake.***

Telling somebody they have 4 hours when they have 7 would be a lie dressed
as urgency. The full working — multiplier, each term, raw and effective
hours — rides on the route payload as `urgency`, because a level that moved
for a reason nobody can see is a level they will argue with, and they would
be right to.

### Corroboration: what one uncorroborated source may do

Sika, answering Q5: factor in social media, in line with the corroboration
threshold. Social media is not a *better* signal, it is an *earlier* one —
its whole value is arriving while there is still time to act.

So it may raise a flag and may not on its own move a delivery date. An
uncorroborated tier-3 report **caps at Watch**; a second independent tier-3
source lifts the cap; an authority notice at tier 1–2 needs no corroboration
at all, because it is the record rather than a claim about it. The cap only
ever *lowers* a level — it can never promote a quiet route.

### The numbers, and who owns them

All in `config.example/scoring.yaml` under `urgency` and `corroboration`,
marked **ASSUMED**, editable from the risk profile page. They are the
arguable part and they belong to the planning team:

```yaml
urgency:
  weights: {magnitude: 0.8, likelihood: 0.4, irreversible: 0.8}   # sum ≤ 2.5
  magnitude_reference_chf: 150000
  dead_band: 0.10
corroboration:
  uncorroborated_tier3_cap: blue
```

---

## The driver app, and the loop it closes

`/driver` — one screen, for the person with the freight in front of them.

### It is the only tier-1 *observed* source in the system

Every other input here describes a **region**. Pegelonline says the Rhine is
at 78 cm; trade press says Antwerp dockers have voted; a forecast says it
will blow. All of it is inference about whether *your* freight is affected.

A driver looking at their own trailer is not inference. *"I am third in a
queue of forty at Kaub and the lock is shut"* is an observation of the actual
consignment, and it beats every feed in this system — **including the ones
that are not connected yet**. That is why it was worth building before the
paid APIs.

### The loop

```
planner's playbook       "confirm the disruption with the carrier"
        │                "request the GPS position"
        │                "record the revised ETA"
        ▼
driver's phone           taps four things
        ▼
report lands             tier 1, observed
        ▼
planner's gate OPENS     rerouting unlocked
```

The checklist steps a driver can answer are the three confirming ones, and
answering them **is** the confirmation — a planner is not asked to re-tick a
box a driver already answered, because that is the "we declare too late"
failure expressed as a process. The gate then says so:

> *Confirmed — 3 step(s) answered by a field report from the freight itself,
> not from a feed.*

**A driver saying "moving" does not confirm a disruption.** It is evidence
*against* one. Only an explicit *"yes, I can see this happening"* counts,
and it is a separate control from the status with its own warning, because
treating a status as a confirmation would unlock a reroute on good news.

### Designed for a cab, not a desk

The person using this is in a tunnel, at a gate, or on a deck — often
one-handed, in bad light, on a cracked phone, with no signal.

* **Four taps to file**, and the first three are optional.
* **48px+ targets**, asserted by the headless check.
* **It shows what the office is waiting for.** The driver is not guessing
  what would be useful; they are answering a question somebody actually
  asked, and the field that answers it is marked *they are waiting for this*.
* **No status the tool computed.** A driver is not asked whether the shipment
  is Critical. The ladder, the matrix and the convene rule are absent.
* **It works with no signal.** A report is written to the phone first and
  sent afterwards. The queue drains itself when the connection returns, and
  on the next visit if the app was closed. **A report lost in a tunnel is
  worse than no app at all.**

`observed_at` is stamped when the driver taps send, **not** when the report
reaches the server. A report queued in the Gotthard and delivered forty
minutes later must not claim to be a forty-minute-old observation — the
planner's whole decision turns on when somebody actually looked.

### Append-only, always

A correction is a **new report**, never an edit. Three reasons, all of which
bite:

* a driver who said *"held"* at 09:00 and *"moving"* at 11:00 has told us
  the **duration of the hold**, which a mutable row would erase;
* an audit asking *"what did we know at 10:00"* needs the log as it stood at
  10:00;
* the hindcast replays the log, so an edited history makes every past board
  irreproducible.

Storage is JSON Lines under `data/`. No database — the base install is seven
packages and a dependency here is one a planner installs before the demo
runs. It is also the format an auditor can read without our help, and a
report a lawyer can open in Notepad is worth more than one behind an ORM.

### The as-of discipline survives a live feed

This is where that usually breaks. It does not break here: a board at as-of
**T** sees only reports observed at or **before** T, so replaying yesterday
gives yesterday's answer even though the log has grown since. The wall clock
is read **once**, at the API boundary, when a report arrives.

Reports observed *after* the board's instant are **not hidden** — the
endpoint reports how many it excluded, because a planner looking at
Tuesday's board needs to know something came in on Thursday.

> **The demo clock.** A pinned board plus a real-time report means the driver
> taps send and nothing appears — correct, and useless. So the driver app,
> *when opened from a pinned board*, stamps inside that board's frame and
> **says so on screen**. The fiction is labelled rather than hidden: a demo
> affordance a viewer cannot see is one they will mistake for real
> behaviour.

### Live both ways

Server-sent events, so a planner sees a report land without refreshing.
**SSE rather than websockets** — built into Starlette so it costs no new
dependency, it reconnects on its own, and the traffic only ever flows one
way. The stream is a *notification*; the append-only log is the record, and
a client that was disconnected re-reads the log rather than expecting an
in-memory buffer to have held its events.

> **Security:** `POST /api/v1/reports` is **unauthenticated in the
> prototype** and it is the one thing on the list that must change before
> real drivers use it — this is the only endpoint that can unlock a reroute.
> A shared token via `RADAR_REPORT_TOKEN` is enforced when set, and
> `SECURITY.md` has the path to per-driver credentials.

---

## Operations: the playbook, the consignment list, the execute view

`/ops?route=…` — three surfaces over one engine, none of which recomputes
anything.

### The playbook, and the gate that is the point

Detect → Confirm → Act → Close out. Each task carries an owner and an SLA
read from `contacts.yaml`, because a playbook quoting invented response times
is one nobody is held to.

The rule is the feature:

> **Do not reroute until the disruption has been confirmed.**

and it is *enforced*, not printed. `unlocked_actions()` withholds the routing
mitigations until the confirming tasks are ticked. A rule that appears as
advisory text above a button that still works is not a control — it is a
caption.

Why it matters here specifically: this tool reads trade press and, per Q5,
social media. Those are **early** signals, which is their whole value, and
early signals are wrong more often than late ones. Rerouting a barge on an
unconfirmed rumour costs real money and burns the credibility that makes
anyone act on the next alert. **The gate is what lets the tool be early
without being reckless.**

Three details worth knowing:

* **Corroboration is part of the gate, not a separate idea.** An event whose
  only source is uncorroborated tier 3 cannot be confirmed by ticking a box,
  because nothing about ticking a box makes a rumour true. The task is
  disabled and names what *would* resolve it — a carrier callback, an
  authority notice.
* **Notifying the customer is never locked.** Telling somebody early costs
  nothing and is the one action that never needs confirming.
* **Locked actions are withheld, not greyed**, and the reason is stated once
  in the gate banner rather than repeated on every row. Identical per-shipment
  actions are grouped — a planner takes one decision and applies it to the
  freight it fits, so eight rows reading *"Switch barge leg to rail"* are one
  row reading *"× 8 consignments"*.

The engine owns the steps and the gate, both pure and testable. The **ticks
live in the browser**, because *"has Maria called the carrier yet"* is
per-planner working state, not a fact about the world — putting it in the
engine would make the board's answer depend on who was looking at it.

### The consignment list

From the team:

> *"one disruption on a route may only affect some of the vessels using that
> route. And the effects will not be the same for all vessels along the same
> route."*

Correct — and it has always been true in the engine, because the gate is per
`(event, shipment, leg)` and the Monte Carlo is per shipment. It was simply
never visible: the board painted one colour per lane.

On `LANE_RHINE_01`, one disruption, one lane:

```
17 consignments · 11 touched · 10 DISTINCT DEADLINES · 6 out of scope entirely
                  deadlines running from 36 h to 11 days
```

If that number were 1, a lane colour would be sufficient and this page would
be decoration. There is a test asserting it is not.

Untouched consignments stay on the page **as untouched**, with `null` rather
than zero — *"this event does not reach six of your seventeen"* is an answer
a planner wants, and zero would read as "assessed and found harmless", which
is a different claim.

### The execute view

Also from the team, and it is the binding architectural constraint:

> *"The calculations cannot be simplified."*

So `execute_view` returns a **subset of numbers already computed for the
planner's board**. It recomputes nothing, and there is a test that asserts
the deadline it shows is the identical float the board published. The moment
a driver's screen works out its own ETA it will disagree with the planner's,
and a planner contradicted by their own tool once stops using it.

What the transport manager gets is therefore not a smaller model — it is the
same model answering only the questions someone *executing* needs: where is
it going next, which hop is the problem, what is the constraint, who do I
call. The ladder arithmetic, the matrix and the convene rule are absent
because they are not theirs to decide, not because they were too complicated
to show. A test greps for them and fails if any leaks in.

It names **which legs** are affected, not just the shipment: a Rhine
low-water event hits the barge legs and leaves the road leg alone, and
someone executing needs to know which hop is the problem.

> **Assumption, pending an answer:** "transport manager" is built to serve
> both readings — a driver and an on-site agent — since the view is the same
> either way. If it turns out to mean only one, the `report_back` fields are
> the part that would change.

**Report back** is a socket. A driver or an agent knows things no feed here
carries — the queue at the gate, whether the crane turned up, whether the
load shifted — and submitting it would be the first **tier-1 observed**
source in the system, better than anything currently wired, because it is
somebody looking at the freight rather than a feed describing the region. The
app composes; it does not submit, because no store is wired.

### Lane markers

One marker per lane on the globe, radius scaled by how much freight the gate
touched, coloured by the lane's level. Clicking it opens the consignment
list.

Deliberately **not** one marker per vessel: roughly 65 of 125 shipments are
touched in a typical run, and 65 dots on a globe is exactly the clutter the
brief warned about. The per-vessel divergence is real and belongs on a page
that can hold it.

Radius scales with the **square root** of the affected count, because a
ring's visual weight is its area — a linear radius makes a lane with twice
the freight look four times as bad.

---

## The four-layer taxonomy, and the idea that makes it compose

Layer 1 is the 45-variable ledger. Layers 2–4 describe **our** side of the
encounter, and live in `config.example/taxonomy.yaml`:

| Layer | What it is | What it contributes |
|---|---|---|
| **1 · Event** | the 45 variables | *pathway* — damage, delay, or both |
| **2 · Asset** | 15 equipment types, 8 node kinds | a **gate**: can this event physically reach this equipment? |
| **3 · Channel** | 5 delivery channels | a **consequence**: what lateness costs, and how much is free |
| **4 · Cargo** | 11 vulnerability classes | a **gate**: does the damage pathway open? |

### Pathway is the load-bearing idea

The obvious scoring model is the one an architecture brief asks for:

```
Alert = f(severity) × overlap × vulnerability × criticality
```

It cannot answer the commonest question on a freight desk, which is what to
do about a **severe event hitting invulnerable cargo**. Set vulnerability near
zero and a four-day dock strike stops mattering. Set it near one and ambient
dry mortar gets flagged for a freeze. No override rule fixes it, because the
question was malformed. *"How vulnerable is this cargo"* is not one question.

It is two:

```
DELAY    the freight moves late. Applies to everything on the corridor —
         delay does not care what is in the box.
DAMAGE   the goods are harmed. Only where Layer 4 opens the gate.
```

Split them and every clash in the brief resolves itself:

| encounter | delay | damage | answer |
|---|---|---|---|
| dock strike + dry mortar | open | shut | *"no cargo risk, four days late"* |
| freeze + dry mortar | — | shut | nothing, correctly |
| freeze + water-based polymer | open | **open, irreversible** | scrap, not late |
| freeze + polymer in a reefer | open | gated by genset autonomy | a countdown |

None of those needed a rule. They fall out of asking the right question.

`config.example/taxonomy.yaml` still declares seven `clash_rules`, but they
are the genuinely contested cases — pot-life turning delay *into* damage, ADR
blocking the standard expedite, overlapping events taking the max per pathway
rather than the sum. The resolver records every clash it settled **including
the ones it did not have to adjudicate**, because a severe event producing a
calm answer looks like a missed alarm until you can see why it is calm.

### Gates and consequences, never weights

Everything in Layers 2 and 4 is a yes/no about physics or regulation.
*"Freeze-critical cargo is 0.8 vulnerable to a freeze"* is a number nobody can
source; *"the emulsion breaks below 0 °C"* is in the product data sheet. Layer
4 carries **two** thresholds for exactly this reason — 5 °C is the
precautionary margin and 0 °C is the physics, and the gap between them is the
difference between *expedite it* and *write it off and remake*.

Layer 3 is a consequence model rather than a criticality score, because
*"criticality 4"* cannot express the distinction that decides what to do:

```
b2b_distributor     8 h of dock flexibility, then CHF 180/day   (linear)
retail_diy          1 h, then a flat CHF 350 chargeback         (step)
direct_to_jobsite   30 min, then a crew stands idle             (cliff)
```

Those are three different **shapes**. A retail penalty stops growing, so an
expedite has to beat CHF 350 and no more; a jobsite penalty does not.

## The 0–100 Alert Score

A TMS wants a number — SAP TM and Blue Yonder both route on thresholds — so
the board emits one. It is a **projection of the ladder, not a second model**:

```
score = 20 × rung_rank + 20 × consequence_fraction

Normal 0–19 │ Bias 20–39 │ Watch 40–59 │ Alert 60–79 │ Critical 80–99
```

which lands the requested bands on the client's own rungs without either
having to move:

| band | rungs | meaning |
|---|---|---|
| **Green** 0–39 | Normal + Bias | informational; monitor |
| **Amber** 40–69 | Watch + low Alert | evaluate a reroute |
| **Red** 70–100 | high Alert + Critical | act now |

The fraction is clamped inside its own decade, so a Bias shipment carrying
the book's largest exposure can never outscore a Watch one. **Disagreement
between the score and the ladder is structurally impossible**, and a
parametrised test asserts it for all five rungs — two scales over one
shipment will otherwise drift, and then the board says Yellow while the TMS
says 38/Green.

Irreversible cargo damage **escalates the rung**, because the ladder measures
time-to-act and irreversible damage compresses it to nothing: once the
emulsion has broken there is no later moment at which the same decision is
still available. Reversible degradation does not escalate — it is a cost, and
costs are already carried by the consequence term.

### The three validation scenarios

Executable, in `tests/test_taxonomy.py`:

| | scenario | result |
|---|---|---|
| **A** | −7 °C freeze · water-based polymer · jobsite | both pathways, irreversible, **escalated** Blue → Alert |
| **B** | dock strike · ambient dry mortar · distributor | **41, Amber.** No cargo risk; 88 chargeable hours priced |
| **C** | weekend driving ban · ADR Class 3 · JIT plant | **76, Red.** Air freight suppressed, not ranked low |

**B is the one that matters.** It is the clash the brief names, and the case
where a multiplicative score must choose between a false Red and silently
dropping a four-day delay. Here it produces a third, correct answer — and the
test asserts *both* halves: no cargo risk, and a real delay.

## External sources, and the shock they exist to catch

Nine feeds ship wired. **Every one is free, keyless, and needs no
registration.** Nothing in the catalogue can cost money — a paid source cannot
be enabled by editing config, and a test fails the build if one ever ships
enabled.

| Source | Family | Tier | Nature |
|---|---|---|---|
| **GDELT 2.0 DOC** | geopolitical, labour, port ops | 2 | report |
| GDACS (EU JRC) | force majeure, climate | 1 | report |
| ReliefWeb (UN OCHA) | force majeure, geopolitical | 2 | report |
| CISA KEV | cyber | 1 | report |
| Autobahn A5 / A61 / A3 | infrastructure | 1 | report |
| USGS earthquakes | force majeure | 1 | **instrument** |
| Open-Meteo marine | climate | 1 | **instrument** |

Two more — ENTSO-E and OpenSanctions — are free but need a free registration,
so they ship **disabled** and say exactly which variable to set.

**Network is off by default.** Nothing reaches the internet until
`RADAR_ALLOW_NETWORK=1`; every source falls back to a recorded fixture and says
so on `/inputs`. A tool that quietly starts calling nine external services on
first run is one nobody can deploy inside a corporate network.

### Nature: the field that decides who pays for a model call

`instrument`
: Something measured a number. A gauge reads 78 cm, a seismometer reads M6.1.
  A threshold table maps it to a variable, exactly and for free. **These never
  reach a model** — paying one to re-derive arithmetic it cannot check buys a
  worse answer.

`report`
: Somebody *said* a thing. The claim needs reading. **This is what the funnel
  is for.**

That one field is why the model bill is small.

### Adding a source is a block of YAML

`config/sources.yaml` is the whole integration surface. Name the URL, name
where the items are, name which field means what — timeouts, retries, fixture
fallback, the honest `/inputs` status, the funnel and the challenger all come
for free. Nothing in `engine/` changes.

```yaml
custom:
  - key: tms_events
    label: "Transport management system — exception events"
    nature: report
    source_tier: 1                       # your own system, watching your own freight
    url: "${TMS_BASE_URL}/api/v1/exceptions"
    items_path: "data.events"
    auth: {kind: bearer, env: TMS_BEARER_TOKEN}   # the NAME, never the value
    fields:
      headline: "summary"
      identifier: "eventId"
      node_hint: "location.unlocode"
      starts: "effectiveFrom"
```

Three worked examples ship disabled: a TMS exception stream, a DATEX II
national access point, and a minimal carrier notice feed.

**A credential can never appear in this file.** The loader refuses a spec
containing `token:`, `secret:` or `password:` outright, and accepts only the
*name* of an environment variable. `.gitignore` protects a file somebody might
still email; a value that was never in the file cannot be emailed.

---

## The funnel: what reaches a model, and what a model may decide

```
~50,000  items      server-side query        free, zero bytes transferred
   ~400  items      deterministic gate       free, microseconds
   ~200  items      instrument split         free — measured numbers leave here
    ~40  items      STAGE 1  triage          small model, one yes/no
     ~8  items      STAGE 2  extraction      strong model, structured JSON
     ~8  verdicts   deterministic challenge  free, auditable
```

Each layer is cheaper than the one below and removes more. **The expensive
reader only ever sees what four free filters could not dismiss.**

### The noise filter is not a model, on purpose

Three reasons, in the order they bite:

1. **Cost.** GDELT alone is tens of thousands of items a day.
2. **Reproducibility.** A hindcast must replay a past day and produce that
   day's board. Sampling at the top of the funnel makes every past board
   un-replayable.
3. **Silence.** The one that matters. A deterministic filter that is wrong
   leaves a rule you can read and a count you can see. A model that drops an
   item leaves *nothing* — no trace, no count, no way to find out. **The most
   dangerous filter is the one that fails invisibly.**

The GDELT query does the first layer server-side, built from this deployment's
own chokepoints and ports, so the items we do not want are never sent.

### Two models, different sizes, split by cost not by job

**Stage 1 — triage** answers one question: could this affect the physical
movement of freight? Nearly a classification task, so a 3B does it about as
well as a 70B — and it must be small, because it runs the most times.

**Stage 2 — extraction** reads the survivor properly and returns JSON: what
happened, where, when, how long, how likely, which of the 45 ledger variables
it activates, and what it quotes.

Splitting them is cheaper than one mid-sized model doing both, and better at
each end.

### Rules that keep it honest

- **Triage fails OPEN.** No model, a timeout, a crash — the item passes. *A
  filter that deletes evidence when it breaks is worse than no filter.*
- **Triage may only REMOVE.** A "yes" buys a full read and nothing more.
  Nothing a model says at stage 1 reaches the board.
- **A budget, always** — and the overflow passes through *unfiltered*, never
  silently dropped.
- **The challenger stays deterministic.** An LLM judging an LLM costs a second
  call and fails on the same sentences as the first; two confident wrong
  answers look like corroboration.
- **The model never scores.** It turns a sentence into structured claims about
  what happened. What that is *worth* is computed, from config, by code a
  planner can read.

---

## The Hormuz scenario

> "Iran announces closure of the Strait of Hormuz to commercial shipping."

This is the class of event the product exists for, and the class **no API
predicts.** Weather is forecast days out by a free endpoint. A strait closed by
announcement arrives as a sentence, or not at all.

Run it — no model required, no network required:

```bash
.venv/bin/python -m pytest tests/test_funnel.py -q     # 29 tests
```

What the chain does, with everything deterministic:

| Layer | Result |
|---|---|
| GDELT query | asks about Hormuz server-side; the item is returned |
| Place resolution | `"Strait of Hormuz"` → `CHOKE_HORMUZ`, no coordinates needed |
| Router | matches `GEO_CONFLICT` |
| Gate | window widened from the ledger's 60 days, strikes 3 Gulf shipments |
| Board | `LANE_GULF_01` raised to **yellow, 20 h to act, CHF 16,101** |

Those figures are **at the pinned as-of `2026-09-18T06:00Z`**, which is the
instant the fixture was written for. Open the board on today's wall clock and
the same item is several days old, so the lane reads lower — correctly, since
the ladder measures *time to act*, not how dramatic the event is. Pin the
as-of to reproduce the numbers above:

```
http://localhost:8000/?as_of=2026-09-18T06:00:00%2B00:00
```

### Why a second Hormuz headline needs the model

```
"Iran announces closure of Strait of Hormuz…"              → GEO_CONFLICT  (free)
"Unprecedented maritime interdiction regime declared…"     → ABSTAIN
```

Same meaning. No shared vocabulary. **That is not a gap to be closed by adding
another keyword** — the next shock will use different words again. So an
abstention on an item that already cleared the geographic filter is not
dropped: it is held as a **rescue candidate** and sent to the funnel, which
asks the one question a keyword list structurally cannot answer. With no model
running, rescue does nothing and these drop exactly as they did before.

### Jebel Ali is the point

Nothing in that sentence mentions **Jebel Ali**, and Jebel Ali is where the
freight is. Concluding *"therefore the consignments behind Hormuz are
stranded — stranded rather than delayed, because there is no second route into
the Gulf"* is inference over a network the text has never heard of. That is
what stage 2 is bought for.

`CHOKE_HORMUZ.alternatives` is `[]`. Suez has the Cape; Malacca has Sunda and
Lombok; **Hormuz has nothing.** That empty list is a domain fact, and it is
what makes a Hormuz event a *damage* pathway rather than a delay one.

### Two real bugs this scenario found

**The gate expired the closure before any ship reached it.** An open-ended
event was widened by a global three-day constant, so a strait closure on
18 September ended on the 21st while the freight arrived on 10 October. The
ledger already declared `GEO_CONFLICT` as lasting 60 days — that figure was
simply being ignored. The fallback now reads the ledger, floored at three days
and capped at 120.

**A motorway closure landed on a Rhine barge leg.** "A5 closed after HGV fire"
matches `FOR_FIRE`, which is declared for every mode. The variable is right to
be generic; the *source* knows better, so a spec may now declare the modes it
can possibly speak about. A source narrows a variable and never widens it, and
a contradiction is ignored rather than obeyed — an empty intersection would
remove the event from the gate entirely, which is a drop disguised as a filter.

---

## Integration contracts

- **`schemas/event_radar_taxonomy.json`** — generated by
  `scripts/gen_schema.py` from the config, so the enums cannot drift from
  what the engine loads. `--check` fails CI when stale. Every layer carries
  an `unmapped` fallback with a required `reason`: a taxonomy that cannot say
  *"we saw something we have no box for"* forces the caller into the nearest
  wrong box, and a wrong box is indistinguishable from a right one downstream.
- **`GET /api/v1/shipment-alerts`** — the exact payload this system would
  POST to a TMS, exposed as a GET so an integrator can see the shape before
  wiring anything. A worked RED example is in
  `docs/examples/tms_red_alert.json`.
- **`SECURITY.md`** — zero-cost offline operation, `.env` management, and the
  secret-leakage audit that runs in the suite. That audit found a real gap the
  first time it ran: `.env` was not in `.gitignore`.

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
