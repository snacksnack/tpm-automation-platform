#!/bin/zsh
# The KPI program's daily run (RC1-299, RC1-301): advance the simulated
# program one day, then snapshot every registered program. Run by launchd
# at 07:00 local via scripts/launchd/com.reidcollins.kpi-daily.plist; safe
# to run by hand. `--no-tick` snapshots without advancing the clock.
#
# EVAL_DATABASE_URL lives in ~/.zshrc and nowhere else (RC1-263). launchd
# does not read shell profiles, so the one `export` line is pulled from the
# profile here — the credential keeps its single home, and a rotation is
# "update ~/.zshrc", nothing more. If the line is absent the eval-run-store
# snapshot records the source as an error and the run carries on.
#
# Exit code: the worst of the three steps (0 ok; 1 a tick refused or a
# source was not ok; 2 a Jira error). Every step runs regardless — a
# finished program (tick exits 1 on day 69) still gets snapshotted, and a
# broken source is exactly the day worth recording.

set -u
cd "$(dirname "$0")/.." || exit 2
PY="$PWD/.venv/bin/python"
TICK=1
[[ "${1:-}" == "--no-tick" ]] && TICK=0

if [[ -z "${EVAL_DATABASE_URL:-}" && -r "$HOME/.zshrc" ]]; then
  eval "$(grep -E '^export EVAL_DATABASE_URL=' "$HOME/.zshrc" || true)"
fi
export EVAL_DATABASE_URL="${EVAL_DATABASE_URL:-}"

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
echo "== done exit $worst"
exit $worst
