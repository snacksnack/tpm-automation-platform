#!/bin/zsh
# The KPI program's weekly brief (RC1-306): narrate both programs from the
# readings the daily job has been landing all week, archive each brief in
# kpi_briefs, and post it to Slack. Run by launchd Monday 08:00 local via
# scripts/launchd/com.reidcollins.kpi-weekly.plist — an hour after the daily
# job, so Monday's readings are in before the week is written up. Safe to run
# by hand; pass --dry to write and archive without posting.
#
# Credentials, same one-home rule as the daily job: EVAL_DATABASE_URL from
# ~/.zshrc (launchd reads no profiles, so the one export line is pulled here),
# ANTHROPIC_API_KEY and SLACK_WEBHOOK_URL from the repo .env (config reads it).
#
# Exit code: the worst of the two programs (0 both briefs posted; 2 one could
# not be written or posted — no key, nothing tracked, or a brief the numbers
# audit refused). Both run regardless: the real program's brief is the
# done-when and must not wait on the simulated one.

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

step brief:eval-run-store "$PY" -m kpi.narrate --program eval-run-store "${POST[@]}"
step brief:simulated-program "$PY" -m kpi.narrate --program simulated-program "${POST[@]}"
echo "== done exit $worst"
exit $worst
