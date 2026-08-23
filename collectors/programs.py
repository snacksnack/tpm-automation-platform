"""Programs as first-class config (RC1-301; the `register` step of RC1-233).

A program is the unit a snapshot is taken of: which Jira issues are its, where
its spend line comes from, whether it has a simulated clock, whether the eval
store is one of its sources. Two are registered — the two the KPI agent has
trees for (`docs/kpi/programs/`). The ids match the brief filenames so the
tree, the brief, the ledger and the snapshots all key on one string.

Kept as data in code rather than a config file: there are two programs, the
fields are typed, and a third one is a three-line addition that gets reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import settings


@dataclass(frozen=True)
class JiraSource:
    project: str
    jql: str  # the program's issues, epic included — the label the collector keys on


@dataclass(frozen=True)
class Program:
    id: str
    name: str
    jira: JiraSource | None = None
    #: The simulator's weekly spend line (RC1-299). A real billing feed is RC1-308.
    spend_csv: str | None = None
    #: Directory holding the simulator's clock.json. None means the program
    #: runs on wall-clock time and the snapshot's sim-date is today's date.
    clock_dir: str | None = None
    #: Read run rows from the shared eval store (EVAL_DATABASE_URL).
    eval_store: bool = False
    eval_subjects: tuple[str, ...] = ()  # empty = every subject
    #: Values a KPI may use that are declared, not measured — a plan price off
    #: a billing page. Listed in the source catalog (RC1-303) as exactly that,
    #: so a KPI leaning on one is a proxy with a stated caveat, not a measurement.
    constants: dict[str, float] = field(default_factory=dict)

    @property
    def project_key(self) -> str:
        """What the shared `runs` table records as the project. Programs without
        Jira use their own id, so a run always names something."""
        return self.jira.project if self.jira else self.id


SIMULATED = Program(
    id="simulated-program",
    name="Observability Platform GA (simulated)",
    jira=JiraSource("PMA", 'project = PMA AND labels = "kpi-sim" ORDER BY key ASC'),
    spend_csv=f"{settings.kpi_sim_dir}/spend.csv",
    clock_dir=settings.kpi_sim_dir,
)

EVAL_RUN_STORE = Program(
    id="eval-run-store",
    name="Eval run store",
    eval_store=True,
    # heroku-postgresql:essential-0 on reid-eval-store (RC1-263): the fixed half
    # of what the program costs. Re-verified against the billing page monthly.
    constants={"store_plan_usd_per_month": 5.0},
)

PROGRAMS: dict[str, Program] = {p.id: p for p in (SIMULATED, EVAL_RUN_STORE)}


def get(program_id: str) -> Program:
    try:
        return PROGRAMS[program_id]
    except KeyError:
        raise KeyError(
            f"no program {program_id!r}; registered: {', '.join(PROGRAMS)}"
        ) from None
