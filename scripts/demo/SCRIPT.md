# Horizon demo: script and camera plan

Runtime without pauses: **2:30**. Shanshan 60 s · Harjot 90 s. With the 10 pauses it runs about 3:00, depending on how long each one is held.

Generated from `scripts/demo/storyboard.json` by `python scripts/demo/record.py --script`; edit the storyboard, not this file.

## Shanshan: The dynamic map (0:00.0 to 1:00.0)

**The words**

> The dynamic map is the main interface for situation awareness. It brings together vessels, shipping routes, ports, warehouses, and real-time or predicted disruptions in one view. When a disruption is detected, Horizon identifies which routes and vessels may be affected. But the impact is not the same for every vessel, even on the same route. Based on each vessel's current position, route, ETA, disruption timing, and downstream inventory, Horizon evaluates the impact individually. The map then zooms into the affected area, highlights the vessels at risk, and shows their routes and disruption information. For each affected vessel, Horizon can also evaluate alternative ports and routes, including sea, rail, and road connections. This turns the map from a monitoring tool into an intuitive interface that helps planners clearly see where and when disruptions occur, which shipments are at risk, and what alternative options are available.

**Camera, cues and breakpoints**

### 0:00.0 · S1a

_The dynamic map is the main interface for situation awareness._

- `0:00.0`: screen: **board**; pull back to the full screen
- `0:00.2` on "dynamic map": frame tightly on the map pane (1.5 s)
- `0:02.9` on "situation awareness": zoom 2.3× on the alert ladder in the header (Critical, Alert, Watch, Bias, Normal) (1.0 s); ring the alert ladder in the header (Critical, Alert, Watch, Bias, Normal); label "Every route, ranked by how soon someone must decide"
- `0:04.2`: zoom 1.9× on the critical route's card in the list (0.8 s); cursor to the card's open button (›)

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `0:05.0`: hotspot on the card's open button (›), "Open the critical route"

### 0:05.0 · S1b

_It brings together vessels, shipping routes, ports, warehouses, and real-time or predicted disruptions in one view._

- `0:05.0`: click the card's open button (›)
- `0:05.4`: screen: **route**; frame tightly on the map pane (0.8 s)
- `0:06.2` on "vessels": zoom 2.3× on the vessel icons at the origin port (0.7 s); ring the vessel icons at the origin port
- `0:06.7` on "shipping routes": frame tightly on the map pane (1.1 s); label "The route, stop by stop"
- `0:07.7` on "ports": ring the port icons on the map and the Ports chip
- `0:08.1` on "warehouses": ring the plants and warehouses on the map and the Inventory chip; label "Plants, depots, partner warehouses"
- `0:09.0` on "real-time or predicted": zoom 2.2× on step 1 of the route's tree, What is happening: the event and its trend (0.9 s); ring step 1 of the route's tree, What is happening: the event and its trend; label "Detected live, with its trend"
- `0:10.7` on "in one view": pull back to the full screen (1.0 s)

### 0:12.0 · S2a

_When a disruption is detected, Horizon identifies which routes and vessels may be affected._

- `0:12.4` on "disruption is detected": zoom 2.6× on the CRITICAL chip (0.8 s); ring the CRITICAL chip
- `0:14.8` on "which routes": zoom 2.1× on the route name (0.7 s); ring the route name
- `0:15.8` on "vessels may be affected": zoom 2.3× on step 2, Who is hit: orders on the route and exposure (0.7 s); ring step 2, Who is hit: orders on the route and exposure
- `0:17.1`: zoom 2.0× on the Shipments tab (0.6 s); cursor to the Shipments tab

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `0:17.8`: hotspot on the Shipments tab, "Open Shipments"

### 0:17.8 · S2b

_But the impact is not the same for every vessel, even on the same route._

- `0:17.9`: click the Shipments tab
- `0:18.2`: screen: **ships_mid**; frame tightly on the list of shipments on the route (0.8 s)
- `0:19.3` on "not the same": ring the rows marked Major disruption; label "Misses the promised date"
- `0:20.5` on "every vessel": ring the rows marked Nominal; label "Same stretch, still on time"
- `0:22.4` on "same route": zoom 2.7× on the stretch both groups are on (0.7 s); ring the stretch both groups are on

