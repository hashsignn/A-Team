#!/usr/bin/env python3
"""Build the 4-minute pitch deck as a .pptx.

Structure follows The Pitch Guide (UZH Innovation Hub): the five content
elements (real customer experience, business model, team, competition,
go-to-market) and the five performance rules (concise, elevator pitch,
timing, details, audience). Timing is carried in the speaker notes, which
is where the 4-minute budget is actually enforced.

    python docs/pitch/build_deck.py
"""
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
IMG = HERE / "img"
OUT = HERE / "Supply_Chain_Risk_Radar_pitch.pptx"

# Sika-ish palette, the same one the app's "Sika" theme uses.
RED = RGBColor(0xD9, 0x04, 0x2B)
GOLD = RGBColor(0xFF, 0xC7, 0x00)
INK = RGBColor(0x14, 0x14, 0x16)
PAPER = RGBColor(0xF7, 0xF5, 0xEF)
MUTED = RGBColor(0x6B, 0x66, 0x5E)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

W, H = Inches(13.333), Inches(7.5)

# Precomputed because ruff (rightly) dislikes calls in argument defaults.
ZERO = Emu(0)
BAR_H = Inches(0.09)
CHIP_W = Inches(1.5)
FRAME_PAD = Emu(9000)
HEAD = "Verdana"
BODY = "Verdana"


def deck() -> Presentation:
    p = Presentation()
    p.slide_width, p.slide_height = W, H
    return p


def blank(prs, bg=PAPER):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = bg
    return s


def text(slide, left, top, width, height, runs, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, spacing=1.0):
    """runs: list of (string, size_pt, bold, colour, space_after_pt)."""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, (body, size, bold, colour, after) in enumerate(runs):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.line_spacing = spacing
        para.space_after = Pt(after)
        r = para.add_run()
        r.text = body
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = colour
        r.font.name = HEAD if bold else BODY
    return box


def bar(slide, top=ZERO, height=BAR_H):
    """The two-tone rule the app draws under its header."""
    half = Emu(int(W / 2))
    for i, colour in enumerate((GOLD, RED)):
        sh = slide.shapes.add_shape(1, Emu(i * int(half)), top, half, height)
        sh.fill.solid()
        sh.fill.fore_color.rgb = colour
        sh.line.fill.background()
        sh.shadow.inherit = False


def chip(slide, left, top, label, fg=WHITE, bg=RED, width=CHIP_W):
    sh = slide.shapes.add_shape(5, left, top, width, Inches(0.38))
    sh.fill.solid()
    sh.fill.fore_color.rgb = bg
    sh.line.fill.background()
    sh.shadow.inherit = False
    tf = sh.text_frame
    tf.margin_left = tf.margin_right = 0
    para = tf.paragraphs[0]
    para.alignment = PP_ALIGN.CENTER
    r = para.add_run()
    r.text = label
    r.font.size = Pt(12)
    r.font.bold = True
    r.font.color.rgb = fg
    r.font.name = HEAD
    return sh


def picture(slide, name, left, top, width=None, height=None):
    """Place an image, keeping its aspect ratio from whichever side is given."""
    path = IMG / name
    if not path.exists():
        raise SystemExit(f"missing screenshot: {path}")
    return slide.shapes.add_picture(str(path), left, top, width=width, height=height)


def frame(slide, pic, pad=FRAME_PAD):
    """A hairline behind a screenshot so it reads as a screen, not a bleed."""
    sh = slide.shapes.add_shape(
        1, pic.left - pad, pic.top - pad, pic.width + 2 * pad, pic.height + 2 * pad)
    sh.fill.background()
    sh.line.color.rgb = RGBColor(0xD8, 0xD3, 0xC7)
    sh.line.width = Pt(1)
    sh.shadow.inherit = False
    slide.shapes._spTree.remove(sh._element)
    slide.shapes._spTree.insert(2, sh._element)


def notes(slide, body):
    slide.notes_slide.notes_text_frame.text = body


