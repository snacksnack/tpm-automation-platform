"""Program KPI agent (RC1-298).

Owns the KPI lifecycle for a program rather than a dashboard: define a tree,
verify each KPI is measurable, compute on a schedule, narrate weekly, escalate
when a source breaks. The rubric the judgments are made under is a versioned
document (docs/kpi/rubric.md), and every stage states which version it used.

Stages land one story at a time:
  define      kpi/define.py     RC1-302  brief + rubric -> reviewable KPI tree
  snapshot    collectors/       RC1-301  one dated ProgramSnapshot per run per program
  instrument  kpi/instrument.py RC1-303  adopted tree + source catalog -> verified set
  track                         RC1-305  emits kpi/reading.py Readings (RC1-300)
  narrate                       RC1-306
  escalate                      RC1-307

The agent is checked against the simulated program's ground-truth ledger
(simulate/ledger.py, the `kpi-ledger` eval subject) — what every KPI should
read on every sim-day, derived from the scenario.
"""

#: The rubric version this package's judgments are made under. Bumped with
#: docs/kpi/rubric.md; a test asserts the two agree.
RUBRIC_VERSION = 2
