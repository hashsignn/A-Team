"""The fleet map: assets, live status, recovery routes, split loads, partners.

WHAT THIS ADDS, AND WHAT IT DOES NOT
====================================
The board ranks lanes. The route page draws a lane as the vehicles on it.
This package puts those vehicles on a 2D map and answers the question that
follows a red dot: *where can it go instead, what does that cost, and who
nearby can carry it?*

It does not re-score anything. Status, delay, P(late), deadlines and the bill
if late are read off the run the board was built from — the same floats — so
the map cannot disagree with the board about how bad something is. What is
new is geometry and arithmetic the board never needed:

  assets.py     where each asset is, its three-colour status, and the Action
                Hub payload (manifest, logs, 5x5 matrix, five-axis radar)
  reroute.py    recovery routes from the live location, ranked on a weighted
                Time / Cost / Risk formula
  split.py      assigning containers to routes, and the suggestion that moves
                only the ones whose deadline the original asset now misses
  vendors.py    partners within the radius of the disruption, and which of the
                generated routes each can legally and physically cover
  sealanes.py   a small, explicit sea-lane graph, so "avoid Suez" is a
                shortest-path question with the Suez node removed
  osrm.py       optional road geometry, only with RADAR_ALLOW_NETWORK=1
  manifest.py   the synthetic manifest and crew — labelled synthetic

Every function here takes the RunContext and reads the board's as-of. Nothing
reads the wall clock.
"""
