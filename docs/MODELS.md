# The reasoning layer: models, sources, and the noise filter

## TL;DR — is anything missing?

The funnel and the two-model layer were **built and running**; what was
missing was any way to *look* at them. They showed as one line of small print
under the routes table and a tooltip on the Ask button. There is now a
**Signals** tab beside "Affected routes" on the board that shows the whole
filter layer, stage by stage, with the rule beside each count.

Nothing here is required. With no model installed the deterministic router
runs alone, the board is complete, and the panel says so rather than
pretending.

---

## 1. Which models, and why two

| stage | job | default | why |
|---|---|---|---|
| **triage** | one yes/no per headline — *could this touch freight?* | `qwen2.5:1.5b-instruct` (~1.0 GB) | Hundreds of items per run. A 7B reading each of them is most of the cost of the funnel for none of the judgement. |
| **extract** | read the survivors, return structured JSON | `qwen2.5:7b-instruct` (~4.7 GB) | Where the hard reading is: a conditional ("unless talks resume"), a second-order effect, a stated duration that contradicts the headline. |

Why qwen2.5 specifically:

- it follows a JSON schema **without a grammar**, which matters because the
  extraction step validates into a Pydantic model and a malformed answer is
  thrown away rather than repaired
- it is **multilingual** — Rhine notices are German, port notices are Dutch,
  and an English-only model silently drops half the corpus
- 7B fits in the ~5 GB a planner's laptop can spare beside everything else
  they run

Both are overridable:

```bash
export RADAR_TRIAGE_MODEL=llama3.2:3b
export RADAR_EXTRACT_MODEL=mistral-small:22b
```

A single-model install works: leave both unset and everything uses
`RADAR_LOCAL_MODEL` (`qwen2.5:7b-instruct`).

### If you are recording, go bigger

The defaults above are sized for a planner running the funnel **live, every
day** — that is the case where a 7B reading hundreds of headlines is the wrong
trade.

**Recording inverts that.** The model runs once, on one machine, and every
later run replays the answers for free. Speed stops mattering and quality
stops being expensive, so the right size is whatever your laptop can hold:

```bash
export RADAR_TRIAGE_MODEL=qwen2.5:7b-instruct     # ~4.7 GB
export RADAR_EXTRACT_MODEL=qwen2.5:14b-instruct   # ~9 GB
```

| model | RAM it wants | on a laptop |
|---|---|---|
| `qwen2.5:1.5b-instruct` | ~2 GB | instant |
| `qwen2.5:7b-instruct` | ~6 GB | comfortable on 16 GB |
| `qwen2.5:14b-instruct` | ~10 GB | fine on 16 GB, better on 32 |
| `qwen2.5:32b-instruct` | ~20 GB | needs 32 GB, slow without a GPU |

A recording pass over one as-of is a few hundred calls. At 14B on CPU that is
tens of minutes — once. The Codespace still needs nothing.

`check_models.py` checks whatever the environment is set to, so set the
variables first and it will look for the models you actually chose.

---

## 2. Do I already have them? — step by step

One command answers all four questions and, on the first thing that is wrong,
prints the command that fixes it:

```bash
.venv/bin/python scripts/check_models.py
```

It checks, in order:

1. **Is Ollama installed?** — `ollama --version`
2. **Is it answering?** — `GET $OLLAMA_HOST/api/tags` (default
   `http://localhost:11434`)
3. **Are the two stage models pulled?** — exact tag first, then the bare name,
   because `qwen2.5:7b` and `qwen2.5:7b-instruct` are different models
4. **What does the radar itself see?** — `llm.detect()`, the same call the
   board makes

By hand, if you prefer:

```bash
ollama --version                                   # 1
curl -s localhost:11434/api/tags | python -m json.tool   # 2 and 3
curl -s localhost:8000/api/model | python -m json.tool   # 4, with the server up
```

### If you do not have it

```bash
curl -fsSL https://ollama.com/install.sh | sh      # Linux / WSL
brew install ollama                                # macOS
ollama serve                                       # if not a service

ollama pull qwen2.5:1.5b-instruct                  # triage,  ~1.0 GB
ollama pull qwen2.5:7b-instruct                    # extract, ~4.7 GB
```