### 0:23.8 · S2c

_Based on each vessel's current position, route, ETA, disruption timing, and downstream inventory, Horizon evaluates the impact individually._

- `0:23.8`: screen: **ships_top**; zoom 1.9× on the showcase vessel's row (0.5 s)
- `0:24.1`: click the showcase vessel's row
- `0:24.6`: screen: **card**; frame tightly on the shipment card that opens under the row (0.7 s)
- `0:25.4` on "current position": ring the pill of the stretch it is on now and the progress bar: % done, km to go
- `0:26.2` on "route": ring the journey pills, stop by stop
- `0:26.6` on "ETA": ring the 'as planned' tile: its ETA and days late
- `0:26.8` on "disruption timing": ring the amber pill where the event hits; label "Where and when it hits"
- `0:27.9` on "downstream inventory": ring the 'at risk' tile in CHF; label "Cost at the customer if stock runs out"
- `0:29.2` on "evaluates the impact individually": ring the ways table: every way, evaluated
- `0:30.9`: frame tightly on the map pane (0.5 s); cursor to the red hazard marker on the map

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `0:31.4`: hotspot on the red hazard marker on the map, "Zoom into the affected area"

### 0:31.4 · S3

_The map then zooms into the affected area, highlights the vessels at risk, and shows their routes and disruption information._

- `0:32.1` on "zooms into the affected area": zoom 2.5× on the red hazard marker on the map (1.3 s); ring the red hazard marker on the map; label "{event_short}"
- `0:33.9` on "highlights the vessels at risk": zoom 2.5× on the selected vessel's halo (0.9 s); ring the selected vessel's halo; label "{vessel}"
- `0:36.1` on "shows their routes": frame tightly on the old route (dotted) and the two new ones (0.9 s); ring the #1 badge on the map and the #2 badge on the map
- `0:37.4` on "disruption information": zoom 2.3× on the Action Hub's header: vessel, CRITICAL ROUTE, Major disruption (0.8 s); ring the Action Hub's header: vessel, CRITICAL ROUTE, Major disruption

### 0:39.4 · S4a

_For each affected vessel, Horizon can also evaluate alternative ports and routes, including sea, rail, and road connections._

