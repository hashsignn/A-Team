# The 4-minute pitch

`Supply_Chain_Risk_Radar_pitch.pptx` — ten slides, 16:9, with the timing for
each one in its speaker notes. It is generated, not hand-edited: change
`build_deck.py` and rebuild, so the deck and the screenshots never drift apart.

```
python docs/pitch/build_deck.py
```

## What it argues

The value proposition is **speed of action**: the freight reaches the customer
on the promised date even when the lane breaks. Cost is secondary to delivery —
but no option is proposed that turns a profit into a loss, which is why the
playbook lists only mitigations worth more than they cost and says so plainly
when none are.

## Structure

It follows The Pitch Guide (UZH Innovation Hub). The five content elements land
on slides 2 (a real customer experience — the Kaub low-water morning), 9 (the
business model and the go-to-market) and 8 (the competitor grid); the team and
the ask close it on slide 10. The performance rules are enforced in the notes:
a 15-second elevator pitch on slide 1, a running clock on every slide, and an
explicit instruction about what to cut first when the clock is against you.

## The screenshots

`img/` holds real captures of the running app in its Sika theme, taken by
Playwright against a local server at a pinned as-of date so they can be
regenerated identically:

```
python run.py serve --port 8099
# then capture against /?as_of=2026-09-16&shipments=220
```

The shipments in them are synthetic and the deck says so on its final slide.
The lanes, the chokepoints and the feeds behind them are real.