### On Windows

`cmd.exe` and PowerShell differ from the Unix commands above in three ways
that all produce confusing errors rather than useful ones:

| | Unix | Windows |
|---|---|---|
| venv python | `.venv/bin/python` | `.venv\Scripts\python` |
| create venv | `python3.11 -m venv .venv` | `py -3.11 -m venv .venv` |
| set a variable | `VAR=1 cmd` | `set VAR=1` on its own line |

And **`cmd.exe` does not strip `#` comments.** Pasting

```
ollama pull qwen2.5:7b-instruct   # extract, ~4.7 GB
```

gives `Error: accepts 1 arg(s), received 5` — which reads like Ollama is
broken when in fact it is installed and working, and only the comment is
wrong. Paste the command without the comment.

Full sequence:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt

ollama pull qwen2.5:1.5b-instruct
ollama pull qwen2.5:7b-instruct
.venv\Scripts\python scripts\check_models.py

set RADAR_RECORD_REASONING=1
.venv\Scripts\python scripts\record_reasoning.py --as-of 2026-09-16
```

`scripts/check_models.py` is platform-aware: the commands it prints when
something is wrong are the ones for the machine it is running on.

### In a Codespace

Two things bite:

- **Disk.** A default Codespace has ~32 GB and the image already uses a good
  chunk. 5.7 GB of models fits, but check `df -h /workspaces` first.
- **RAM.** A 2-core / 8 GB Codespace runs the 7B, slowly. If it thrashes, run
  triage-only and leave extraction off:

  ```bash
  ollama pull qwen2.5:1.5b-instruct
  export RADAR_EXTRACT_MODEL=qwen2.5:1.5b-instruct
  ```

  The funnel still works; the extraction is just blunter, and the challenger
  will disagree with it more often — which is visible, and the point.

### The API path

Set `ANTHROPIC_API_KEY` and the API backend becomes available. **Local still
wins when both are present** — the order book stays on the machine unless you
set `RADAR_LLM_BACKEND=api` deliberately.

---

## 3. What the noise filter actually is

Four layers, and **three of them are arithmetic**:

| stage | what it removes | model? |
|---|---|---|
| **Arrived** | — | no |
| **Near our freight** | outside the bounding box of every node we touch | no |
| **A kind that can hurt us** | no risk vocabulary matched a named variable family | no |
| **While we are there** | the window does not overlap any leg's transit | no |
| **Distinct events** | many reports, one event — clustered by place, kind, window | no |
| **Read by a model** | — | **yes** |

A typical run: **27 arrived → 9 distinct events → 11 model reads.** The three
deterministic filters remove two thirds of the corpus before anything costs a
call.

This is deliberate and the reasons are not cost alone:

- **reproducibility** — a hindcast over two years of archived feeds has to
  give the same answer twice
- **silence** — a model that drops an item leaves no trace; a threshold leaves
  a number
- **cost** — at local prices thousands of calls is an afternoon; the filter
  makes it dozens

The triage model **fails open, may only REMOVE, is bounded, and overflow
passes through unfiltered**. It can never add an item, and it can never
promote one.

---

## 4. News and event ingestion

Eleven free sources ship built in, no key needed for nine of them:

| source | nature | tier |
|---|---|---|
| GDELT — global news index | report | 2 |
| GDACS — disaster alerts (EU JRC) | report | 1 |
| ReliefWeb — situation reports (UN OCHA) | report | 2 |
| CISA KEV — exploited vulnerabilities | report | 1 |
| Autobahn A5 / A61 / A3 — closures (BASt) | report | 1 |
| USGS — earthquakes M4.5+ | **instrument** | 1 |
| Open-Meteo — marine wave height | **instrument** | 1 |
| ENTSO-E — grid outages | report | 1 (key) |
| OpenSanctions — consolidated lists | report | 1 (key) |

**`nature` is the field that decides who pays for a model call.** An
*instrument* is a measured number — a gauge reading, a wave height, a
magnitude. It goes straight to a threshold in `thresholds.yaml` and **never
reaches a model**. A *report* is somebody saying a thing, and only reports go
through the funnel.

That is the encoding of "it is not built for events we can predict with an
API like weather": weather and water are instruments, and the model never
sees them.

Egress is opt-in:

```bash
export RADAR_ALLOW_NETWORK=1
```

Without it every source serves its recorded fixture and the source panel says
`stand-in` rather than `connected`. Custom sources are declared in
`config.example/sources.yaml` — YAML, no code, and the file may name an
**environment variable** for a key but never a key.

---

## 5. Social media — volume, not voices

`engine/ingest/sources/chatter.py`

One post saying a lock is shut is not evidence. Forty posts from thirty
different accounts inside six hours, all naming the same place and the same
kind of disruption, is a different object — nothing about any individual post
changed, the **shape** changed, and shape is measurable without reading a
word.

```
every post     →  bucket by (place, disruption kind, fixed time window)
bucket         →  count DISTINCT authors, not posts
over threshold →  ONE synthesised item enters the funnel
under          →  nothing enters, and the count is still reported
```

Three defences, each against a specific way social lies about how many people
are saying something:

| attack | defence |
|---|---|
| one account posting forty times | count **distinct authors**, not posts |
| forty accounts reposting one claim | a repost carries the **original's** id; the reposter is not a witness |
| two unrelated events pooling | bucket by **(place, kind, window)**, not by keyword |

Windows are **fixed**, not sliding: a sliding window makes the same corpus
bucket differently depending on which post you start from, and a hindcast has
to be reproducible.

What comes out says what it **is** — *"20 accounts reporting blocked near
GAUGE_KAUB in 6 h"* — not what it claims. It enters at **tier 3 and stays
there**. Volume is a reason to look, never a reason to believe. Promotion to
tier 2 happens the way it always does: when an independent source says the
same thing.

No social source is wired by default. The gate is built and tested; pointing
it at a feed is a `sources.yaml` entry plus a normaliser that produces
`Post(post_id, author, text, at, repost_of)`.

---

## 6. Record once, replay anywhere

`engine/reason/cache.py`, `scripts/record_reasoning.py`

**You do not need a model in the Codespace.** A model's answer to a fixed
prompt about a fixed headline is a fact about that pair, not about the machine
that asked — so record it once where a model exists, commit it, and every
other machine replays it for free.

```bash
# on the laptop that has Ollama
RADAR_RECORD_REASONING=1 .venv/bin/python scripts/record_reasoning.py --as-of 2026-09-16
git add data/reasoning && git commit -m "Record the reasoning layer"
git push

