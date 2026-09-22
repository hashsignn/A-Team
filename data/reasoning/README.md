# Recorded model answers

Files here are **committed on purpose**. Each is a map from a content hash of
`(stage, system prompt, prompt)` to the answer a model gave, with the model's
name and the date.

```
laptop with a model  →  record once  →  commit  →  every other machine replays
```

## Record

```bash
RADAR_RECORD_REASONING=1 .venv/bin/python scripts/record_reasoning.py --as-of 2026-09-16
git add data/reasoning && git commit -m "Record the reasoning layer"
```

## Replay

Nothing to do. Any run that finds a recording for a question uses it and marks
the answer `replayed`, with the date and the model that produced it. The
Signals panel says so on screen.

## Re-record when the prompt changes

The key includes the system prompt, so an edited prompt **misses** rather than
replaying answers to the question it used to ask. That is the correct
behaviour — it is also why the counts drop the first time you run after
editing one.

## What this is not

It is not a model in a file. A headline outside the recording misses, and with
no model present the deterministic router handles it alone — the same
supported state as before. This carries a *rehearsed run*, not a capability.
