---
name: delegate
description: Plan a change yourself, then hand well-specified implementation tasks to cheaper or local models (run through OpenCode) in separate git worktrees, and review and merge their work. Use when the user says "delegate", "hand off to the local models", "plan then delegate", or asks to have worker models implement a plan.
---

# Delegate to worker models

You (the main session) plan and review. Worker models implement. Workers are separate OpenCode
processes started with `scripts/delegate-run.sh` (in this skill's base directory); each gets its own
worktree and branch and never commits. You review every diff before anything reaches the user's branch.

Settings live in `~/.config/delegate/env` (a shell file the script sources):

| Variable | Meaning |
|---|---|
| `DELEGATE_PROVIDER` | OpenCode provider id, as in the user's `opencode.json` |
| `DELEGATE_MODEL` | Default worker model id |
| `DELEGATE_STRONG_MODEL` | Optional: a stronger, slower model for tasks that failed twice |
| `DELEGATE_ALT_MODEL` | Optional: another copy to use when the default is saturated |
| `DELEGATE_MAX_PARALLEL` | How many workers to run at once on one model (default 2) |

Read it with `cat ~/.config/delegate/env` at the start so you know the models and limits.

## 1. Check the workers are reachable

```bash
<skill-dir>/scripts/delegate-run.sh --check
```

If it fails (no settings file, OpenCode missing, model unreachable), tell the user what it printed and
stop. Confirm you are in a git repo and note uncommitted changes: workers start from `HEAD`, so
uncommitted work is not in their worktrees. Ask the user to commit or stash first if it matters.

## 2. Plan, then split

Read the code you need and write the plan as you normally would. Then split it into tasks.

A task is a good fit for a worker when it:
- touches a handful of files that you can name,
- has a clear, checkable outcome (a test, a command, a type check),
- needs no design decisions you have not already made in the spec.

Keep for yourself: architecture and interface decisions, cross-cutting refactors, security-sensitive
code, anything ambiguous, and integration work that spans tasks. Prefer independent tasks so they can
run in parallel; order dependent ones explicitly.

Show the user the task list (name, model, files, how it is verified, which tasks you keep) and get
their OK before dispatching. Workers change code and run commands.

## 3. Write a spec per task

Write each spec to `<repo>.delegate/specs/<name>.md` (outside the repo, so it never gets committed).
Names are short kebab-case and become the branch `delegate/<name>`. Use this shape:

```markdown
# Task: <one line>

## Goal
What should be true when this is done, in one or two sentences.

## Context
- Files to read first: `path/a.py` (what matters in it), `path/b.py`
- Conventions to follow: e.g. existing error type, test style in `tests/test_x.py`

## Change
Precise steps. Name functions, signatures, and files to create or edit.
Include interface decisions you already made (names, types, return values).

## Do not
Files or areas that must not change. No new dependencies.

## Verify
The exact command that must pass, e.g. `pytest tests/test_x.py -q`.
```

Smaller models do best with concrete specs: name things exactly and spell out the interfaces.

## 4. Dispatch

Run each task with the helper, from inside the repo (it finds the repo from the current directory).
Run independent tasks in the background so they work in parallel, up to `DELEGATE_MAX_PARALLEL`
per model:

```bash
<skill-dir>/scripts/delegate-run.sh <repo>.delegate/specs/<name>.md <name> [model]
```

It prints the worker's closing summary, `git status` and a diff stat. The full log is
`<repo>.delegate/<name>.attempt-<n>.log`. Default timeout is 30 minutes (`DELEGATE_TIMEOUT` seconds).

## 5. Review every task

For each finished task, in its worktree `<repo>.delegate/<name>`:
1. Read the full diff (`git -C <worktree> diff`), not just the summary. Check it stays in scope.
2. Run the spec's Verify command yourself, plus any nearby tests.
3. Decide:
   - **Accept**: commit only the files the task should change, never `add -A`, since test runs leave
     artifacts such as `__pycache__/` (`git -C <worktree> add <files> && git -C <worktree> commit -m "<message>"`).
   - **Send back**: append a `## Review feedback (attempt N)` section to the spec with specific
     problems and what to do, then rerun the same command. The worker continues from its last changes.
     At most two send-backs on the default model; a third attempt may go to `DELEGATE_STRONG_MODEL`.
   - **Take over**: fix it yourself in the worktree when the remaining work is small or the worker is stuck.

Do not accept work whose Verify command fails.

## 6. Integrate and clean up

Merge accepted branches into the user's working branch in dependency order
(`git merge --no-ff delegate/<name>` or cherry-pick), resolve conflicts yourself, and run the full
relevant test suite once everything is in. Then remove the worktrees and branches:

```bash
git worktree remove <repo>.delegate/<name> && git branch -d delegate/<name>
```

Leave `<repo>.delegate/` logs and specs in place until the user has seen the report, then offer to delete it.

## 7. Report

Tell the user, per task: model, attempts, outcome (accepted / taken over / dropped), and what you
changed in review. Then what was merged, the final test result, and anything left open.