- `0:42.2` on "alternative ports and routes": zoom 2.5× on the Hub's list of ways (#1, #2) (0.8 s); ring the Hub's list of ways (#1, #2)
- `0:43.5` on "including sea": frame tightly on the old route (dotted) and the two new ones (0.8 s)
- `0:44.3` on "sea": ring the destination port's label on the map; label "Seaport"
- `0:44.6` on "rail": ring the #1 badge on the map; label "#1 {alt1}"
- `0:45.3` on "road connections": ring the #2 badge on the map and the Road & rail chip; label "#2 {alt2}"

### 0:46.8 · S4b

_This turns the map from a monitoring tool into an intuitive interface that helps planners clearly see where and when disruptions occur, which shipments are at risk, and what alternative options are available._

- `0:46.8` on "This turns the map": pull back to the full screen (1.6 s)
- `0:52.9` on "where": ring the red hazard marker on the map; label "Where"
- `0:53.5` on "when disruptions occur": ring the amber pill where the event hits; label "When: the stretch it hits"
- `0:55.0` on "which shipments are at risk": ring the showcase vessel's row and the Shipments tab; label "Which shipments"
- `0:57.3` on "alternative options": ring the Hub's list of ways (#1, #2) and the ways table: every way, evaluated; label "What the options are"
- `0:59.2`: frame tightly on the Action Hub (0.8 s); cursor to the Hub's Time, Cost and Risk sliders

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `1:00.0`: hotspot on the Hub's Time, Cost and Risk sliders, "Over to Harjot: solutions, architecture and AI"

## Harjot: Solutions, architecture and AI (1:00.0 to 2:30.0)

**The words**

> From here, planners act. The Hub ranks every way by time, cost and risk, a smart split rushes the urgent boxes while the rest stay put, and it lists partner carriers nearby. The decision tree turns this into a decision: key accounts first, each way with arrival, extra cost and CO₂e, the cost of doing nothing, with penalties, then sign-off and one-click booking, with fifteen minutes to undo. Every shipment has its own page: each vehicle, each box by deadline, its risk matrix and its risk-in-effect radar: severity, category, impact. Drivers and skippers report from the road in a simple app, straight onto their vehicle. Under the hood, Horizon reads sixteen sources: free public APIs, custom connectors for our own data, and commercial feeds once licensed. Two AI layers read them: one triages out the noise, one extracts the event, predictable, like a gauge or a forecast, or sudden, like a closure or a blockade. Unusual activity counts too: bursts of small orders or carrier push-outs flag a crisis days early, tied to the route they hit. Ask anything: answers come from the board's own numbers, with a free local AI for open questions. When limits are crossed, the all-hands goes daily and every department confirms with one click. Critical alerts reach your phone, the summary goes out as a PDF, and every closed case feeds the risk ledger. From signal to decision, in minutes.

**Camera, cues and breakpoints**

### 1:00.0 · H1

_From here, planners act. The Hub ranks every way by time, cost and risk, a smart split rushes the urgent boxes while the rest stay put, and it lists partner carriers nearby._

- `1:02.2` on "ranks every way": zoom 2.7× on the Hub's Time, Cost and Risk sliders (0.8 s); ring the Hub's Time, Cost and Risk sliders; label "Your weights: time, cost, risk"
- `1:04.3` on "smart split": screen: **hub_split0**; zoom 2.3× on the Smart split section (0.6 s)
- `1:05.1` on "smart split": click the Split shipment switch
- `1:05.8` on "rushes": screen: **hub_split**; zoom 2.1× on the Smart split section (0.4 s)
- `1:06.5` on "urgent boxes": zoom 2.8× on the box chips, urgent ones marked ! (0.5 s); ring the first urgent box; label "{moved} of {boxes} boxes move, most urgent first"
- `1:07.7` on "the rest stay put": frame tightly on the two branch labels on the map (0.8 s); ring branch A on the map and branch B on the map
- `1:09.8` on "partner carriers nearby": screen: **hub_partners**; zoom 2.2× on the Partners nearby list (0.6 s); ring the first partner
- `1:11.3`: zoom 1.9× on the card's Decision tree button (0.6 s); cursor to the card's Decision tree button

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `1:12.0`: hotspot on the card's Decision tree button, "Open the decision tree"

### 1:12.0 · H2a

_The decision tree turns this into a decision: key accounts first, each way with arrival, extra cost and CO₂e, the cost of doing nothing, with penalties, then sign-off and one-click booking,_

- `1:12.0`: click the card's Decision tree button
- `1:12.3`: screen: **tree**; pull back to the full screen
- `1:15.0` on "key accounts first": frame tightly on the key-account boxes in Who is hit (0.8 s); ring the first key account and the second key account; label "Key accounts first"
- `1:16.3` on "each way": zoom 2.6× on the BEST way's box (0.7 s); ring the BEST way's box
- `1:17.1` on "arrival": ring its arrival date
- `1:17.7` on "extra cost": ring its extra cost
- `1:18.6` on "CO₂e": zoom 2.8× on its CO₂e figure (leaf icon: lowest) (0.5 s); ring its CO₂e figure (leaf icon: lowest); label "Lowest CO₂e of the ways"
- `1:19.2` on "cost of doing nothing": screen: **tree_cost**; zoom 2.4× on the If nobody acts breakdown (0.6 s); ring the If nobody acts breakdown
- `1:20.4` on "with penalties": click the Count them (penalties) link
- `1:21.0` on "penalties": screen: **tree_cost_pen**; ring the delay penalties line
- `1:21.4` on "then sign-off": screen: **tree_book**; zoom 2.4× on the Sign-off box (your limit) (0.6 s); ring the Sign-off box (your limit); label "Within your sign-off limit"
- `1:22.7` on "one-click booking": zoom 2.6× on the Book it now button (0.6 s); cursor to the Book it now button

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `1:24.2`: hotspot on the Book it now button, "Book it"

### 1:24.2 · H2b

_with fifteen minutes to undo._

- `1:24.2`: click the Book it now button
- `1:24.5`: screen: **tree_booked**; zoom 2.6× on the Undo button (0.5 s); ring the Undo button; label "Undo within 15 minutes"

### 1:26.2 · H3

_Every shipment has its own page: each vehicle, each box by deadline, its risk matrix and its risk-in-effect radar: severity, category, impact._

- `1:26.2`: screen: **ship_page**; pull back to the full screen
- `1:28.0` on "each vehicle": frame tightly on the vehicles, stretch by stretch (0.7 s); ring the vehicle carrying it now and a vehicle it has not reached yet (grey); label "Grey: not reached yet"
- `1:28.8` on "each box by deadline": frame tightly on the box chips with their deadlines (0.6 s); ring a critical box; label "Critical boxes marked"
- `1:30.3` on "risk matrix": frame tightly on the risk matrix (0.8 s); ring the event's cell in the matrix
- `1:31.5` on "risk-in-effect radar": frame tightly on the two risk-in-effect radars (0.7 s); ring the Measured radar
- `1:32.6` on "severity": ring the severity legend
- `1:33.2` on "category, impact": ring the top category row with its days of delay; label "Category and days of delay"

### 1:34.8 · H4

_Drivers and skippers report from the road in a simple app, straight onto their vehicle._

- `1:34.8`: screen: **drv_form**; pull back to the full screen
- `1:36.1` on "report from the road": ring the Queued button and the Where are you field
- `1:36.6`: frame tightly on the Send report button (0.5 s)
- `1:37.1`: click the Send report button
- `1:37.5`: screen: **drv_sent**; frame tightly on the sent report (0.4 s); ring the sent report
- `1:38.7` on "straight onto their vehicle": screen: **ship_report**; frame tightly on the new report on the vehicle (0.6 s); ring the new report on the vehicle; label "Filed from {vessel}"
- `1:40.6`: screen: **board**; zoom 2.0× on the Risk profile link in the header (0.4 s); cursor to the Risk profile link in the header

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `1:41.2`: hotspot on the Risk profile link in the header, "Under the hood: the sources"

### 1:41.2 · H5

_Under the hood, Horizon reads sixteen sources: free public APIs, custom connectors for our own data, and commercial feeds once licensed._

- `1:41.2`: click the Risk profile link in the header
- `1:41.5`: screen: **prof_top**; pull back to the full screen
- `1:42.9` on "sixteen sources": zoom 2.4× on the line '16 external sources configured, 0 billable' (0.7 s); ring the line '16 external sources configured, 0 billable'
- `1:43.8` on "free public APIs": frame tightly on a free API's row (0.6 s); ring its FREE · NO KEY tag
- `1:44.7` on "custom connectors": frame tightly on a custom connector's row (our own export) (0.6 s); ring a custom connector's row (our own export); label "Our own exports, read as files"
- `1:46.7` on "commercial feeds": screen: **prof_absent**; frame tightly on the commercial feeds' rows (0.6 s); ring the AIS vessel tracking row (commercial licence); label "Commercial: plugs in once licensed"
- `1:48.7`: screen: **prof_top**; zoom 2.2× on the ← Horizon link (0.4 s); cursor to the ← Horizon link

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `1:49.2`: hotspot on the ← Horizon link, "Back to the board: the signals"

### 1:49.2 · H6

_Two AI layers read them: one triages out the noise, one extracts the event, predictable, like a gauge or a forecast, or sudden, like a closure or a blockade. Unusual activity counts too: bursts of small orders or carrier push-outs flag a crisis days early, tied to the route they hit._

- `1:49.2`: click the ← Horizon link
- `1:49.5`: screen: **board**; zoom 1.8× on the Signals tab (0.4 s); cursor to the Signals tab
- `1:49.9`: click the Signals tab
- `1:50.2`: screen: **sig_funnel**; frame tightly on the signal funnel (0.6 s)
- `1:50.5` on "Two AI layers": zoom 2.5× on the Read by AI stage (0.7 s); ring the Read by AI stage; label "Layer 1 triage, layer 2 extraction"
- `1:52.2` on "triages out the noise": frame tightly on the signal funnel (0.7 s); ring the kept / dropped count
- `1:53.8` on "extracts the event": zoom 2.4× on the first event read (0.6 s); ring the first event read
- `1:55.0` on "predictable": frame tightly on the measured events (gauge, forecast) (0.6 s); ring the river gauge event and the forecast event; label "Measured: gauges and forecasts"
- `1:57.6` on "sudden": screen: **sig_rows**; frame tightly on the reported events (news, closures) (0.6 s); ring the news event and the road closure event; label "Reported: news and closures"
- `1:60.0` on "Unusual activity": screen: **sig_top**; frame tightly on the two early-warning lists (0.7 s)
- `2:01.6` on "bursts of small orders": zoom 2.5× on the burst of small orders (0.6 s); ring the burst of small orders
- `2:03.1` on "carrier push-outs": zoom 2.5× on the carrier push-out pattern (0.6 s); ring the carrier push-out pattern
- `2:04.5` on "crisis days early": label "No official notice yet"
- `2:05.3` on "tied to the route": click the Unusual volume button
- `2:06.3` on "route they hit": screen: **volume**; frame tightly on the Unusual volume panel and the route it lights up (0.7 s); ring the route in the panel and the order-surge label on the map
- `2:07.5`: zoom 2.2× on the Ask button (0.5 s); cursor to the Ask button

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `2:08.2`: hotspot on the Ask button, "Ask the AI"

### 2:08.2 · H7

_Ask anything: answers come from the board's own numbers, with a free local AI for open questions._

- `2:08.2`: click the Ask button
- `2:08.5`: screen: **ask0**; frame tightly on the Ask panel (0.5 s); cursor to the suggested question 'Which key accounts are at risk?'
- `2:09.1`: click the suggested question 'Which key accounts are at risk?'
- `2:09.4`: screen: **ask1**; frame tightly on the answer (0.6 s)
- `2:10.8` on "board's own numbers": ring the answer's source tag
- `2:12.2` on "free local AI": label "A local model: nothing leaves the machine"
- `2:14.0`: zoom 2.0× on the All-hands tab (0.5 s); cursor to the All-hands tab

### 2:14.6 · H8

_When limits are crossed, the all-hands goes daily and every department confirms with one click._

- `2:14.7`: click the All-hands tab
- `2:14.9`: screen: **ah**; frame tightly on the three limits (0.5 s)
- `2:15.0` on "limits are crossed": ring the first limit crossed
- `2:16.7` on "goes daily": zoom 2.5× on Meets: Daily, and when it next sits (0.5 s); ring Meets: Daily, and when it next sits
- `2:17.5` on "every department": zoom 2.6× on the ring of departments (0.5 s); click the first department's dot
- `2:18.3` on "confirms": screen: **ah_card**; cursor to its Confirmed button
- `2:19.0` on "one click": click its Confirmed button
- `2:19.7` on "click": screen: **ah_conf**; ring the ring of departments; label "Confirmed"
- `2:19.9`: zoom 2.0× on the Alerts button (0.4 s); cursor to the Alerts button

**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `2:20.4`: hotspot on the Alerts button, "Alerts, the summary, the case"

### 2:20.4 · H9

_Critical alerts reach your phone, the summary goes out as a PDF, and every closed case feeds the risk ledger. From signal to decision, in minutes._

- `2:20.5`: click the Alerts button
- `2:20.7`: screen: **alerts**; frame tightly on the Critical alerts by email window (0.5 s)
- `2:21.4` on "reach your phone": ring the Critical tick
- `2:22.8` on "summary goes out": screen: **esc**; zoom 2.4× on the Download PDF pack button (0.6 s); ring the Download PDF pack button
- `2:24.6` on "every closed case": screen: **case0**; zoom 2.2× on the Close case choices (0.6 s); cursor to Rerouted
- `2:25.6` on "feeds": click Rerouted
- `2:26.1` on "risk ledger": screen: **ledger**; frame tightly on the closed case in the risk ledger's history (0.5 s); ring the closed case in the risk ledger's history; label "Closed: into the risk ledger"
- `2:27.1` on "From signal to decision": screen: **final**; pull back to the full screen (1.4 s)

