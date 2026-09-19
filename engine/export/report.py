"""The convening pack: a PDF and a plain-text summary for one route.

BRIEF §9.2 lists a per-event report. Sika's answer to Q6 changes what it is
for: not a customer-service handout, but the evidence pack that justifies
pulling the standing teams out of their weekly cycle. So it leads with the
deadline and who is being asked to convene, and puts the arithmetic behind it.

FONTS
-----
Built-in Helvetica, no vendored font file. Everything in the data is cp1252 —
umlauts, accents, em dashes and curly quotes all encode — with one exception,
the arrow in lane names, which is transliterated. Vendoring a Unicode TTF
would add several hundred kilobytes to carry a single glyph.

WHAT THIS DOES NOT DO
---------------------
It does not send anything. Sending is an outward-facing action and there is no
mail path wired, so the UI composes and hands over; the planner sends. That is
the socket pattern (§8.2): the surface is real and says plainly what is not
connected, rather than faking a delivery that never happened.
"""

from __future__ import annotations

import io
from datetime import datetime

from fpdf import FPDF

# Page geometry
MARGIN = 16
WIDTH = 210 - 2 * MARGIN

INK = (14, 18, 28)
MUTED = (110, 122, 145)
RULE = (208, 216, 230)

LEVEL_RGB = {
    "red": (206, 48, 48),
    "yellow": (196, 138, 10),
    "blue": (34, 96, 190),
    "white": (90, 102, 124),
    "green": (22, 140, 66),
}


# fpdf2's built-in fonts encode ISO-8859-1, which is NARROWER than cp1252: it
# carries the umlauts and accents but none of the typographic punctuation. Em
# dashes, en dashes, curly quotes and ellipses all appear in this data, and any
# one of them raises inside the font layer and takes the whole report with it.
#
# Keyed by codepoint rather than by literal character so the mapping survives
# being edited through tooling that mangles non-ASCII source.
_TRANSLITERATE = {
    0x2192: "->",   0x2190: "<-",
    0x2264: "<=",   0x2265: ">=",
    0x2014: "-",  0x2013: "-",    0x2011: "-",   0x2212: "-",
    0x2018: "'",    0x2019: "'",
    0x201C: '"',    0x201D: '"',
    0x2026: "...",  0x2022: "-",    0x00A0: " ",
}


