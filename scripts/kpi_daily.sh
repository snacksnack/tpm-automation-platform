#!/bin/zsh
# The KPI program's daily run (RC1-299, RC1-301, RC1-305, RC1-307): advance
# the simulated program one day, snapshot every registered program, track
# every shipping KPI from the snapshot just taken, then escalate — retry any
# source that came back broken, record what stands with its blast radius and
# proposed fix, and post it to Slack. Run by launchd at 07:00 local via
# scripts/launchd/com.reidcollins.kpi-daily.plist; safe to run by hand.
# `--no-tick` snapshots, tracks and escalates without advancing the clock.
#
# Order matters and is why this is one job rather than three: converge the
# world, record it, then read it. Tracking against a snapshot from yesterday
# would date every reading a day behind the program.
#
# EVAL_DATABASE_URL lives in ~/.zshrc and nowhere else (RC1-263). launchd
# does not read shell profiles, so the one `export` line is pulled from the
# profile here — the credential keeps its single home, and a rotation is
# "update ~/.zshrc", nothing more. If the line is absent the eval-run-store
# snapshot records the source as an error and the run carries on.
#
# Exit code: the worst of the steps (0 ok; 1 a tick refused, a source was
# not ok, or a KPI read stale/broken; 2 a Jira error or a store that could
# not be reached). Every step runs regardless — a finished program (tick
# exits 1 on day 69) still gets snapshotted, and a broken source is exactly
# the day worth recording.
#
# A `1` here is routine, not an alarm: the simulated program's spend line
# has no landed week until day 7, so its two cost KPIs read stale and the
# job exits 1 every morning until then. `launchctl list` shows that as the
# last exit status. Read data/kpi-sim/daily.log before assuming a break.

set -u
cd "$(dirname "$0")/.." || exit 2
PY="$PWD/.venv/bin/python"
TICK=1
[[ "${1:-}" == "--no-tick" ]] && TICK=0

if [[ -z "${EVAL_DATABASE_URL:-}" && -r "$HOME/.zshrc" ]]; then
  eval "$(grep -E '^export EVAL_DATABASE_URL=' "$HOME/.zshrc" || true)"
fi
export EVAL_DATABASE_URL="${EVAL_DATABASE_URL:-}"

# DD_API_KEY keeps the same single home (RC1-305 Datadog leg). Absent, the
# track stage skips the Datadog write and Postgres still gets the day.
if [[ -z "${DD_API_KEY:-}" && -r "$HOME/.zshrc" ]]; then
  eval "$(grep -E '^export DD_API_KEY=' "$HOME/.zshrc" || true)"
fi
export DD_API_KEY="${DD_API_KEY:-}"

worst=0
step() {  # step <name> <command...>
  local name=$1; shift
  echo "== $name  $(date '+%Y-%m-%d %H:%M:%S')"
  "$@"
  local rc=$?
  (( rc > worst )) && worst=$rc
  echo "== $name exit $rc"
}

(( TICK )) && step tick "$PY" -m simulate tick
step snapshot:simulated-program "$PY" -m collectors snapshot simulated-program
step snapshot:eval-run-store "$PY" -m collectors snapshot eval-run-store
step track:simulated-program "$PY" -m kpi.track --program simulated-program
step track:eval-run-store "$PY" -m kpi.track --program eval-run-store
step escalate:simulated-program "$PY" -m kpi.escalate --program simulated-program
step escalate:eval-run-store "$PY" -m kpi.escalate --program eval-run-store
echo "== done exit $worst"
exit $worst
