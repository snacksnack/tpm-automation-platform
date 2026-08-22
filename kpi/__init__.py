"""Program KPI agent (RC1-298).

Owns the KPI lifecycle for a program rather than a dashboard: define a tree,
verify each KPI is measurable, compute on a schedule, narrate weekly, escalate
when a source breaks. The rubric the judgments are made under is a versioned
document (docs/kpi/rubric.md), and every stage states which version it used.

Stages land one story at a time:
  define      kpi/define.py     RC1-302  brief + rubric -> reviewable KPI tree
  instrument                    RC1-303
  track                         RC1-305
  narrate                       RC1-306
  escalate                      RC1-307
"""

#: The rubric version this package's judgments are made under. Bumped with
#: docs/kpi/rubric.md; a test asserts the two agree.
RUBRIC_VERSION = 1
