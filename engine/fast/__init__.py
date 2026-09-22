"""Solution A — fast action.

The rest of the engine answers "how much will this cost us". This package
answers a different question, and it is the one the planner actually asks at
07:40 on the morning the river drops:

    what gets this load to the customer on the promised date, and how fast
    can I set it running?

The ordering is therefore lexicographic on TIME, not on money: fewest days
late first, then soonest resolution. Money enters exactly once, as a veto —
an option that would take the consignment into a negative margin is removed
before ranking, not ranked lower. See ``margin.py`` for why a veto is the
honest shape for that constraint, and ``options.py`` for the ranking.
"""
