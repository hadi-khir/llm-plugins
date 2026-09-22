# Jev plan gate — design

A Claude Code hook that scores every plan-mode plan with TypeSafe's Jev before
Claude may leave plan mode. Claude writes candidates and steps; Jev returns
calibrated judgments; code applies thresholds and bounces weak plans back with
the failing items. Jev never generates text and never plans.

## Flow

1. Claude enters plan mode and writes a plan with two required sections:
   `## Candidates` (2–3 approaches, each a `### <name>` heading with what
   changes, files touched, new-code size, risk; then one `Chosen: <name>` line)
   and `## Plan` (numbered steps, each naming how it is checked).
2. Claude calls `ExitPlanMode`. A `PreToolUse` hook matched on `ExitPlanMode`
   receives the plan under `tool_input.plan` and the session transcript path.
3. The hook reads the original request: the first user entry in the transcript
   whose content is a string, is not `isMeta`, does not start with `<`, and is
   at least 20 characters. Fallback: the plan's first paragraph.
4. One `POST https://api.typesafe.ai/v1/systemone` (model `jev-latest`,
   standard library `urllib`) carries every question at once.
5. Code applies thresholds. Pass → allow. Fail → deny with the failing items
   and their probabilities as `permissionDecisionReason`.
6. A per-session counter (temp dir, keyed by `session_id`) caps denies at 3;
   the fourth attempt passes with a note. Missing key, timeout, or an HTTP
   error after two retries → allow with a one-line stderr note. An outage
   never blocks planning.

## Plan parsing (code, no Jev)

- Candidates: text under `## Candidates`, split on `### ` headings; the
  heading is the name. `Chosen: <name>` selects one. Optional
  `Why not <name>: <reason>` lines are collected per name.
- Steps: lines under `## Plan` matching `^\s*\d+[.)]\s+`; following
  non-numbered lines join the step above.
- Requirements: the request split on sentence ends and newlines, keeping
  pieces of at least four words.
- A plan missing either section, a `Chosen:` line, or any numbered step is
  denied without calling Jev, and the deny message shows the required shape.
  That deny counts toward the cap.

## State sent to Jev

```json
{
  "request": "...",
  "requirements": ["...", "..."],
  "candidates": [{"name": "A", "text": "..."}],
  "chosen": "A",
  "why_not": {"B": "..."},
  "steps": ["1. ...", "2. ..."]
}
```

## Questions (one request; ids are for code only)

Rank:

- `best_candidate` (choice over candidate names): "Which entry in
  `candidates` solves `request` with the least new code and the lowest risk of
  breaking behavior that other code depends on?"
- `cand_{i}_solves` (noul): "Does `candidates[i].text` describe a change that,
  once done, produces everything asked for in `request`?" true: every requested
  outcome is produced; false: at least one requested outcome is missing or
  deferred.
- `cand_{i}_size` (score): "How much new code does `candidates[i].text` add?"
  levels: edits a few lines in existing files / adds a helper or small block
  inside an existing file / adds a new file or module / adds a new package,
  service, or external dependency.
- `cand_{i}_risk` (score): "How likely is `candidates[i].text` to change
  behavior that other parts of the system depend on?" levels: touches only
  code with no other callers / touches shared code but keeps its behavior /
  changes shared behavior, a config other components read, or a validation
  or security gate.

Verify the chosen plan:

- `req_{j}_covered` (noul): "Is there at least one entry in `steps` whose
  action produces what `requirements[j]` asks for?" true: a step's action
  directly produces it; false: no step produces it, or a step only mentions it.
- `scope_creep` (noul): "Does any entry in `steps` do work that `request` did
  not ask for and that is not needed to complete what was asked?"
- `unsafe_without_guard` (noul): "Does any entry in `steps` change production
  configuration, a validation or authentication boundary, or a tasking or
  countermeasure gate, without a step that tests or verifies that change?"
- `step_{k}_needed` (noul): "Is `steps[k]` required to complete `request`?"
  true: removing it leaves part of `request` undone; false: it could be
  removed and `request` would still be fully done.
