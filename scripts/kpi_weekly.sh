#!/bin/zsh
# The KPI program's weekly brief (RC1-306): narrate both programs from the
# readings the daily job has been landing all week, archive each brief in
# kpi_briefs, and post it to Slack. Run by launchd Monday 10:30 local via
# scripts/launchd/com.reidcollins.kpi-weekly.plist. Safe to run by hand; pass
# --dry to write and archive without posting.
#
# The eval-run-store is re-read first (RC1-498). The weekly eval sweep
# (com.hihelloreid.agent-evals.weekly, Monday 09:30) lands after the daily
# job's 07:00 track, so without this the brief describes last Monday's runs
# an hour before, or just after, this Monday's replace them: on 2026-10-05
# it led with a pass rate the sweep had already superseded. So this waits
# for a sweep that is mid-run, then snapshots and tracks the real program
# before writing it up. The simulated program is not re-read: nothing moves
# it between 07:00 and now, and a second snapshot would only add a run.
#
# Credentials, same one-home rule as the daily job: EVAL_DATABASE_URL from
# ~/.zshrc (launchd reads no profiles, so the one export line is pulled here),
# ANTHROPIC_API_KEY and SLACK_WEBHOOK_URL from the repo .env (config reads it).
#
# Exit code: the worst of the steps (0 both briefs posted; 1 the re-read
# found a source not ok or a KPI stale or broken, which the brief then says;
# 2 a brief could not be written or posted — no key, nothing tracked, or a
# brief the numbers audit refused). Every step runs regardless: the real
# program's brief is the done-when and must not wait on the simulated one,
# and a brief over a stale reading still beats no brief.

set -u
cd "$(dirname "$0")/.." || exit 2
PY="$PWD/.venv/bin/python"
POST=(--post)
[[ "${1:-}" == "--dry" ]] && POST=()

if [[ -z "${EVAL_DATABASE_URL:-}" && -r "$HOME/.zshrc" ]]; then
  eval "$(grep -E '^export EVAL_DATABASE_URL=' "$HOME/.zshrc" || true)"
fi
export EVAL_DATABASE_URL="${EVAL_DATABASE_URL:-}"

# DD_API_KEY, same one-home rule (RC1-322): with it, narrate's model calls
# become LLM Observability traces; without it they are simply untraced.
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

# launchd prints a "PID" line for a job only while it is running. Bounded:
# a sweep that hangs must not take the brief with it.
SWEEP=com.hihelloreid.agent-evals.weekly
waited=0
while launchctl list "$SWEEP" 2>/dev/null | grep -q '"PID"'; do
  if (( waited >= 45 )); then
    echo "== the eval sweep is still running after 45 min; writing the brief without it"
    break
  fi
  (( waited == 0 )) && echo "== waiting for the eval sweep ($SWEEP) to finish"
  sleep 60
  (( waited++ ))
done

step snapshot:eval-run-store "$PY" -m collectors snapshot eval-run-store
step track:eval-run-store "$PY" -m kpi.track --program eval-run-store
step brief:eval-run-store "$PY" -m kpi.narrate --program eval-run-store "${POST[@]}"
step brief:simulated-program "$PY" -m kpi.narrate --program simulated-program "${POST[@]}"
echo "== done exit $worst"
exit $worst
