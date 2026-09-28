"""Bursts of small orders: the pattern Sika sees a week or so before a crisis.

Sika, describing what the desk notices first:

    "A week or so before a crisis begins, small shipments that normally go
     now and then start going in a single day."

It is the customer's own hands moving: sites and customers who have heard
something (a strike, a tariff, a port closing, a river falling) pull their
next few orders forward and send them as soon as they can, in whatever size
is ready. So on one day a flow carries many more orders than usual, and most
of them are smaller than usual. Nothing has been announced yet; the order
book is the first place it shows. Advisory work lists the same sign
("customers placing smaller, more frequent orders, forward buying, unusual
timing": Bedford Consulting). scripts/check_order_bursts.py checks it
against Sika's own order book, on the machine that has the export, by
listing each burst with the public event that followed it.

WHAT COUNTS
-----------
Per flow (a Sika country-to-country flow, e.g. CH_US), per working day:

    orders    >= min_orders
    z         (orders - usual) / spread over the previous baseline_days
              working days  >= min_z
    small     share of that day's orders below the usual order size (the
              baseline's median)  >= small_share

A burst inside the last ``window_days`` before the as-of is raised on the
flow's route as an early warning: the route is at least Bias ("monitor
closely, decide within 5 days"), with the burst as its reason. It is not a
disruption and carries no probability or delay: it says "something is
coming", not what.

WHERE THE ORDERS COME FROM
--------------------------
``config/orders_daily.yaml`` when it exists (gitignored: Sika's order book),
written by scripts/import_sika_flows.py from the export's creation dates and
net weights. Without it, a SAMPLE history is generated for the focus flows,
labelled so, with one burst a week before the as-of on the Switzerland to
China flow, because a signal nobody can see is a signal nobody believes.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from engine.clock import Clock
from engine.config import CUSTOMER_DIR, Config
from engine.ingest.observations import FeedReport, FeedStatus

ORDERS_FILE = "orders_daily.yaml"
SAMPLE_BURST_FLOW = "CH_CN"


@dataclass(frozen=True)
class Rule:
    window_days: float = 10
    baseline_days: int = 20
    min_orders: int = 4
    min_z: float = 3.0
    small_share: float = 0.6
    # A flow that was nearly idle (the weeks after New Year) makes any
    # ordinary day look like a burst; below this many orders in the
    # baseline there is no "usual" to beat.
    min_baseline_orders: int = 10


def rule(config: Config) -> Rule:
    spec = (config.desk or {}).get("order_bursts") or {}
    return Rule(
        window_days=float(spec.get("window_days", 10)),
        baseline_days=int(spec.get("baseline_days", 20)),
        min_orders=int(spec.get("min_orders", 4)),
        min_z=float(spec.get("min_z", 3.0)),
        small_share=float(spec.get("small_share", 0.6)),
        min_baseline_orders=int(spec.get("min_baseline_orders", 10)),
    )


def known_events(config: Config) -> list[dict]:
    spec = (config.desk or {}).get("order_bursts") or {}
    out = []
    for e in spec.get("known_events") or []:
        try:
            when = date.fromisoformat(str(e["date"]))
        except (KeyError, ValueError):
            continue
        out.append({"date": when, "what": str(e.get("what", "")), "source": str(e.get("source", ""))})
    return out


# ---------------------------------------------------------------------
# The order history: flow -> day -> the size (kg) of each order that day
# ---------------------------------------------------------------------
History = dict[str, dict[date, list[float]]]


def read_file(path: Path) -> History:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: History = {}
    for flow, days in (data.get("flows") or {}).items():
        series: dict[date, list[float]] = {}
        for day, sizes in (days or {}).items():
            when = day if isinstance(day, date) else date.fromisoformat(str(day))
            series[when] = [float(x) for x in sizes or []]
        out[str(flow)] = series
    return out


def _working_days(end: date, count: int) -> list[date]:
    out, day = [], end
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


def sample_history(flows: list[str], clock: Clock, days: int = 70) -> History:
    """A labelled stand-in: a normal trickle of orders on each flow, and one
    burst of small orders a week before the as-of on SAMPLE_BURST_FLOW."""
    today = clock.as_of.date()
    rng = random.Random(f"order-bursts-{today.isoformat()}")
    calendar = _working_days(today, days)
    burst_day = calendar[-6] if len(calendar) >= 6 else calendar[0]
    out: History = {}
    for flow in flows:
        series: dict[date, list[float]] = {}
        for day in calendar:
            n = sum(1 for _ in range(4) if rng.random() < 0.38)   # about 1.5 a day
            if flow == SAMPLE_BURST_FLOW and day == burst_day:
                series[day] = [round(rng.lognormvariate(5.4, 0.5), 1) for _ in range(9)]
                continue
            if n:
                series[day] = [round(rng.lognormvariate(6.8, 0.8), 1) for _ in range(n)]
        out[flow] = series
    return out


# ---------------------------------------------------------------------
# Finding the bursts
# ---------------------------------------------------------------------
def _day_burst(series: dict[date, list[float]], day: date, r: Rule) -> dict | None:
    sizes = series.get(day) or []
    if len(sizes) < r.min_orders:
        return None
    base_days = _working_days(day - timedelta(days=1), r.baseline_days)
    counts = [len(series.get(d) or []) for d in base_days]
    base_sizes = [k for d in base_days for k in series.get(d) or []]
    if len(base_sizes) < r.min_baseline_orders:
        return None
    usual = statistics.mean(counts)
    spread = max(statistics.pstdev(counts), 0.5)
    z = (len(sizes) - usual) / spread
    typical = statistics.median(base_sizes)
    small = sum(1 for k in sizes if k < typical) / len(sizes)
    if z < r.min_z or small < r.small_share:
        return None
    return {
        "day": day.isoformat(),
        "orders": len(sizes),
        "usual": round(usual, 1),
        "times": round(len(sizes) / usual, 1) if usual > 0 else None,
        "z": round(z, 1),
        "small_share": round(small, 2),
        "size_ratio": round(statistics.median(sizes) / typical, 2) if typical > 0 else None,
    }


def detect(history: History, clock: Clock, r: Rule) -> list[dict]:
    """Bursts inside the window before the as-of, newest first, per flow."""
    today = clock.as_of.date()
    since = today - timedelta(days=r.window_days)
    out = []
    for flow, series in sorted(history.items()):
        for day in sorted((d for d in series if since <= d <= today), reverse=True):
            hit = _day_burst(series, day, r)
            if hit:
                out.append({"flow": flow, **hit, "days_ago": (today - day).days})
    return out


def backtest(history: History, r: Rule, events: list[dict], lead_days: int = 14) -> list[dict]:
    """Every burst in the whole history, each with the known event that
    followed it within ``lead_days``, if any: the check that the sign comes
    before the crisis."""
    out = []
    for flow, series in sorted(history.items()):
        for day in sorted(series):
            hit = _day_burst(series, day, r)
            if not hit:
                continue
            after = [e for e in events if 0 <= (e["date"] - day).days <= lead_days]
            follow = min(after, key=lambda e: e["date"]) if after else None
            out.append({"flow": flow, **hit, "followed_by": (
                {"date": follow["date"].isoformat(), "what": follow["what"], "source": follow["source"],
                 "days_later": (follow["date"] - day).days} if follow else None)})
    return out


def sentence(burst: dict, flow_label: str) -> str:
    usual = burst["usual"]
    return (f"Early warning: {burst['orders']} orders in one day on {flow_label} "
            f"({burst['day']}), against a usual {usual:g} a day, and "
            f"{burst['small_share']:.0%} of them smaller than usual. Sika sees this "
            "about a week before a crisis.")


def assess(config: Config, clock: Clock, flows: list[str],
           customer_dir: Path | None = None) -> tuple[FeedReport, dict]:
    """Read the order history and find the bursts: (report, summary)."""
    path = (customer_dir or CUSTOMER_DIR) / ORDERS_FILE
    r = rule(config)
    if path.exists():
        history = read_file(path)
        status = FeedStatus.CONNECTED
        last = max((d for s in history.values() for d in s), default=None)
        detail = f"{sum(len(s) for s in history.values())} flow-days of orders from {path.name}"
        if last is not None and last < clock.as_of.date() - timedelta(days=r.window_days):
            # Honest about a book that stops before the as-of: nothing recent
            # to read is not the same as nothing happening.
            detail += f" (the book ends {last.isoformat()}: nothing recent to read)"
        synthetic = False
    else:
        history = sample_history(flows, clock)
        status = FeedStatus.FIXTURE
        detail = ("SAMPLE order history for the focus flows, with one burst a week before "
                  "the as-of; run scripts/import_sika_flows.py to read Sika's own")
        synthetic = True
    current = detect(history, clock, r)
    report = FeedReport(
        key="order_bursts",
        label="Order pattern (bursts of small orders)",
        status=status,
        detail=f"{detail}; {len(current)} burst(s) in the last {r.window_days:.0f} days",
        unlocks_if_connected=(
            "Sika's own order book, day by day: a burst of small orders is the sign "
            "Sika sees about a week before a crisis."
        ),
        records=sum(len(v) for s in history.values() for v in s.values()),
        retrieved_at=clock.as_of,
        source_tier=2,
    )
    summary = {
        "bursts": current,
        "history": backtest(history, r, known_events(config)) if not synthetic else [],
        "synthetic": synthetic,
        "rule": (f"{r.min_orders}+ orders in one day, at least {r.min_z:g} spreads above the "
                 f"usual of the previous {r.baseline_days} working days, and "
                 f"{r.small_share:.0%}+ of them smaller than usual"),
        "as_of": clock.as_of.date().isoformat(),
    }
    return report, summary
