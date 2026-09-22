# Solution A — fast action

The previous build optimised for expected loss. This one optimises for the
delivery date, and treats profitability as a constraint rather than an
objective. The change is not a weighting; it is a different shape of decision,
and it touches the ranking, the option set, the pipeline and the screen.

```
BEFORE   rank every option by expected loss avoided; recommend the cheapest
         one that survives. Cost IS the objective.

NOW      discard every option that would take the consignment into a loss;
         among what is left, the fastest wins outright. Cost is a VETO.
```

Nothing in `engine/` outside `engine/fast/` changed its meaning. What moved is
which surface is the front page:

| | |
|---|---|
| `/` | the decision surface — what needs me first, and what do I press |
| `/board` | the analytical board — the globe, the ladder, why a lane is bad |
| `/fast` | an alias of `/`, so older links keep working |

A planner opening the tool during an incident is asking the first question,
and making them find a second page to ask it was the complaint. The board is
one click away, because "why is this lane bad" is a real question — just not
the first one.

---

## 1. Core optimisation — delivery-first

`engine/fast/options.py`, `engine/fast/margin.py`

Every option, wherever it came from, is put on one scale and sorted
lexicographically. The first four keys are about time; money is the last, and
only breaks ties:

```python
rank_key = (
    expired,                   # gone options never outrank live ones
    not on_time,               # does it hold the customer's date
    not restores_delivery,     # does it MOVE the freight, or only manage the consequence
    not executable,            # is it a lever we hold, or a request we make
    days_late_after,
    hours_to_resolve,
    -margin_chf,               # tie-break only
)
```

Two of these keys are the whole pivot.

**`restores_delivery`** separates "get it there" from "handle the fallout".
Notifying the customer recovers nothing; it converts a missed commitment into
an agreed one, which is worth a great deal and is not the same thing. Without
this flag the notification wins every ranking on a lane with schedule slack,
because it is always the quickest thing to do. The line sits at a residual
fraction of 0.5 — escalating to the carrier (0.80), reserving capacity (0.60)
and part-loading (0.55) sit above it; the actual re-routings (0.15–0.35) sit
below.

**`executable`** encodes *speed of action* rather than speed of sending an
email. A carrier's decision is a request however fast the button is, so an
option we do not own can never be the primary action. It is rendered as
"Who to call", not as a button.

### The profitability veto

`margin.py` computes, per consignment:

```
contribution     = value_chf × gross_margin_rate(product family)
residual penalty = days still late after acting × SLA rate
                   + the customer-impact charge if it is still late
margin           = contribution − action cost − residual penalty
```

An option below `margin_floor_chf` (default 0) is **removed before ranking**,
not ranked lower. A loss-making option is not a slow option; it is not an
option. It is still returned, in `vetoed`, with the arithmetic — because "we
could have trucked it, but it would have cost more than the load is worth" is
a sentence a planner needs to be able to say, and a silently shortened list
cannot say it.

One exception, and it matters: **the veto applies to spending, not to the
situation.** An option that costs nothing cannot turn a profit into a loss —
where the margin has already gone, the delay spent it. Vetoing the free
fallback would empty the list at exactly the moment the planner needs to be
told to ring the customer.

Rates live in `config.example/fast.yaml`, every one carrying `source`. A
missing file falls back to a *lower* assumed margin, which vetoes more
options — so an unconfigured install is more cautious, never less.

---

## 2. Event ingestion — low latency, no batch

`engine/fast/bus.py`, `engine/fast/watch.py`

The batch answered "what does the book look like as of 06:00". An item
arriving at 07:41 waited for the next run. Arrival is now the trigger.

```
field report  ─┐
inbound hook  ─┼─→ Watcher.saw_*()  ─→  BUS.publish()  ─→  SSE  ─→  the screen
polled source ─┘        (classify)         (in-process)
```

`publish()` is synchronous and returns once every subscriber has been handed
the message. A broken subscriber cannot silence the bus. Async subscribers get
a bounded queue that drops the **oldest** message when it fills, because a
dashboard that has fallen behind wants current state, not a faithful replay of
a backlog.

The classifier is deliberately narrow and deterministic. `moving` is not an
incident. Neither is `queued` — a queue is normal at a port, and a stream that
fires on every heartbeat gets muted, which costs more than the latency it
saved. `held`, `stopped`, damage, and an ETA slip beyond six hours are
incidents; everything else is a heartbeat.

`POST /api/v1/reports` now hands each report to the watcher before the
response is written. That is the lowest-latency signal in the system —
somebody standing next to the problem — and making it wait for a batch was the
thing worth fixing.

Nothing here reads the wall clock: every entry point takes the instant as an
argument, so a replay reproduces the same incidents.

---

## 3. Orchestration and connectivity

`engine/fast/dispatch.py`, `engine/fast/execute.py`

Three channel kinds, configured in `fast.yaml`:

| kind | reaches | needs egress |
|---|---|---|
| `socket` | planner screens, driver app — in-process onto the bus, out over SSE | no |
| `webhook` | ground ops, authorities — HTTP POST to a URL held in an **env var** | yes |
| `api` | carriers — same mechanism, separated so a planner sees which failed | yes |

Every channel ships **disabled**, and a disabled channel *records* the message
it would have sent and reports `recorded`, with the reason. The failure being
avoided is a demo where nothing is wired, everything says "sent", and the
first real incident discovers it. `GET /api/v2/outbox` shows exactly what was
recorded and to whom.

Nothing in dispatch raises. A dead webhook must not be able to stop an
executed reroute; every failure becomes a receipt with `ok: false`.

