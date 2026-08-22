# KPI rubric — version 1 (RC1-302)

The rubric the define stage applies and the reviewer argues with. It is
versioned the way the prompt templates are: bump the number in the title on any
change that would alter a verdict, and record the reason in the changelog at the
bottom. Every KPI tree states the rubric version it was drafted under, so a
tree and the rules it was judged by can be read together.

The point of writing it down first is the epic's own argument: a KPI agent's
judgment is only defensible if the rules are explicit enough that a human can
apply them and get the same answer — or disagree with the rule rather than with
the output.

## Vocabulary

A **KPI tree** for a program is one or two **outcome KPIs** at the root and
three or four **leading indicators** beneath them. Each leading indicator names
the outcome it leads. That is the whole shape; more than that is a dashboard,
which is a different artifact with a different audience.

An **outcome** is a change in the world the program exists to cause, stated so
that the SVP would still care about the number if the team did nothing this
week. A **leading indicator** is a number that moves *before* the outcome does,
with a stated mechanism for why. An **activity metric** counts things the team
did. Activity is never an outcome; it may be a leading indicator when the
mechanism is real and it is labelled as activity-derived.

## The six tests

Every candidate KPI passes or fails each of these, in order. A candidate that
fails test 1 at the root, or test 3 or test 4 anywhere, is rejected and the
rejection is recorded with its reason — a rejected candidate is part of the
tree's evidence, not waste.

### 1. Outcome or activity

> *If the team doubled this number by working harder on the wrong thing, would
> the SVP be pleased?*

If yes, it is an activity metric. Tickets closed, story points burned, runs
executed, briefs sent, comments posted — all activity. Activity can be
legitimately useful beneath an outcome (throughput is a fine input to a
schedule forecast) but it is labelled as such and never sits at the root.

The common disguise is a ratio with activity on top: "velocity" is activity;
"forecast date, given velocity" is an outcome. Ask what the number is *of*.

### 2. Leading or lagging

A lagging number confirms; a leading number predicts with lead time. Every
leading indicator states three things: **which** outcome it leads, the
**mechanism** (why movement here precedes movement there), and the **lead
time** in the program's units. "Correlates with" is not a mechanism. A leading
indicator with no stated mechanism is a coincidence waiting to be
Goodharted.

Outcomes are usually lagging. That is fine — that is what the leading
indicators are for.

### 3. So what, for an SVP

One sentence, in this shape:

> *If this number moves by X, the decision that changes is Y.*

If no decision changes — if the honest answer is "we would look into it" — the
number is a diagnostic, not a KPI. Diagnostics belong in the drill-through, not
the tree. An SVP reads the tree to decide whether to add people, cut scope,
move a date, change a vendor, or stop; a KPI that cannot feed one of those is
decoration.

### 4. A named, verified source

A KPI names its **source system**, the **exact field or query**, the **cadence**
at which a fresh value can exist, and **who owns** the source. "Measurable in
principle" does not count. "From Jira" does not count; "Jira `duedate` and
`customfield_10015` on issues labelled `ks-*`, snapshotted daily" counts.

The define stage *names* the source; the instrument stage (RC1-303) *verifies*
it against a real snapshot. A KPI that names a source that turns out not to
exist is proxied or rejected there — it does not ship on the strength of the
name.

### 5. Goodhart risk

Rate **low / medium / high**, and for anything above low, write the **gaming
path** (the cheapest way to move the number without moving the outcome) and
the **counter-metric** paired with it. A pass rate is gamed by deleting hard
cases, so it is paired with case count. A forecast date is gamed by closing
tickets that are not done, so it is paired with reopen rate. Unpaired
high-risk KPIs are not accepted.

### 6. Failure modes — how the number lies

List the ways this specific number can be *wrong while looking fine*. The
recurring ones:

- **Stale source** — the collector stopped and the last value is being re-read.
- **Denominator collapse** — a rate over a shrinking base looks stable or
  improves.
- **Survivorship** — only the things that got far enough to be recorded are
  counted.
- **Instrumentation hides the outcome** — the stage that would show the
  result is never recorded, so the outcome reads worse (or better) than
  reality. The job-search board that never records "interviewing" (RC1-91)
  is the worked example.
- **Proxy drift** — the proxy tracked the outcome when chosen and stopped.

A KPI with no listed failure mode has not been thought about.

## Staleness is a state

Every KPI declares its **cadence** and a **stale-after** threshold (normally one
to two cadences). A value older than that is reported as **stale** — a
first-class state alongside the number — and is never rendered as zero, blank,
or the previous value without the label. A source that returns nothing is
**broken**, which is a distinct state from stale: stale means "we have not
looked"; broken means "we looked and the instrument failed". The escalate
stage (RC1-307) acts on both; the brief (RC1-306) labels both.

## Proxies

A proxy is allowed when the direct measure is not available from any source the
program controls. A proxy must state, in one sentence, **what it misses** —
the cases where the proxy and the real outcome diverge — and that sentence
travels with the number into every brief. A proxy whose caveat cannot be
written is a rejected KPI, not a weaker one.

## Rejection grounds

Record a candidate as rejected, with the ground, when it:

- is an activity metric proposed at the root (test 1);
- changes no decision (test 3);
- has no honest source and no honest proxy (test 4 / proxies);
- duplicates the information of an accepted KPI — two numbers that always move
  together are one KPI and one distraction;
- is a diagnostic (useful in the drill-through, not at SVP altitude).

## Required fields per KPI

This is what the define stage emits and the instrument stage consumes. The
tree document renders these; the machine-readable twin carries them verbatim.

| Field | Meaning |
| --- | --- |
| `id` | Stable slug, e.g. `forecast-slip-days` |
| `name` | Short human name |
| `type` | `outcome` or `leading` |
| `direction` | `higher` or `lower` is better |
| `unit` | days, %, USD, count… |
| `definition` | The formula in words, precise enough to code |
| `source.system` | Jira, eval store, Heroku billing, simulator… |
| `source.query` | The fields / query / table the value comes from |
| `source.cadence` | How often a fresh value can exist |
| `source.owner` | Who fixes it when it breaks |
| `stale_after` | Threshold after which the value is reported stale |
| `so_what` | The test-3 sentence |
| `leads` | Leading only: `outcome_id`, `mechanism`, `lead_time` |
| `goodhart.risk` | low / medium / high |
| `goodhart.gaming_path` | How to move it without moving the outcome |
| `goodhart.counter` | The paired metric, or `none` with risk low |
| `failure_modes` | At least one, from the list above or specific |
| `activity_derived` | Leading only: `true` when the input is an activity count |

## Review procedure

Two trees exist for every program: one **hand-written before the agent runs**
(the baseline) and one the agent drafts from the program brief and this rubric.
The review compares them:

1. The **outcome KPIs must agree** in substance — same thing measured, even if
   named differently. Disagreement at the root means either the brief was
   wrong, the rubric has a gap, or the agent is wrong; the review says which.
2. Every **leading-indicator disagreement gets a recorded reason** — the
   reviewer either adopts the agent's indicator, keeps the baseline's, or
   rejects both, and says why in the review document.
3. A disagreement that turns out to be a **rubric gap** amends this document
   and bumps the version. That is the mechanism by which the rubric improves:
   arguing with drafts, not editing in the abstract.

## Changelog

- **v1** (2026-08-22) — initial rubric for RC1-302. Six tests, staleness as a
  state, proxy rule, rejection grounds, required fields, review procedure.
