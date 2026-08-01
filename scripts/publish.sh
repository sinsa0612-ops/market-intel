#!/bin/bash
# Commit and push the *artefacts only* (spec B11).
#
# The CEO approved automatic commit/push for `reports/` and `docs/` and for
# nothing else. Source code stays a manual, human-approved commit, so this
# script is built to be structurally incapable of shipping it:
#
#   1. only `reports` and `docs` are ever `git add`ed;
#   2. the whole index is then inspected — anything outside those two trees
#      (e.g. a source file someone had already staged by hand) causes a
#      `git reset` and a non-zero exit, so the commit never happens;
#   3. the spec-B12 secret gate runs on the staged trees before the commit;
#   4. an empty stage is a silent success (nothing changed today);
#   5. a failed push is logged and exits 0 — being offline must not mark
#      report generation as failed, the next run retries by itself;
#   6. on any branch other than `main`, nothing happens at all.
#
# Usage: bash scripts/publish.sh [--dry-run]
# Exit:  0 ok / nothing to do / push failed
#        2 not a git repo
#        3 path guard fired (out-of-scope path staged)
#        4 secret gate fired
#
# No `flock`/`timeout` here — macOS ships neither (duplicate-run prevention
# is jobs.py's `fcntl.flock`).
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

PY="${MI_PYTHON:-python3}"
ALLOWED_RE='^(reports|docs)/'

git rev-parse --git-dir >/dev/null 2>&1 || { echo "publish: not a git repository"; exit 2; }

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
if [ "$branch" != "main" ]; then
  echo "publish: branch=$branch is not main — doing nothing (spec B11-6)"
  exit 0
fi

# spec B11-1 — only these two pathspecs, ever.
for d in reports docs; do
  [ -d "$d" ] && git add -- "$d"
done

staged="$(git diff --cached --name-only)"

# spec B11-2 — the guard looks at the WHOLE index, not just what we added:
# anything staged beforehand would otherwise ride along on our commit.
offenders="$(printf '%s\n' "$staged" | grep -v -E "$ALLOWED_RE" | grep -v '^$')"
if [ -n "$offenders" ]; then
  git reset -q
  echo "publish: refusing — out-of-scope paths were staged (source code is never auto-committed):" >&2
  printf '  %s\n' $offenders >&2
  echo "publish: index reset, nothing committed" >&2
  exit 3
fi

# spec B11-3 — nothing changed today.
if [ -z "$staged" ]; then
  echo "publish: nothing staged — nothing to do"
  exit 0
fi

# spec B12 — hard gate before anything becomes public.
if ! "$PY" "$REPO_ROOT/scripts/preflight_secret_gate.py" --root "$REPO_ROOT"; then
  git reset -q
  echo "publish: secret gate failed — index reset, nothing committed" >&2
  exit 4
fi

echo "publish: staged files"
printf '  %s\n' $staged

if [ "$DRY_RUN" -eq 1 ]; then
  git reset -q
  echo "publish: --dry-run — index reset, nothing committed or pushed"
  exit 0
fi

# spec B11-4 — `reports: <type> <stem> (auto)`, named after the newest
# report in the stage (falls back to the artefact count when only `docs/`
# changed, which happens on a pure site rebuild).
newest="$(printf '%s\n' $staged | grep -E '^reports/' | sort | tail -1)"
if [ -n "$newest" ]; then
  rtype="$(basename "$(dirname "$newest")")"
  stem="$(basename "$newest" .json)"
  msg="reports: $rtype $stem (auto)"
else
  msg="reports: site rebuild (auto)"
fi

git commit -q -m "$msg" || { echo "publish: commit failed" >&2; exit 0; }
echo "publish: committed — $msg"

# The staging guard above only governs what THIS commit contains. `git push`
# moves the whole branch, so any earlier unpushed commit rides along with the
# first report — that is how source code reaches the public repo with no
# approval (final-review.md F1: 2 unpushed source commits, 66 files, were
# queued behind the first scheduled publish). The CEO approved auto-publishing
# artefacts, not source, so the invariant has to be enforced here rather than
# remembered: refuse to push while anything other than reports/docs is ahead
# of the remote. The commit stays local and a human pushes after review.
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -n "$upstream" ]; then
  # The leading '.' matters: with only :(exclude) pathspecs git matches
  # nothing at all and the guard silently passes everything.
  ahead="$(git diff --name-only "$upstream"..HEAD -- \
             . ':(exclude)reports' ':(exclude)docs' 2>/dev/null || true)"
  if [ -n "$ahead" ]; then
    echo "publish: refusing to push — unreviewed non-artefact changes are ahead of $upstream:" >&2
    printf '  %s\n' $ahead | head -20 >&2
    echo "publish: the report is committed locally; a human must review and push those first." >&2
    exit 5
  fi
fi

if git push -q; then
  echo "publish: pushed"
else
  # spec B11-5 — offline or no credentials must not fail the job.
  echo "publish: push failed (offline or no credentials) — will retry next run" >&2
fi
exit 0
