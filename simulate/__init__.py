"""Program simulator (RC1-299): a scripted ten-week program in Jira that
advances one simulated day per tick.

`scenario` is the program as data plus `state_at(day)`, a pure function from a
sim-day to the exact Jira state the program should be in — which stories
exist, their status, dates, points, flags, labels and links, and which spend
rows have landed. `apply` converges live Jira to that state and verifies it.
Seed is converge(0); tick is converge(day + 1); the ground-truth ledger
(RC1-300) is derived from the same function, so the simulator and the thing
that checks the KPI agent cannot disagree about what day N looks like.

    python -m simulate seed            # create / converge to day 0
    python -m simulate tick            # advance one day
    python -m simulate to-day 45       # jump (development)
    python -m simulate verify          # Jira == scenario for the current day?
    python -m simulate teardown        # delete everything, forget the clock
"""
