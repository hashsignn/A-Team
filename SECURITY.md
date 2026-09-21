# Security, cost and audit rules

Three constraints govern this pipeline. Each is enforced by something that
runs, not by a paragraph asking people to be careful.

---

## 1 · It costs nothing to run, and proves it offline

**The radar needs no credential and no network to produce a complete board.**

```bash
python run.py demo           # full pipeline, pinned as-of, no network
python run.py inputs         # what is real, standing in, and absent
python run.py serve          # the dashboard
pytest -q                    # the whole suite
```

Nothing above reaches the internet. That is not a fallback mode — it is the
normal one, and it is what makes the system demonstrable on a locked-down
laptop, in a sandbox whose egress proxy blocks every data host (which is
exactly where this was built), and in CI.

### How each external dependency is stubbed

| Dependency | Cost | How it runs at zero cost |
|---|---|---|
| Rhine gauge (Pegelonline) | free, blocked here | `engine/ingest/watergauge.py` falls back to a shaped reconstruction, **labelled as one** on the inputs panel |
| Weather (Open-Meteo, Copernicus) | free, blocked here | declared absent; the socket says what connecting it unlocks |
| News / trade press | free | 10 synthetic items, written to exercise the gate — including items that correctly match nothing |
| Order book | n/a | `generate_shipments()` produces a synthetic book, flagged `synthetic: true` everywhere it surfaces |
| Reasoning model | free locally | Ollama, or **nothing at all** — `RADAR_LLM_BACKEND=none` runs the deterministic router alone |
| TMS | licensed | never POSTed to. The payload is exposed at `GET /api/v1/shipment-alerts` for inspection |

A stand-in is **never silently substituted for real data.** Every one carries
its status into the UI, the CLI and the profile page, in three states —
connected / example stand-in / absent — and an absent feed states what
connecting it would buy. A number whose provenance is invisible is a number
somebody will eventually quote in a meeting.

### The dry-run flag

`RADAR_DRY_RUN=1` is the default in `.env.example` and means *refuse to send
anything, whatever else is configured*. Leave it on until somebody has
deliberately turned it off, in a change a reviewer can see.

---

## 2 · No secret exists in this repository, and a test says so

**The radar has no credential to leak.** Every feed it uses is public or
stubbed; the model runs locally. The only secrets in the picture belong to
*integrations* — the TMS you POST to, the optional API model — and they live
in the environment of the process that uses them.

### Enforced, not requested

`tests/test_tms.py` runs on every commit:

| test | what it prevents |
|---|---|
| `test_no_alert_payload_carries_anything_credential_shaped` | a token serialised into every alert and shipped to a system that logs request bodies |
| `test_the_audit_actually_catches_a_planted_secret` | the audit silently degrading into a no-op |
| `test_no_endpoint_or_hostname_is_baked_into_a_payload` | internal hostnames escaping in freight data |
| `test_the_committed_config_contains_no_secrets` | a real key pasted into `config.example/`, which is public |
| `test_gitignore_covers_the_files_that_would_carry_credentials` | the gap that let it happen |

The audit (`engine/export/tms.py::audit_for_secrets`) walks a payload for
credential-shaped keys and values — `sk-`, `ghp_`, `AKIA`, `eyJ`, and the
rest — and is itself tested against planted secrets, because a check that
cannot fail is not a check.

> That last test **failed the first time it ran**: `.env` was not in
> `.gitignore`. It is now. That is the entire argument for writing these as
> tests rather than as a checklist.

### `.env` management

```bash
cp .env.example .env     # .env is gitignored; .env.example is committed and empty
```

`.gitignore` covers `.env`, `.env.*` (with `!.env.example` re-included),
`*.pem`, `*.key`, `*_rsa`, `credentials.json`, `service-account*.json` and
`secrets.y*ml`. `config/` — the customer's real data — has been ignored since
the first commit; `config.example/` is the synthetic public stand-in and is
the only one that ships.

**If a secret is ever committed, rotate it.** Removing it in a later commit
does not remove it from the history, and `git filter-repo` does not remove it
from anyone's existing clone. Rotation is the only fix.

### Why the radar does not POST to the TMS itself

A separate process does, reading `TMS_BEARER_TOKEN` from its own environment.
The credential therefore never enters the radar's address space, never
appears in a traceback, and never reaches the LLM context — which matters
because the assistant serialises board data into a prompt, and a token that
had leaked into the board would leak into the model.

---

## 3 · Code audit rules for anything added to this pipeline

1. **No hardcoded endpoint, key, token or internal hostname.** Read from the
   environment, and name the variable in `.env.example` with a comment saying
   what it is for.
2. **No external call without a stub.** If it cannot run offline it cannot
   run in CI, and a test that only passes with network access is a test that
   will be deleted.
3. **No absence becomes a value.** `None` plus a reason, never an imputed
   0.5. There are tests for this at the schema, the matrix and the payload.
4. **Nothing in `engine/` reads the wall clock.** An AST-walking test
   enforces it. Reproducibility is what makes the hindcast possible.
5. **Generated artefacts are generated.** `schemas/event_radar_taxonomy.json`
   comes from `scripts/gen_schema.py`, and `--check` fails CI when it is
   stale. A schema that disagrees with the runtime is worse than none.
6. **A recommendation that cannot legally be executed must be suppressed,
   not ranked low.** See `suppressed_actions` — offering air freight for a
   Class 3 solvent costs a planner the hours they had left.

### If you add a Dockerfile

None is committed, because none is needed — the install is seven packages and
`python run.py demo` works from a clean checkout. Should one be added:

```dockerfile
# Never ARG or ENV a secret: both are readable in `docker history`.
# Mount at runtime instead.
#   docker run --env-file .env  (with .env gitignored)
# or, better, a secret mount:
#   RUN --mount=type=secret,id=tms_token ...
USER nonroot              # never run as root
# no COPY .env, no COPY config/ — both are gitignored and neither belongs
# in an image layer
```
