# How risk is judged

Every event on the board is judged by **what is uncertain about it**, and
each of those questions is answered by a method that practitioners already
use. Nothing here is a new invention; the prototype's part is applying the
right method to the right question and saying which one it used.

| question | method | where |
|---|---|---|
| Does the report say it happened, that it might, or that it is over? | ConText (clinical text processing) | `engine/variables/modality.py` |
| What kind of event is it? | sudden-onset / slow-onset hazards (UN) | `engine/variables/onset.py` |
| Will this shipment still arrive on the promised date? | Time-to-Recover vs Time-to-Survive | `engine/score/survive.py` |
| Is a warning worth acting on? | the cost–loss ratio (weather services) | `engine/score/survive.py` |
| How exposed is each shipment? | likelihood × impact matrix (ISO 31000 / IEC 31010) | `engine/score/matrix.py` |
| How much is at stake across the book? | three-point estimates + Monte Carlo (PMI, AACE) | `engine/simulate/draws.py` |

---

## 1. Four kinds of event

Disaster science separates **sudden-onset** hazards (an earthquake, a fire)
from **slow-onset** ones (a drought, a falling river) — the UN's own
terminology (UNDRR). Freight adds two cases of its own. Each kind leaves a
different question open:

| kind | examples | known | open question | judged by |
|---|---|---|---|---|
| **Sudden impact** | quake, fire, collision, cyber outage, a blockade in force | that it happened, where, when | how long | Time-to-Survive against the judged delay |
| **Scheduled** | Autobahn roadworks, rail works, public holidays, blank sailings | start and end | overrun | the stated window caps the delay |
| **Building up** | the Rhine at Kaub, a growing port queue, a storm forecast | a number and its trend | whether and when it bites | the measurement or the forecaster's confidence |
| **Warning sign** | a strike ballot, a threat to close a strait | only that a stoppage is likelier | whether, when, how long | the cost–loss break-even, and lead time |

Every risk type in `config.example/variables.yaml` states how it usually
arrives (`onset: sudden | slow | scheduled | precursor`). The event's kind is
decided from **evidence first, the ledger second**: a road closure is
usually sudden, but the Autobahn feed lists roadworks with their dates, so
those are scheduled.

## 2. Has it happened? Reading the words (ConText)

The keyword router finds a Hormuz closure in *"Iran threatens to close the
Strait of Hormuz"* and in *"Iran closes the Strait of Hormuz"* alike. It
answers what a report is about, not whether the report says it is so.
Before this, a threat went on the board priced as the closure.

The reading uses **ConText** (Harkema, Dowling, Thornblade & Chapman,
*Journal of Biomedical Informatics*, 2009). It's the standard rule-based way
of reading clinical notes for whether a finding is asserted, hypothetical or
negated. A trigger phrase modifies the concept after it, or before it for a
post-trigger, up to the end of its clause:

    Iran [threatens to] close the Strait of Hormuz        warning sign
    Dockworkers vote to strike from Monday [unless] …      warning sign
    The strike at Antwerp was [called off]                 over: not an event
    The strike [could] be [called off]                     still on: the ENDING is hypothetical
    … redirected 115 vessels since … its blockade          in force

It is conservative about "over", the only reading that removes something
from the board. Only completed forms count ("called off", "reopened",
"lifted"). "The closure ends on Friday" states an end; it doesn't report
one. The trigger that decided is shown on the route page, so a planner can
overrule it at a glance.

A warning is then treated as one. It has not happened, and no odds are
invented. When the report gives no date, it starts where the stoppage it
warns of usually would: the risk type's `typical_lead_time_hours` in the
ledger. A ballot reported today no longer puts every shipment through that
port on a six-hour clock.

## 3. Will the shipment still be on time? Time-to-Survive

Simchi-Levi, Schmidt and Wei (*"From Superstorms to Factory Fires"*, Harvard
Business Review, 2014 — the risk-exposure work done with Ford) set aside the
probability of a rare disruption, which nobody can estimate, and ask two
questions instead:
- **Time-to-Recover:** how long until it's fixed.
- **Time-to-Survive:** how long we can keep our promises without it.