def title_slide(prs):
    s = blank(prs, INK)
    bar(s, Inches(7.41))
    text(s, Inches(0.9), Inches(1.5), Inches(11.5), Inches(3.2), [
        ("Supply Chain Risk Radar", 54, True, WHITE, 10),
        ("When the Rhine drops, the freight still has to arrive.", 26, False, GOLD, 26),
        ("A decision tool for Sika's transport planners: it turns a disruption "
         "into a named action with a deadline, in minutes rather than days.",
         18, False, RGBColor(0xC9, 0xC5, 0xBC), 0),
    ])
    text(s, Inches(0.9), Inches(6.4), Inches(11.5), Inches(0.6), [
        ("Sika Innovathon 2026  ·  Team A  ·  4-minute pitch", 13, False, MUTED, 0),
    ])
    notes(s, """[0:00–0:15]  ELEVATOR PITCH — say this, then stop.

"Sika moves freight across the Rhine, Suez and half of Europe. When something
breaks, the delay is not the expensive part — the two days spent working out
who is affected is. Our Risk Radar cuts that to minutes, and it names the one
action that still gets the load there on time."

Do not read the subtitle aloud. Let the room read it.""")


def problem_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.9), Inches(0.75), Inches(5.6), Inches(5.4), [
        ("A real Monday morning", 34, True, INK, 18),
        ("The Kaub gauge on the Rhine falls. Barges out of Basel are cut to "
         "45% of full payload.", 17, False, INK, 14),
        ("The planner knows within the hour. What they do not know is which "
         "of 28 consignments are on the water, which customer contracts they "
         "belong to, and which ones still have an option left.",
         17, False, INK, 14),
        ("That part takes a day and a half of phone calls, and the cheapest "
         "options expire while it happens.", 17, True, RED, 22),
        ("The loss is not caused by the river. It is caused by the time "
         "between the river and the decision.", 19, True, INK, 0),
    ])
    pic = picture(s, "routes_table.png", Inches(6.9), Inches(0.95), width=Inches(5.7))
    frame(s, pic)
    text(s, Inches(6.9), Inches(6.62), Inches(5.7), Inches(0.5), [
        ("Every affected lane, ranked by how soon it needs a decision.",
         11, False, MUTED, 0),
    ])
    notes(s, """[0:15–0:50]  REAL CUSTOMER EXPERIENCE — guide item 1.

Tell it as one specific morning, not as a category of problem. Kaub. 45%.
28 consignments. Name the numbers; they are on the screen behind you.

The line that has to land: the money is lost in the gap between the event
and the decision, not in the event. Pause after it.

Do not explain the table yet — it is the next slide's job.""")


def value_slide(prs):
    s = blank(prs, INK)
    bar(s)
    text(s, Inches(0.9), Inches(0.9), Inches(11.5), Inches(1.4), [
        ("Our value proposition is speed of action", 40, True, WHITE, 8),
        ("Not prediction. Not a cheaper route. Speed.", 20, False, GOLD, 0),
    ])
    cards = [
        ("DELIVER ON TIME",
         "The freight reaches the customer on the promised date, even when the "
         "lane breaks. That is the thing we optimise for."),
        ("COST IS SECONDARY",
         "We will pay to keep a date. What we will not do is take a route that "
         "loses money — every option shown is worth more than it costs."),
        ("A DEADLINE, NOT A SCORE",
         "Each lane carries the hour its cheapest option expires. The ladder is "
         "a clock: act within 6 h, within 24–48 h, watch, monitor."),
    ]
    x = Inches(0.9)
    for head, body in cards:
        card = s.shapes.add_shape(5, x, Inches(2.85), Inches(3.7), Inches(3.0))
        card.fill.solid()
        card.fill.fore_color.rgb = RGBColor(0x22, 0x22, 0x25)
        card.line.color.rgb = RGBColor(0x3A, 0x3A, 0x3E)
        card.shadow.inherit = False
        card.text_frame.text = ""
        text(s, x + Inches(0.35), Inches(3.2), Inches(3.0), Inches(2.4), [
            (head, 15, True, GOLD, 12),
            (body, 15, False, RGBColor(0xDE, 0xDA, 0xD2), 0),
        ])
        x += Inches(3.85)
    notes(s, """[0:50–1:10]  THE PROPOSITION — say it in one breath.

"Our value proposition is speed of action: the freight reaches the customer
on time even during a delay. Cost is secondary to delivery — but we never
propose a route that turns a profit into a loss."

This is the sentence the judges should be able to repeat back. Say it slowly.
Three cards, three words each if you are behind: on time, not at a loss, by
a deadline.""")