def latin(text: str) -> str:
    """Fold a string down to what the built-in fonts can encode.

    The explicit map keeps the common cases readable ("->" rather than "?").
    The encode/decode at the end is the backstop: a stray glyph from a future
    feed becomes a question mark instead of a 500.
    """
    return (
        str(text)
        .translate(_TRANSLITERATE)
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


class _Pack(FPDF):
    def __init__(self, route: dict, as_of_label: str) -> None:
        super().__init__(format="A4")
        self.route = route
        self.as_of_label = as_of_label
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(MARGIN, MARGIN, MARGIN)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(*MUTED)
        self.cell(
            0, 4,
            latin(
                f"Supply Chain Risk Radar  ·  as of {self.as_of_label}  ·  "
                f"page {self.page_no()}  ·  ADVISORY — the planner decides  ·  "
                "figures from synthetic data"
            ),
            align="C",
        )


def _h(pdf: _Pack, text: str, size: float = 10.5, gap: float = 2.5) -> None:
    pdf.ln(gap)
    pdf.set_font("Helvetica", "B", size)
    pdf.set_text_color(*INK)
    pdf.cell(0, 5.5, latin(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.2)
    pdf.line(MARGIN, pdf.get_y() + 0.5, 210 - MARGIN, pdf.get_y() + 0.5)
    pdf.ln(2)


def _body(pdf: _Pack, text: str, size: float = 9, muted: bool = False) -> None:
    pdf.set_font("Helvetica", size=size)
    pdf.set_text_color(*(MUTED if muted else INK))
    pdf.multi_cell(WIDTH, 4.4, latin(text), new_x="LMARGIN", new_y="NEXT")


def _kv(pdf: _Pack, key: str, value: str) -> None:
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(46, 4.8, latin(key))
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*INK)
    pdf.multi_cell(WIDTH - 46, 4.8, latin(value), new_x="LMARGIN", new_y="NEXT")


def build_pdf(route: dict, as_of_label: str, posture: dict) -> bytes:
    """Render the convening pack for one route."""
    pdf = _Pack(route, as_of_label)
    pdf.add_page()

    level = route["level"]
    rgb = LEVEL_RGB.get(level, MUTED)

    # ---- banner: the deadline first ---------------------------------
    pdf.set_fill_color(*rgb)
    pdf.rect(MARGIN, MARGIN, 2.2, 17, style="F")
    pdf.set_xy(MARGIN + 5, MARGIN)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*rgb)
    pdf.cell(0, 4.5, latin(route["level_label"].upper()), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(MARGIN + 5)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*INK)
    pdf.multi_cell(WIDTH - 5, 6, latin(route["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(MARGIN + 5)
    pdf.set_font("Helvetica", size=9.5)
    pdf.set_text_color(*rgb)
    pdf.cell(0, 5, latin(route["directive"]), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    # ---- why ----------------------------------------------------------
    _h(pdf, "Why this is on the board")
    _body(pdf, route["reason"])

    pdf.ln(1)
    _kv(pdf, "Action by", _hours(route.get("lead_time_hours")))
    _kv(pdf, "Exposure", _chf(route.get("exposure_chf")))
    _kv(pdf, "Shipments at risk",
        f"{route['shipments_at_risk']} of {route['shipments']} on this route")
    _kv(pdf, "Customer contracts", ", ".join(route.get("contracts", [])) or "none")

    # ---- events -------------------------------------------------------
    if route.get("events"):
        _h(pdf, "What is happening")
        for event in route["events"]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*INK)
            pdf.multi_cell(WIDTH, 4.4, latin(event["title"]),
                           new_x="LMARGIN", new_y="NEXT")
            prob = (
                f"P {event['probability'] * 100:.0f}%"
                if event.get("probability") is not None
                else "probability unsourced"
            )
            _body(
                pdf,
                f"{event['severity']} · {event['event_class'].replace('_', ' ')} · "
                f"{prob} · {event['shipments_here']} shipment(s) · "
                f"{_chf(event['exposure_chf'])} · source: {event['source']} "
                f"(tier {event['source_tier']})",
                size=8, muted=True,
            )
            if event.get("quote"):
                pdf.set_font("Helvetica", "I", 8.5)
                pdf.set_text_color(*MUTED)
                pdf.multi_cell(WIDTH, 4.2, latin(f'"{event["quote"]}"'),
                               new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)

    # ---- options ------------------------------------------------------
    _h(pdf, "Options on the table")
    actions = route.get("actions", [])
    if not actions:
        _body(pdf, "No option currently saves more than it costs. Monitor.",
              muted=True)
    else:
        for action in actions[:6]:
            pdf.set_font("Helvetica", size=9)
            pdf.set_text_color(*INK)
            pdf.multi_cell(WIDTH, 4.4, latin(action["sentence"]),
                           new_x="LMARGIN", new_y="NEXT")
            _body(
                pdf,
                f"{action['shipment_id']} · {action['customer']} · "
                f"lead {_hours(action.get('lead_time_hours'))} · "
                f"needs {action['min_hours']:.0f} h · "
                f"lever held by {action['owner']}",
                size=8, muted=True,
            )
            pdf.ln(1.2)

    # ---- who ----------------------------------------------------------
    response = route.get("response", {})
    _h(pdf, "Who to involve")

    manager = response.get("route_manager")
    if manager:
        _kv(pdf, "Route manager",
            f"{manager['name']} — {manager['role']}"
            + (f" · {manager['phone']}" if manager.get("phone") else ""))

    for label, key in (
        ("Convene", "standing_teams"),
        ("Seniors", "seniors"),
    ):
        people = response.get(key, [])
        if people:
            _kv(pdf, label,
                "; ".join(f"{p['name']} ({p['role']})" for p in people))

    vendors = response.get("vendors", [])
    if vendors:
        _kv(pdf, "Local vendors on route",
            "; ".join(f"{v['name']} — {v['role']}" for v in vendors))

    carriers = response.get("carriers", [])
    if carriers:
        _kv(pdf, "Carriers", "; ".join(c["name"] for c in carriers))

    routing = response.get("alternate_routing", [])
    if routing:
        _kv(pdf, "Declared alternatives",
            "; ".join(
                f"{r['node_name']} -> "
                + ", ".join(a["name"] for a in r["alternatives"])
                for r in routing
            ))
    else:
        _kv(pdf, "Declared alternatives",
            "none configured for the nodes on this route")

    approval = response.get("approval")
    if approval:
        _body(pdf, approval["note"], size=8.5)

    # ---- portfolio context --------------------------------------------
    _h(pdf, "Portfolio context")
    _body(pdf, posture.get("headline", ""), size=8.5, muted=True)
    if not posture.get("rule_agreed", False):
        _body(pdf,
              "The convene thresholds are placeholders and have not been agreed "
              "with the planning team.", size=8, muted=True)

    # ---- provenance ----------------------------------------------------
    _h(pdf, "Provenance")
    _body(
        pdf,
        "Every figure above is computed from the shipment book and the external "
        "feeds listed against each event, and is reproducible from the as-of "
        "instant on this page. Figures marked as synthetic come from generated "
        "data, not from Sika records. This report is advisory: it proposes, the "
        "planner decides.",
        size=8, muted=True,
    )

    out = pdf.output()
    return bytes(out) if not isinstance(out, (bytes, bytearray)) else bytes(out)


def build_summary(route: dict, as_of_label: str, posture: dict) -> str:
    """Plain-text summary, for pasting into mail or chat.

    Deliberately short: the deadline, the money, the ask, and who is on it. A
    summary that needs scrolling is one nobody reads before the meeting.
    """
    lines = [
        f"[{route['level_label'].upper()}] {route['name']}",
        route["directive"],
        "",
        route["reason"],
        "",
        f"Action by       : {_hours(route.get('lead_time_hours'))}",
        f"Exposure        : {_chf(route.get('exposure_chf'))}",
        f"Shipments       : {route['shipments_at_risk']} of {route['shipments']} at risk",
    ]
    if route.get("contracts"):
        lines.append(f"Contracts       : {', '.join(route['contracts'])}")

    if route.get("events"):
        lines += ["", "DRIVERS"]
        for event in route["events"][:4]:
            prob = (
                f"P {event['probability'] * 100:.0f}%"
                if event.get("probability") is not None
                else "P unsourced"
            )
            lines.append(f"  - {event['title']} ({prob}, {event['source']})")

    actions = route.get("actions", [])
    lines += ["", "OPTIONS"]
    if actions:
        for action in actions[:4]:
            lines.append(f"  - {action['sentence']}")
    else:
        lines.append("  - No option currently saves more than it costs. Monitor.")

    response = route.get("response", {})
    manager = response.get("route_manager")
    lines += ["", "WHO"]
    if manager:
        lines.append(f"  Route manager : {manager['name']} ({manager['role']})")
    teams = response.get("standing_teams", [])
    if teams:
        lines.append(f"  Convene       : {', '.join(t['name'] for t in teams)}")
    seniors = response.get("seniors", [])
    if seniors:
        lines.append(
            f"  Seniors       : {', '.join(s['name'] + ' — ' + s['role'] for s in seniors)}"
        )

    approval = response.get("approval")
    if approval:
        lines += ["", f"APPROVAL: {approval['note']}"]

    lines += [
        "",
        f"As of {as_of_label}. {posture.get('headline', '')}",
        "Advisory — the tool proposes, the planner decides. Synthetic data.",
    ]
    return "\n".join(lines)


def _chf(value) -> str:
    if value is None:
        return "—"
    return f"CHF {value:,.0f}"


def _hours(value) -> str:
    if value is None:
        return "—"
    if value < 0:
        return "passed"
    if value < 48:
        return f"{value:.0f} h"
    return f"{value / 24:.0f} days"


def filename(route: dict, as_of: str) -> str:
    slug = "".join(
        ch if ch.isalnum() else "-" for ch in route["route_id"].lower()
    ).strip("-")
    stamp = as_of[:10] if isinstance(as_of, str) else datetime.utcnow().date().isoformat()
    return f"risk-pack_{slug}_{stamp}.pdf"


def as_stream(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