The config names the **environment variable**, never the URL:

```yaml
- id: ops_webhook
  kind: webhook
  enabled: false
  url_env: RADAR_OPS_WEBHOOK_URL     # the NAME, never a value
```

---

## 4. Contingency — generated, not declared

`engine/fast/contingency.py`

The old playbook could only offer what somebody had written down. This
searches.

A directed graph is built from every leg any configured lane uses (in **both**
directions — a consignment diverted off the river may need Duisburg → Basel to
reach a railhead) plus every declared node alternative as a lateral road hop.
Each edge is weighted in **hours**: `distance ÷ modal speed + handling at the
arrival node`.

Then Dijkstra, with the failed nodes **deleted from the graph rather than
penalised**. A weight says "expensive but possible"; a closed lock is not
expensive, it is shut, and a path through it is not a slower answer but a
wrong one.

Weighting by time and applying money afterwards is the point. The cheapest
path and the fastest path are different paths, and on a broken lane they are
very different.

Distinct alternatives come from banning one hop of the previous winner at a
time (a cut-down Yen). Without that, the second-best path is the best one with
a single node swapped, which is not a second option a planner can use.

A generated reroute is charged the **difference** against the planned path,
never the full price of the alternative — and the planned path is priced from
the shipment's own remaining legs, not by re-searching the graph. Pricing the
plan by searching finds the fastest unblocked path and calls that the plan, so
a road reroute gets compared against a road plan and comes out free. That is
how a barge load gets trucked to Rotterdam at no apparent cost.

**Fallback.** When the graph has nothing left, `local_options()` asks a
narrower question — who is near enough to take this over today — and answers
it from the vendor directory plus plain geography, ranked by distance. A 3PL
handover is expensive by construction; it should only survive the veto when
the alternative is a missed date on a consignment worth protecting.

---

## 5. The screen

`api/static/fast.html`, `fast-route.html`, `fast.js`, `fast-route.js`, `fast.css`

**One decision on screen.** The most urgent lane is a card with three numbers
— decide within, running late by, consignments — and under it the actions.
Everything else is one line long or behind a disclosure triangle.

**Removed, deliberately**, and pinned by a test so a well-meaning merge cannot
put them back:

- the risk matrix — a 4×4 of probability against impact is a portfolio
  instrument; it cannot be read in the six minutes a planner has, and it never
  told them what to press
- the radar chart — same objection, less information
- the per-vehicle icon grids — 28 icons is a picture of the problem, not a
  decision; the count and the deadline carry what the icons carried, and the
  detail stays one click away on the v1 route page
- expected loss as a headline — still computed, still what the veto reads, no
  longer on screen

**The execution flow.** `Detect → Confirm → Act → Close out` is gone. Each
option is a button and clicking it runs it:

```
POST /api/v2/act  {route_id, option_id}   →   dispatched, running
```

What replaces the gate is an **undo window** (default 15 minutes, in
`fast.yaml`), with a live countdown, and the retraction goes out on the same
channels that carried the original.

This is strictly better than pre-confirmation for the case that actually
happens — a planner acting on a report that turns out to be wrong — because it
costs nothing when the report was right, which is most of the time, and the
checklist charged its full price every single time.

It is not free. The honest statement of the trade: executing on an unconfirmed
signal can commit spend against something that did not happen. So `confidence`
rides on every execution and is shown next to it, a low-confidence trigger is
labelled rather than blocked, and the undo window exists. The planner is
trusted with the decision — and told what they are trusting.

Three things are still refused, and none of them is a checklist:

- an option we do not own (`not ours`)
- an option the veto removed (`vetoed`)
- an option that has expired (`expired`)

and an **inbound hook can raise an incident and have the options computed, but
cannot execute anything**. That line is where the safety of instant execution
actually lives: the click is instant, but the click is a person's.

---

## API

| | |
|---|---|
| `GET  /api/v2/now` | headline + short queue + counts |
| `GET  /api/v2/route/{id}` | one lane: delay now, options, folded detail |
| `POST /api/v2/act` | execute a grouped option across the consignments it covers |
| `POST /api/v2/undo` | pull it back inside the window |
| `GET  /api/v2/stream` | SSE off the bus (`signal`, `disruption`, `action`, `field`, `undo`) |
| `POST /api/v2/hooks/incident` | carrier / TMS / port pushes in |
| `GET  /api/v2/incidents` | the live incident list |
| `GET  /api/v2/outbox` | what dispatch recorded but could not send |

The grouped `option_id` a client sends back is **re-derived server-side**
rather than trusted: a client that sent an edited list of consignment ids
would otherwise be executing against freight the engine never offered.

Set `RADAR_HOOK_TOKEN` to require a bearer token on the hook. Unset, the hook
still works — it is the only way to demo an integration offline — but
everything it raises is marked `authenticated: false` and the planner sees
that on the card.

---

## Running it

```bash
.venv/bin/python run.py serve       # binds 0.0.0.0 in a Codespace
# "/" is the decision surface; "/board" is the old globe.
```

If a page looks older than the commit you are on, do not guess:

```bash
.venv/bin/python scripts/verify_install.py 8000
```

It prints which commit is checked out, which features are in the files on
disk, and which are in the bytes the server is actually sending. Whichever
pair disagrees names the fix.

Zero cost and fully offline: every outbound channel is disabled by default and
records instead of sending, `RADAR_ALLOW_NETWORK` gates all egress, and no
model is called anywhere in this path.

```bash
pytest tests/test_fast_options.py tests/test_fast_pipeline.py tests/test_fast_api.py
```