def board_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.7), Inches(0.62), Inches(7.2), Inches(1.0), [
        ("One board: what is wrong, and how long you have", 27, True, INK, 4),
        ("Every lane in the book, ranked by time-to-act rather than by severity.",
         15, False, MUTED, 0),
    ])
    pic = picture(s, "board.png", Inches(0.7), Inches(1.75), width=Inches(8.5))
    frame(s, pic)
    text(s, Inches(9.55), Inches(1.75), Inches(3.2), Inches(5.0), [
        ("CRITICAL  ·  ALERT  ·  WATCH  ·  BIAS  ·  NORMAL", 12, True, RED, 10),
        ("Five rungs, and every one of them is a deadline — 6 h, 24–48 h, "
         "3–7 days, monitor, nothing to do.", 14, False, INK, 16),
        ("CHF 509,452", 26, True, INK, 2),
        ("expected loss across the book, which is what trips the convene rule.",
         13, False, MUTED, 16),
        ("48 shipments", 22, True, INK, 2),
        ("need a decision inside 48 h — and the team does not sit again "
         "until Tuesday.", 13, False, MUTED, 16),
        ("Advisory. The tool proposes; the planner decides.",
         12, True, MUTED, 0),
    ])
    notes(s, """[1:10–1:40]  THE PRODUCT, PART 1 — demo the board.

Point at the ladder first: "these are not severities, they are deadlines."
Then the convene banner: half a million Swiss francs of exposure, 48
shipments needing a decision, and the next standing meeting is Tuesday. The
tool is telling the team to meet early — and saying why in one sentence.

If you are short on time, cut everything except the ladder and the banner.

Note for Q&A: the data on screen is synthetic. The pipeline behind it runs
on live public feeds, and the whole thing runs offline at zero cost.""")


def route_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.7), Inches(0.62), Inches(11.9), Inches(1.0), [
        ("Click a lane, and you see the freight — not a chart", 27, True, INK, 4),
        ("Düdingen → Basel → Rotterdam. 28 consignments, leg by leg. Green is on "
         "plan, amber still has an option open, red has run out of them.",
         15, False, MUTED, 0),
    ])
    pic = picture(s, "route.png", Inches(0.7), Inches(1.85), width=Inches(7.3))
    frame(s, pic)
    pic2 = picture(s, "vehicle_panel.png", Inches(8.3), Inches(2.55), width=Inches(4.4))
    frame(s, pic2)
    text(s, Inches(8.3), Inches(1.85), Inches(4.4), Inches(0.6), [
        ("Click one truck or barge:", 15, True, INK, 0),
    ])
    text(s, Inches(8.3), Inches(4.35), Inches(4.4), Inches(2.6), [
        ("the consignment ID, the customer, the value, how far it has got, "
         "what is hitting it, and what the driver reported from the road.",
         14, False, INK, 14),
        ("36 h until the first option closes · CHF 184,352 exposed · "
         "18 consignments can still be changed.", 14, True, RED, 0),
    ])
    notes(s, """[1:40–2:10]  THE PRODUCT, PART 2 — one lane, one page.

This is the slide that answers "does it actually know anything". Trucks for
the road leg, barges for the river legs, wagons where a lane goes by rail.
Colour is not decoration: amber means there is still an option, red means
there is not.

Click one: SYN-0042, Meridian Infrastructure, CHF 30,429, 42 hours to decide,
hit by the Kaub restriction. Under it, whatever the person with the load
actually reported.

"18 of them can still be changed" is the whole product in one number.""")


def act_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.7), Inches(0.62), Inches(11.9), Inches(1.0), [
        ("From alert to an executed decision", 27, True, INK, 4),
        ("A gated playbook, then a message with the approval already worked out.",
         15, False, MUTED, 0),
    ])
    pic = picture(s, "alternatives.png", Inches(0.7), Inches(1.85), width=Inches(6.5))
    frame(s, pic)
    pic2 = picture(s, "escalate_panel.png", Inches(7.9), Inches(1.85), width=Inches(3.3))
    frame(s, pic2)
    text(s, Inches(11.5), Inches(1.95), Inches(1.5), Inches(4.5), [
        ("LOCKED", 13, True, RED, 8),
        ("Rerouting stays locked until a human confirms the disruption "
         "first-hand. The tool will not spend money on a rumour.",
         12, False, INK, 14),
        ("COMPOSED", 13, True, RED, 8),
        ("Who to notify, what to say, and the numbers behind it — ready "
         "to send.", 12, False, INK, 0),
    ])
    notes(s, """[2:10–2:35]  THE PRODUCT, PART 3 — the action, and the brake.

Two things to say, no more.

One: the playbook is gated. You cannot reroute until somebody has confirmed
the disruption first-hand. That is deliberate — speed without confirmation is
just an expensive mistake made faster.

Two: when it unlocks, the escalation is already written — recipients, subject,
the exposure, the contracts, the deadline. The planner presses send.

Only options worth more than they cost are listed. When none of them are, the
tool says so instead of inventing one. That is the profitability guard.""")


