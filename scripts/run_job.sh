#!/bin/zsh
# launchd entry point (spec B10): `/bin/zsh <repo>/scripts/run_job.sh <job>`.
#
# Everything real happens in `market-intel job run` — duplicate-run
# prevention is Python's `fcntl.flock` there, because macOS ships no
# `flock(1)` and no `timeout(1)` (a shell lock would die with
# "command not found" and every overlapping run would proceed silently).
#
# This wrapper only: resolves the repo, finds a runner, and tees the output
# into the dated log spec B10 asks for. It always exits 0 — a launchd job
# that "fails" gets throttled, and a dead data source is not a failure.
set -u

JOB="${1:-}"
if [[ -z "$JOB" ]]; then
  echo "usage: run_job.sh <job>" >&2
  exit 0
fi

REPO_ROOT="${0:A:h:h}"
cd "$REPO_ROOT" || exit 0

LOG_DIR="$REPO_ROOT/var/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/job-$JOB-$(date +%Y%m%d).log"

# launchd starts with a minimal PATH that has neither uv nor a project
# console script. Try uv first (the project's documented runner), then the
# venv's own entry point.
if command -v uv >/dev/null 2>&1; then
  RUNNER=(uv run market-intel)
elif [[ -x "$REPO_ROOT/.venv/bin/market-intel" ]]; then
  RUNNER=("$REPO_ROOT/.venv/bin/market-intel")
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  RUNNER=("$HOME/.local/bin/uv" run market-intel)
else
  echo "$(date -Iseconds) run_job.sh: no runner found (uv / .venv) — job=$JOB skipped" >>"$LOG"
  exit 0
fi

{
  echo "--- $(date -Iseconds) job=$JOB start"
  "${RUNNER[@]}" job run --name "$JOB"
  echo "--- $(date -Iseconds) job=$JOB end rc=$?"
} >>"$LOG" 2>&1

exit 0
