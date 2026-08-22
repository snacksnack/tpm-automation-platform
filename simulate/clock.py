"""The simulation clock and manifest, on disk (RC1-299).

`clock.json` holds the current sim-day and its sim-date — what the collector
(RC1-301) stamps snapshots with. `manifest.json` maps slug -> Jira key for the
issues the last converge saw. `spend.csv` is the cloud-spend line as of the
current day: only the weeks that have landed. All three live in a gitignored
data directory; they are machine state, not repo artifacts.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simulate import scenario


@dataclass(frozen=True)
class Clock:
    day: int
    updated_at: str

    @property
    def sim_date(self):
        return scenario.sim_date(self.day)


class SimState:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.clock_path = self.dir / "clock.json"
        self.manifest_path = self.dir / "manifest.json"
        self.spend_path = self.dir / "spend.csv"

    def read_clock(self) -> Clock | None:
        if not self.clock_path.exists():
            return None
        data = json.loads(self.clock_path.read_text())
        return Clock(day=int(data["day"]), updated_at=data["updated_at"])

    def write(self, day: int, keys: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.clock_path.write_text(
            json.dumps(
                {
                    "day": day,
                    "sim_date": scenario.sim_date(day).isoformat(),
                    "week": scenario.week_of(day),
                    "kickoff": scenario.KICKOFF.isoformat(),
                    "ga_day": scenario.GA_DAY,
                    "last_day": scenario.LAST_DAY,
                    "source_broken": scenario.source_broken_on(day),
                    "active_events": [e.id for e in scenario.active_events(day)],
                    "updated_at": now,
                },
                indent=2,
            )
            + "\n"
        )
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

    def forget(self) -> None:
        for p in (self.clock_path, self.manifest_path, self.spend_path):
            if p.exists():
                p.unlink()
        if self.dir.exists() and not any(self.dir.iterdir()):
            self.dir.rmdir()