# in the Codespace, on stage, on a train
python run.py serve     # no model, no egress, same board
```

### The key is the question, not the machine

Hashed from `(stage, system prompt, prompt)`. It deliberately does **not**
include the model name — the replaying machine has no model, so a key naming
one could never hit. The model name is recorded in the *value*, because
*"qwen2.5:7b said this on the 14th"* and *"a frontier model said this"* are
different claims and a planner is entitled to know which they are reading.

### An edited prompt misses

The system prompt is part of the key. Change it and every recorded answer
becomes an answer to a different question, so it misses and says so. The
alternative — silently replaying answers to the old prompt — is the worst
failure available here, because it looks exactly like the new prompt working.

### A replay is labelled as one

Every hit comes back marked `replayed:recorded 2026-09-14 by qwen2.5:7b-instruct`,
and the Signals panel shows it as its own state, in its own colour, between
"a model is reading this" and "nothing is". An answer whose age is invisible
is an answer nobody can check.

### Recording is off by default

`RADAR_RECORD_REASONING` must be set. A demo machine must never quietly write
answers into the repository.

### What it is not

It is not a model in a file. A headline outside the recording misses, and with
no model present the deterministic router handles it alone. This carries a
rehearsed run, not a capability.

### So: where do I install the models?

**On your own machine, not in the Codespace.** Pull them once, record, commit,
and the Codespace never needs them. Install in the Codespace only if you want
to reason over *new* events there — and then mind the RAM note above.
