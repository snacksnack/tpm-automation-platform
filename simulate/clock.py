"""The simulation clock and manifest, on disk (RC1-299).

`clock.json` holds the current sim-day and its sim-date — what the collector
(RC1-301) stamps snapshots with. `manifest.json` maps slug -> Jira key for the
issues the last converge saw. `spend.csv` is the cloud-spend line as of the
current day: only the weeks that have landed. `ledger.csv` is the ground-truth
ledger (RC1-300) for the whole program — it is a function of the scenario, not
of the day, and is rewritten on every converge so a scenario edit cannot leave
a stale copy behind. All four live in a gitignored data directory; they are
machine state, not repo artifacts.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simulate import ledger, scenario


@dataclass(frozen=True)
class Clock:
    day: int
    updated_at: str
    #: False while a converge mutated Jira and then failed (RC1-417): `day` is
    #: still the last day that fully landed, and the world is somewhere past
    #: it. The next successful converge clears this.
    converged: bool = True
    #: The day that converge was reaching for when it failed, if any.
    converging_to: int | None = None

    @property
    def sim_date(self):
        return scenario.sim_date(self.day)


class SimState:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.clock_path = self.dir / "clock.json"
        self.manifest_path = self.dir / "manifest.json"
        self.spend_path = self.dir / "spend.csv"
        self.ledger_path = self.dir / "ledger.csv"

    def read_clock(self) -> Clock | None:
        if not self.clock_path.exists():
            return None
        data = json.loads(self.clock_path.read_text())
        return Clock(
            day=int(data["day"]),
            updated_at=data["updated_at"],
            # Absent on a clock written before RC1-417: a file that predates
            # the flag was written by a converge that completed.
            converged=bool(data.get("converged", True)),
            converging_to=data.get("converging_to"),
        )

    def _clock_doc(self, day: int, now: str) -> dict:
        return {
            "day": day,
            "sim_date": scenario.sim_date(day).isoformat(),
            "week": scenario.week_of(day),
            "kickoff": scenario.KICKOFF.isoformat(),
            "ga_day": scenario.GA_DAY,
            "last_day": scenario.LAST_DAY,
            "source_broken": scenario.source_broken_on(day),
            "active_events": [e.id for e in scenario.active_events(day)],
            "updated_at": now,
        }

    def _write_manifest(self, keys: dict[str, str], now: str) -> None:
        self.manifest_path.write_text(
            json.dumps(
                {
                    "project": scenario.PROJECT,
                    "program_label": scenario.PROGRAM_LABEL,
                    "epic": keys.get("epic"),
                    "stories": {slug: key for slug, key in sorted(keys.items()) if slug != "epic"},
                    "updated_at": now,
                },
                indent=2,
            )
            + "\n"
        )

    def write(self, day: int, keys: dict[str, str]) -> None:
        """Record a day that fully landed. Clears any RC1-417 dirty marker."""
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        doc = self._clock_doc(day, now) | {"converged": True}
        self.clock_path.write_text(json.dumps(doc, indent=2) + "\n")
        self._write_manifest(keys, now)
        with self.spend_path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["week", "week_start", "planned_usd", "actual_usd", "landed_on_day"])
            for row in scenario.spend_rows(day):
                w.writerow(
                    [
                        row.week,
                        scenario.sim_date(7 * (row.week - 1)).isoformat(),
                        f"{row.planned_usd:.2f}",
                        f"{row.actual_usd:.2f}",
                        row.lands_on_day,
                    ]
                )
        self.ledger_path.write_text(ledger.to_csv(ledger.derive()))

    def mark_incomplete(self, converging_to: int, keys: dict[str, str]) -> None:
        """Record that a converge mutated Jira and then failed (RC1-417).

        `day` stays on the last day that fully landed — there is no honest day
        number for a world half-way between two — and `converged: false` says
        the world past it is untrusted. The collector turns that into an
        `error` on the clock source, so the day is *recorded* as unreadable
        rather than silently mis-dated.

        The spend line and the ledger are deliberately not rewritten: they are
        functions of a day the world never reached. The manifest is, so the
        keys a partial converge created are not lost.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        last_good = self.read_clock()
        day = last_good.day if last_good else max(converging_to - 1, 0)
        doc = self._clock_doc(day, now) | {
            "converged": False,
            "converging_to": converging_to,
        }
        self.clock_path.write_text(json.dumps(doc, indent=2) + "\n")
        self._write_manifest(keys, now)

    def forget(self) -> None:
        for p in (self.clock_path, self.manifest_path, self.spend_path, self.ledger_path):
            if p.exists():
                p.unlink()
        if self.dir.exists() and not any(self.dir.iterdir()):
            self.dir.rmdir()