def loop_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.8), Inches(1.1), Inches(7.3), Inches(4.6), [
        ("The confirmation comes from the road", 30, True, INK, 16),
        ("The person with the load files in four taps: where they are, whether "
         "the load is intact, a photo, a revised arrival time.",
         17, False, INK, 14),
        ("That report is what unlocks the reroute. It is also what makes the "
         "radar's picture true rather than modelled.", 17, False, INK, 18),
        ("Photos are stripped of EXIF before they are stored, and every report "
         "is signed with a per-driver credential.", 15, False, MUTED, 14),
        ("Detect → confirm → act → close out. The loop closes in the app, "
         "not in a mailbox.", 17, True, RED, 0),
    ])
    pic = picture(s, "driver_tall.png", Inches(9.0), Inches(0.75), height=Inches(6.1))
    frame(s, pic)
    notes(s, """[2:35–2:50]  CLOSING THE LOOP — the differentiator, said quickly.

Every competitor watches feeds. Almost none of them collect from the driver.
Four taps, on a phone, at the roadside — and that report is the thing that
releases the reroute.

Mention the two engineering details only if asked: EXIF stripped on upload,
per-driver credentials on the endpoint. They matter to a procurement team,
not to a pitch audience.""")


def competition_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.8), Inches(0.62), Inches(11.7), Inches(1.0), [
        ("Where we sit", 30, True, INK, 4),
        ("Visibility tools tell you something happened. We tell you what to do "
         "about it, and by when.", 15, False, MUTED, 0),
    ])
    rows = [
        ("", "Feed\nmonitors", "Visibility\nplatforms", "TMS\noptimisers", "Risk\nRadar"),
        ("Tells you an event happened", "yes", "yes", "no", "yes"),
        ("Names the affected consignments", "no", "yes", "partly", "yes"),
        ("Gives a deadline to act by", "no", "no", "no", "yes"),
        ("Refuses options that lose money", "no", "no", "yes", "yes"),
        ("Confirms from the vehicle itself", "no", "no", "no", "yes"),
        ("Runs with no per-call model cost", "yes", "no", "no", "yes"),
    ]
    left, top = Inches(0.8), Inches(1.85)
    tw, th = Inches(11.7), Inches(4.4)
    shape = s.shapes.add_table(len(rows), 5, left, top, tw, th)
    tbl = shape.table
    tbl.columns[0].width = Inches(4.5)
    for c in range(1, 5):
        tbl.columns[c].width = Inches(1.8)
    for ri, row in enumerate(rows):
        for ci, cell_text in enumerate(row):
            cell = tbl.cell(ri, ci)
            cell.fill.solid()
            if ri == 0:
                cell.fill.fore_color.rgb = INK
            elif ci == 4:
                cell.fill.fore_color.rgb = RGBColor(0xFF, 0xF4, 0xCC)
            else:
                cell.fill.fore_color.rgb = WHITE if ri % 2 else RGBColor(0xF2, 0xF0, 0xEA)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = cell.margin_right = Inches(0.12)
            para = cell.text_frame.paragraphs[0]
            para.alignment = PP_ALIGN.LEFT if ci == 0 else PP_ALIGN.CENTER
            r = para.add_run()
            r.text = cell_text
            r.font.size = Pt(13 if ri == 0 else 13)
            r.font.name = HEAD
            if ri == 0:
                r.font.bold = True
                r.font.color.rgb = GOLD if ci == 4 else WHITE
            elif ci == 0:
                r.font.color.rgb = INK
            else:
                yes = cell_text == "yes"
                r.font.bold = yes
                r.font.color.rgb = RED if (yes and ci == 4) else (
                    INK if yes else RGBColor(0xA8, 0xA3, 0x99))
    notes(s, """[2:50–3:10]  COMPETITION — guide item 4.

Read only the two rows nobody else ticks: "gives a deadline to act by" and
"confirms from the vehicle itself". Those are the differentiators; the rest of
the grid is there so the room can check we know the landscape.

If challenged on the last row: the noise filter is deterministic, not a model,
so the pipeline costs nothing per event and the same input always produces the
same output. A model is only called on the handful of items that survive.""")