Where recovery takes longer than survival there is exposure, whatever the
odds. ISO 22301 (business continuity) asks the same question as the maximum
tolerable period of disruption.

For each shipment and event:

- **Time-to-Survive** is the most delay the event can add before the
  shipment misses the date promised to its customer. That's the buffers on
  the legs from where the event hits, plus the gap between planned arrival
  and the promised date. It's computed with exactly the walk the simulation
  makes, so the two can never disagree.
- **Time-to-Recover** is the event's three judged points from
  `delay_model.yaml`: best, likely, worst.

The verdict a planner can check against what they know:

    late even in the best case      best   > survive
    late in the likely case         likely > survive ≥ best
    late only in the worst case     worst  > survive ≥ likely
    on time even in the worst case  survive ≥ worst

**A scheduled closure is waited out, never longer.** A closure of a road,
rail line, bridge, tunnel or lock with a stated end delays a shipment at
most until that end. The judged points are capped at the time left in the
window when the shipment gets there. Before this, a two-night A61 closure
carried the infrastructure family's worst case of five days and turned a
route red ("notify the customer now"); now its shipments show on time even
in the worst case. Strikes are not capped: a port that stops for two days
takes longer than two days to clear.

## 4. Is a warning worth acting on? The cost–loss ratio

For a warning whose odds nobody can price, weather services use a standard
rule for acting on warnings (Thompson 1952; Murphy 1977). Take the
protective action when

    P(it happens)  >  C / L   =  cost of acting / loss it avoids

The board shows that **break-even probability**, not a probability. It's
phrased in the words of ICD 203, the US intelligence community's standard
for stating likelihood:

> Worth acting if you judge it more than **12%** likely, which is **very unlikely** on the ICD 203 scale.

A planner can judge whether a strike ballot is "very unlikely". Nobody can
defend "the ballot is 37% likely". Nothing is invented, and the decision is
still computable.

## 5. The matrix: likelihood × impact (ISO 31000 / IEC 31010)

The per-event matrix plots each shipment by P(late) against the bill if it is
late. An event nobody publishes odds on has its own column outside the
likelihood axis, never a made-up 0.5. **Fixed with this method:** real feeds
state no probability for events that have *already happened* (a listed
closure, a measured earthquake), because none is left to state. Reading that
absence as "odds unknown" put every real event in the no-odds column. An
event that has happened now has known odds of 1, and only warnings sit in
that column.

## 6. The book: three-point estimates and Monte Carlo (PMI, AACE)

Combining judged three-point estimates by Monte Carlo is the
project-management standard for schedule risk (PMI's quantitative risk
analysis; AACE International RP 57R-09). It stays for what only it does well:
- the total across all shipments;
- events shared between shipments, drawn once per iteration so forty
  shipments through Antwerp feel the same strike.

A warning is priced **as if it happens** (its odds are unknown, so every
figure is conditional). The crisis-meeting headline says how much of the
exposure is only warnings. Whether warnings count toward the rule is the
team's call (`scoring.yaml → convene_rule.count_warnings`, default yes,
because convening early is the rule's whole purpose).

## What is still assumed, and how Sika replaces it

| assumption | file | how to replace it |
|---|---|---|
| delay if it hits (best / likely / worst) per family and severity | `delay_model.yaml` | a workshop with the planners: "a strike at Antwerp costs us about three days" |
| typical lead time and duration per risk type | `variables.yaml` | the same workshop, or history once there is some |
| cost and residual delay of each action | `engine/act/playbook.py` | carrier quotes |
| forecast confidence falling with lead time | `engine/ingest/weather.py` | the forecaster's ensemble spread |

Not built yet, and the next steps if the method holds:
- **An indicators-and-warnings ladder** per warning type. For Hormuz:
  1. rhetoric
  2. naval moves
  3. war-risk insurance listing
  4. carrier diversion advisories
  5. closure

  Each step seen raises the likelihood.
- **Reference-class durations** from past events (port strikes, canal
  closures), to turn "judged" into "sourced".
- **Kaub persistence from decades of gauge history:** the chance the Rhine
  stays low for N more days given today's level and trend.
