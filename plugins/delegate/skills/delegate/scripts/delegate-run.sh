#!/usr/bin/env bash
# Run one delegated task on a local model through OpenCode, in its own git worktree.
#   delegate-run.sh <task-file> <name> [model]
#   delegate-run.sh --check
# Settings come from the environment or ~/.config/delegate/env (DELEGATE_CONFIG overrides):
#   DELEGATE_PROVIDER      OpenCode provider id from opencode.json (required)
#   DELEGATE_MODEL         default worker model id under that provider (required)
# The worktree is <repo>.delegate/<name> on branch delegate/<name>, created from HEAD on first
# run and reused on later runs, so a retry continues from the previous attempt's changes.
# The worker never commits; the caller reviews the diff and commits.
set -uo pipefail

conf=${DELEGATE_CONFIG:-$HOME/.config/delegate/env}
# shellcheck disable=SC1090
[ -f "$conf" ] && . "$conf"
provider=${DELEGATE_PROVIDER:-}
[ -n "$provider" ] || { echo "set DELEGATE_PROVIDER (an OpenCode provider id) in $conf" >&2; exit 2; }
command -v opencode >/dev/null || { echo "opencode is not installed" >&2; exit 2; }

if [ "${1:-}" = "--check" ]; then
  model=${DELEGATE_MODEL:?set DELEGATE_MODEL in $conf}
  # </dev/null: opencode run reads piped stdin and waits forever if it is left open
  out=$(timeout 120 opencode run -m "$provider/$model" "Reply with exactly: DELEGATE-OK" </dev/null 2>&1)
  if printf '%s' "$out" | grep -q DELEGATE-OK; then
    echo "ok: $provider/$model answers"; exit 0
  fi
  echo "worker model $provider/$model did not answer:" >&2
  printf '%s\n' "$out" | sed 's/\x1b\[[0-9;]*m//g' | tail -n 15 >&2
  exit 1
fi

task=${1:?usage: delegate-run.sh <task-file> <name> [model] | --check}
name=${2:?usage: delegate-run.sh <task-file> <name> [model]}
model=${3:-${DELEGATE_MODEL:?set DELEGATE_MODEL in $conf}}
[ -f "$task" ] || { echo "no task file: $task" >&2; exit 2; }
task=$(cd "$(dirname "$task")" && pwd)/$(basename "$task")

root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "run this from inside the git repo to change" >&2; exit 2; }
wt="$root.delegate/$name"
branch="delegate/$name"
if [ -d "$wt" ]; then
  echo "reusing worktree $wt"
else
  mkdir -p "$root.delegate"
  git -C "$root" worktree add -q -b "$branch" "$wt" HEAD || exit 2
  echo "created worktree $wt on $branch"
fi

attempt=$(( $(ls "$wt".attempt-*.log 2>/dev/null | wc -l) + 1 ))
log="$wt.attempt-$attempt.log"
prompt="You are implementing one task in a git worktree. The task spec is below.
Rules:
- Make only the changes the spec asks for. Do not touch files it does not mention unless the change cannot work without it.
- Do not add dependencies, change CI, or reformat unrelated code.
- Run the spec's Verify command and fix failures until it passes or you are stuck.
- Do not commit, push, or create branches.
- Finish with a short summary: files changed, the Verify result, and anything you could not do or are unsure about.

$(cat "$task")"

echo "attempt $attempt: $provider/$model, log $log"
start=$SECONDS
(cd "$wt" && timeout "${DELEGATE_TIMEOUT:-1800}" opencode run -m "$provider/$model" "$prompt") </dev/null >"$log" 2>&1
rc=$?
echo "worker exited $rc after $(( SECONDS - start ))s$([ $rc = 124 ] && echo ' (timed out)')"

echo "--- worker summary (end of log)"
sed 's/\x1b\[[0-9;]*m//g' "$log" | tail -n 25
echo "--- changes in $wt"
git -C "$wt" status --short
git -C "$wt" diff --stat
exit $rc