def model_slide(prs):
    s = blank(prs)
    bar(s)
    text(s, Inches(0.8), Inches(0.62), Inches(11.7), Inches(0.8), [
        ("Business model and go-to-market", 30, True, INK, 0),
    ])
    cols = [
        ("HOW IT EARNS",
         ["Licensed per planning seat, per year, to the transport and supply "
          "chain teams that already sit on the disruption call.",
          "Priced against one avoided expedite. A single rescued critical "
          "consignment covers a seat for a year.",
          "Connector work — a customer's TMS, their carrier feeds — is a "
          "fixed-fee integration on top."]),
        ("HOW IT LANDS",
         ["Start inside Sika: one corridor, the Rhine, where the pain is "
          "already named and the data already exists.",
          "Widen to the Suez and trans-Atlantic lanes on the same engine — "
          "new lanes are configuration, not code.",
          "Then out: the same problem belongs to every manufacturer moving "
          "bulk chemicals on fixed customer dates."]),
        ("WHY IT IS CHEAP TO RUN",
         ["Free, keyless public feeds do the watching; custom sources are "
          "declared in YAML, so a customer adds their own without us.",
          "The filter is deterministic, so cost does not scale with news "
          "volume.",
          "No per-event model bill, and the whole pipeline replays offline "
          "for audit."]),
    ]
    x = Inches(0.8)
    for head, items in cols:
        chip(s, x, Inches(1.65), head, width=Inches(3.6), bg=INK, fg=GOLD)
        runs = []
        for it in items:
            runs.append(("—  " + it, 14, False, INK, 14))
        text(s, x, Inches(2.35), Inches(3.6), Inches(4.2), runs)
        x += Inches(3.85)
    notes(s, """[3:10–3:45]  BUSINESS MODEL + GO-TO-MARKET — guide items 2 and 5.

Compress. One sentence per column:

"Per seat, per year, priced against a single avoided expedite. Land on the
Rhine corridor inside Sika, widen along the same engine, then out to every
manufacturer with fixed delivery dates. And it is cheap to run, because the
feeds are free and the filter is deterministic — the bill does not grow with
the news."

If the clock is against you, drop the third column entirely and keep it for
questions.""")


def close_slide(prs):
    s = blank(prs, INK)
    bar(s, Inches(7.41))
    text(s, Inches(0.9), Inches(1.15), Inches(11.5), Inches(1.4), [
        ("Team, state, and what we want next", 32, True, WHITE, 0),
    ])
    text(s, Inches(0.9), Inches(2.5), Inches(5.5), Inches(4.0), [
        ("WHERE IT ACTUALLY IS", 14, True, GOLD, 12),
        ("Working software, not a mockup. Board, per-lane page, gated playbook, "
         "driver app and escalation pack all run today.",
         16, False, RGBColor(0xDE, 0xDA, 0xD2), 12),
        ("513 automated tests. Every number on screen is reproducible from a "
         "pinned as-of date, so a decision can be re-examined months later.",
         16, False, RGBColor(0xDE, 0xDA, 0xD2), 12),
        ("The figures shown are synthetic shipments on real lanes. The feeds "
         "behind them are live and free.", 14, False, MUTED, 0),
    ])
    text(s, Inches(7.0), Inches(2.5), Inches(5.4), Inches(4.0), [
        ("WHAT WE ARE ASKING FOR", 14, True, GOLD, 12),
        ("One live corridor and one planner.", 22, True, WHITE, 12),
        ("Give us the Rhine lane, read-only access to the shipment book, and "
         "one planner for two weeks. We will run the radar alongside the way "
         "you work today and count, on real disruptions, how many consignments "
         "kept their date.", 16, False, RGBColor(0xDE, 0xDA, 0xD2), 16),
        ("If it does not shorten the time to a decision, it has failed, and "
         "that is measurable in a fortnight.", 15, True, GOLD, 0),
    ])
    notes(s, """[3:45–4:00]  TEAM + ASK — finish early rather than late.

"This is working software, not a mockup — five hundred tests, every figure
reproducible from a pinned date. What we want is one corridor and one planner
for two weeks, and we will count how many consignments kept their date."

Then stop. Do not fill the silence.

Team introductions go here if the format asks for them — names, one line each,
and move on. The guide is right that judges want to know who is building it;
it is also right that thirty seconds is plenty.

TOTAL: 4:00. Rehearse against a clock — slides 4, 5 and 6 are where time is
lost, so cut there first.""")


def main() -> None:
    prs = deck()
    title_slide(prs)
    problem_slide(prs)
    value_slide(prs)
    board_slide(prs)
    route_slide(prs)
    act_slide(prs)
    loop_slide(prs)
    competition_slide(prs)
    model_slide(prs)
    close_slide(prs)
    prs.save(OUT)
    print(f"wrote {OUT} ({len(prs.slides.__iter__.__self__._sldIdLst)} slides)")


if __name__ == "__main__":
    main()
