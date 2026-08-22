# Program brief — Observability Platform GA

This is the input the define stage receives. It describes the program the way
an SVP sponsor would describe it, and lists the data sources the program
controls. It does not name KPIs — that is the agent's job.

## The program

Ship the **Observability Platform** to general availability: distributed
tracing across the backend services, SLO dashboards for the three tier-1
services, and structured alerting routed to the on-call rotation. It lives
under the PMA project's epic *Platform Reliability & Observability* (PMA-2).

- **Sponsor:** SVP Engineering. The commitment made upward is a **GA date**
  and a **cloud-cost envelope**; scope is the three workstreams above.
- **Duration:** ten weeks from kickoff. GA is committed for the last day of
  week 10.
- **Team:** five engineers across three workstreams (tracing, SLO dashboards,
  alerting), one TPM. Engineers own stories; each story has one assignee.
- **Scope:** roughly thirty stories, estimated in story points, with start and
  due dates and `blocks` links where one workstream depends on another
  (alerting depends on SLO dashboards, which depend on tracing data).
- **Budget:** a planned weekly cloud spend for the new telemetry pipeline
  (ingest, storage, query), with a ten-week envelope agreed with Finance.
  Spend is reported weekly.

What the sponsor has said they care about, in their words: *"Are we going to
hit the date, is it going to cost what we said, and will I find out early if
either of those changes."*

## Data sources the program controls

| Source | What it holds | Cadence |
| --- | --- | --- |
| Jira issues under the program epic | status, assignee, story points, start date, due date, labels, created date, `blocks` / `is blocked by` links, comments | live; snapshotted once per day |
| Jira epic | the committed GA date as the epic's due date | live |
| Cloud spend line | one row per week: planned spend, actual spend, for the telemetry pipeline | weekly, available the Monday after |
| Program calendar | kickoff date, committed GA date, week boundaries | static |

Snapshots are dated. A day's numbers are computed from that day's snapshot,
not from Jira's changelog.

## Constraints on the tree

- One or two outcome KPIs; three or four leading indicators.
- Every KPI names its source from the table above, precisely enough to code.
- Anything the sponsor would not change a decision over is not a KPI.