- `step_{k}_checked` (noul): "Does `steps[k]` either name a test, command, or
  observable result that shows it worked, or consist only of an action so
  small that no check is needed (a commit, a rename, a one-line doc edit)?"
- `step_{k}_altitude` (score): "What kind of code does `steps[k]` add?"
  levels: reuses or edits code already in the repository / uses the standard
  library or a native platform feature / adds a new helper inside an existing
  file / adds a new module or file / adds a new external dependency.

Repo extras: if `<cwd>/.claude/jev-rubric.json` exists, each entry
`{"id", "instructions", "criteria"?, "action": "block"|"warn", "above": p}`
becomes one more noul over the same state.

## Decisions (thresholds live in `rubric.json`)

Block when any holds:

- any `req_{j}_covered` < 0.6
- `scope_creep` > 0.6
- `unsafe_without_guard` > 0.5
- any `step_{k}_needed` < 0.4
- any `step_{k}_checked` < 0.4
- `best_candidate` ≠ `chosen` with confidence ≥ 0.6 and no `Why not <best>:`
  line present
- any repo extra with `action: block` above its threshold

Warn (appended to the deny message, or ignored on pass): any step with
altitude ≥ 3 (new module or dependency); any repo extra with `action: warn`.

## Deny message

```
Jev plan gate — bounce 1 of 3. Fix these and exit plan mode again.
Not covered by any step:
  - "make replay read the same bearing as live" (0.31)
Steps not needed for the request (drop, or restate the request):
  - 7. Add a settings page for bearing offsets (0.18)
Steps with no check named:
  - 3. Rewrite the azimuth normaliser (0.22)
Jev prefers candidate B over A (0.72). Switch, or add "Why not B: <reason>".
Advisory: step 5 adds a new module. A helper in an existing file may do.
```

## Files

```
llm-plugins/plugins/jev-planning/
  .claude-plugin/plugin.json        manifest (marketplace entry later)
  hooks/plan_gate.py                the hook; also `--dry-run <plan.md> "<request>"`
  hooks/rubric.json                 question wording, levels, thresholds
  hooks/test_plan_gate.py           unittest, canned Jev replies
lambda-argus/.claude/jev-rubric.json   repo extras (below)
~/.claude/settings.json             PreToolUse hook entry (absolute path)
~/.claude/CLAUDE.md                 one line: the two required plan sections
```

Hook entry:

```json
"hooks": {"PreToolUse": [{"matcher": "ExitPlanMode", "hooks": [{
  "type": "command", "timeout": 90,
  "command": "python C:/Users/HKhir/repos/llm-plugins/plugins/jev-planning/hooks/plan_gate.py"
}]}]}
```

`TYPESAFE_API_KEY` comes from the user environment. The hook reads it from
`os.environ` only.

lambda-argus extras (`.claude/jev-rubric.json`):

- `tests_outside_tests_dir` (block > 0.6): "Does any entry in `steps` create
  or move a Python test file to a path not under the top-level `tests/`
  directory?"
- `gate_weakened` (block > 0.5): "Does any entry in `steps` remove or loosen a
  validation check, a tasking gate, or a countermeasure gate?"
- `prod_touched` (warn > 0.6): "Does any entry in `steps` edit a file or host
  whose name contains `prod`, or deploy to the prod hub?"

## Tests

`python -m unittest plugins/jev-planning/hooks/test_plan_gate.py`, with
`ask_jev` replaced by canned answers:

1. Parses candidates, chosen, why-not lines, and multi-line steps.
2. Denies a plan without the required sections and names them.
3. Blocks on one uncovered requirement and quotes it.
4. Passes a clean plan.
5. Candidate mismatch blocks; the same plan with a `Why not` line passes.
6. Fourth attempt passes after three denies.
7. No key → allow with a stderr note; HTTP error → allow with a note.

Live check: set the key, restart Claude Code, open plan mode on a small real
task, watch one bounce and one pass. Then run `--dry-run` on two past plans
from the transcripts to tune thresholds.

## Later, not now

- Prompt-submit triage hook (spike / bounded / architectural; touches prod) as
  injected context. Same script, second event.
- GSD `PLAN.md` gate with the same rubric.
- Marketplace entry so other machines install it as a plugin.
