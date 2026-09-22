# Jev Plan Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code hook that scores every plan-mode plan with TypeSafe's Jev and bounces weak plans back with the failing items, capped at three bounces per session.

**Architecture:** One standard-library Python script runs as a `PreToolUse` hook matched on `ExitPlanMode`. It parses the plan into candidates and steps, recovers the original request from the session transcript, sends one batched Jev request, applies thresholds from `rubric.json`, and prints a deny decision or nothing. A per-session counter in the temp dir caps denies; any Jev failure lets the exit through.

**Tech Stack:** Python 3.11, `urllib`, `unittest`. TypeSafe HTTP API `POST /v1/systemone`, model `jev-latest`. No third-party packages.

**Spec:** `docs/superpowers/specs/2026-09-22-jev-plan-gate-design.md`

## Global Constraints

- Standard library only; no SDK, no pip installs.
- API key read from `os.environ["TYPESAFE_API_KEY"]` only; never written to disk.
- On a pass the hook prints nothing to stdout, so the operator's normal plan-approval prompt is unchanged.
- Missing key, timeout, or HTTP error after two retries on 429/529 → allow with one stderr line. Any unexpected exception → allow with one stderr line.
- Max 3 denies per session; the fourth attempt returns `permissionDecision: "ask"` with the open findings.
- Thresholds and question wording live in `hooks/rubric.json`, never in code.
- Hook commands run under Git Bash on this Windows box; stdin and stdout are reconfigured to UTF-8 in code.

---

### Task 1: Hook script, rubric, and tests

**Files:**
- Create: `plugins/jev-planning/hooks/rubric.json`
- Create: `plugins/jev-planning/hooks/plan_gate.py`
- Create: `plugins/jev-planning/hooks/test_plan_gate.py`
- Create: `plugins/jev-planning/.claude-plugin/plugin.json`

**Interfaces:**
- Produces: `parse_plan(plan: str, request: str) -> dict` with keys `request, requirements, candidates, chosen, why_not, steps`; `gate(plan, request, cwd, rubric, ask=None) -> (blocks: list[str], warns: list[str])`; `run_hook(payload: dict, rubric=None, ask=None) -> int`; `ask_jev(state, questions, model) -> dict` raising `JevUnavailable`; `main(argv)`.
- Consumes: hook stdin JSON with `session_id, cwd, tool_name, tool_input.plan, transcript_path`.

- [ ] **Step 1: Write the rubric**

`plugins/jev-planning/hooks/rubric.json`:

```json
{
  "model": "jev-latest",
  "max_bounces": 3,
  "thresholds": {
    "covered_min": 0.6,
    "scope_creep_max": 0.6,
    "unsafe_max": 0.5,
    "needed_min": 0.4,
    "checked_min": 0.4,
    "mismatch_confidence_min": 0.6,
    "altitude_warn_min": 3
  },
  "questions": {
    "best_candidate": {
      "type": "choice",
      "instructions": "Which entry in `candidates` solves `request` with the least new code and the lowest risk of breaking behavior that other code depends on?"
    },
    "cand_solves": {
      "type": "noul",
      "instructions": "Does `candidates[{i}].text` describe a change that, once done, produces everything asked for in `request`?",
      "criteria": {
        "true": "Every outcome asked for in `request` is produced by this candidate",
        "false": "At least one outcome asked for in `request` is missing or deferred"
      }
    },
    "cand_size": {
      "type": "score",
      "instructions": "How much new code does `candidates[{i}].text` add?",
      "criteria": [
        "Edits a few lines in existing files",
        "Adds a helper function or a small block inside an existing file",
        "Adds a new file or module",
        "Adds a new package, service, or external dependency"
      ]
    },
    "cand_risk": {
      "type": "score",
      "instructions": "How likely is `candidates[{i}].text` to change behavior that other parts of the system depend on?",
      "criteria": [
        "Touches only code that nothing else calls",
        "Touches shared code but keeps its existing behavior",
        "Changes shared behavior, a config that other components read, or a validation or security gate"
      ]
    },
    "req_covered": {
      "type": "noul",
      "instructions": "Is there at least one entry in `steps` whose action produces what `requirements[{j}]` asks for?",
      "criteria": {
        "true": "A step's action directly produces it",
        "false": "No step produces it, or a step only mentions it without doing it"
      }
    },
    "scope_creep": {
      "type": "noul",
      "instructions": "Does any entry in `steps` do work that `request` did not ask for and that is not needed to complete what `request` asked for?",
      "criteria": {
        "true": "At least one step adds unrequested, unnecessary work",
        "false": "Every step serves what `request` asked for"
      }
    },
    "unsafe_without_guard": {
      "type": "noul",
      "instructions": "Does any entry in `steps` change production configuration, a validation or authentication boundary, or a tasking or countermeasure gate, without another entry in `steps` that tests or verifies that change?",
      "criteria": {
        "true": "Such a change exists and no step verifies it",
        "false": "No such change, or every such change has a step that verifies it"
      }
    },
    "step_needed": {
      "type": "noul",
      "instructions": "Is `steps[{k}]` required to complete `request`?",
      "criteria": {
        "true": "Removing this step would leave part of `request` undone",
        "false": "This step could be removed and `request` would still be fully done"
      }
    },
    "step_checked": {
      "type": "noul",
      "instructions": "Does `steps[{k}]` either name a test, command, or observable result that shows it worked, or consist only of an action so small that no check is needed, such as a commit, a rename, or a one-line documentation edit?",
      "criteria": {
        "true": "It names its check, or it is too small to need one",
        "false": "It changes behavior and names no way to confirm the change worked"
      }
    },
    "step_altitude": {
      "type": "score",
      "instructions": "What kind of code does `steps[{k}]` add?",
      "criteria": [
        "Reuses or edits code that already exists in the repository",
        "Uses the standard library or a native platform feature",
        "Adds a new helper inside an existing file",
        "Adds a new module or file",
        "Adds a new external dependency"
      ]
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

`plugins/jev-planning/hooks/test_plan_gate.py`:

```python
"""Tests for plan_gate.py, the Jev plan gate hook, with Jev replaced by canned answers."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_gate  # noqa: E402

REQUEST = "Add a --json flag to the audit script. Make the flag print one object per hub."
PLAN = """# Add --json to the audit script

## Candidates

### Flag in the script
Add an argparse flag and a json.dumps branch in the existing script. Files: scripts/hub_audit.py. Size: a few lines. Risk: none.

### New reporter module
Add reporters/json_reporter.py and route output through it. Files: new module plus the script. Size: new module. Risk: low.

Chosen: Flag in the script

## Plan

1. Add the flag and the JSON branch in scripts/hub_audit.py.
   Check: run the script with --json and pipe it through python -m json.tool.
2. Add one test in tests/ops/test_hub_audit.py that parses the output.
   Check: pytest tests/ops/test_hub_audit.py.
3. Commit.
"""


def canned(state, questions, model, **overrides):
    """Good answers for every question, then apply overrides by question id."""
    answers = {}
    for key, q in questions.items():
        if q["type"] == "noul":
            bad = key.startswith(("scope_creep", "unsafe", "extra_"))
            answers[key] = {"type": "noul", "noul": 0.1 if bad else 0.9}
        elif q["type"] == "score":
            legend = {str(i): level for i, level in enumerate(q["criteria"])}
            answers[key] = {"type": "score", "score": 1.0, "legend": legend, "probabilities": {}, "confidence": 0.9}
        else:
            probs = {n: (0.9 if n == state["chosen"] else 0.1) for n in q["criteria"]}
            answers[key] = {"type": "choice", "choice": state["chosen"], "probabilities": probs, "confidence": 0.9}
    for key, patch in overrides.items():
        answers[key].update(patch)
    return answers


def asker(**overrides):
    return lambda state, questions, model: canned(state, questions, model, **overrides)


class ParsePlan(unittest.TestCase):
    def test_parses_candidates_chosen_why_not_and_multiline_steps(self):
        state = plan_gate.parse_plan(PLAN + "\nWhy not New reporter module: too big.\n", REQUEST)
        self.assertEqual([c["name"] for c in state["candidates"]], ["Flag in the script", "New reporter module"])
        self.assertEqual(state["chosen"], "Flag in the script")
        self.assertEqual(state["why_not"], {"New reporter module": "too big."})
        self.assertEqual(len(state["steps"]), 3)
        self.assertIn("Check: run the script with --json", state["steps"][0])
        self.assertEqual(len(state["requirements"]), 2)

    def test_tolerates_bold_markers(self):
        state = plan_gate.parse_plan(PLAN.replace("Chosen: Flag in the script", "**Chosen:** Flag in the script"), REQUEST)
        self.assertEqual(state["chosen"], "Flag in the script")


class Gate(unittest.TestCase):
    def setUp(self):
        self.rubric = plan_gate.load_rubric()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def gate(self, plan, ask):
        return plan_gate.gate(plan, REQUEST, self.tmp.name, self.rubric, ask)

    def test_missing_sections_deny_without_calling_jev(self):
        never = mock.Mock(side_effect=AssertionError("Jev must not be called"))
        blocks, _ = self.gate("# Plan\n\nJust do it.\n", never)
        self.assertIn("Candidates", blocks[0])
        self.assertIn("Chosen", blocks[0])
        self.assertIn("numbered steps", blocks[0])

    def test_uncovered_requirement_blocks_and_is_quoted(self):
        blocks, _ = self.gate(PLAN, asker(req_covered_1={"noul": 0.2}))
        self.assertEqual(len(blocks), 1)
        self.assertIn("Make the flag print one object per hub.", blocks[0])
        self.assertIn("(0.20)", blocks[0])

    def test_clean_plan_passes(self):
        self.assertEqual(self.gate(PLAN, asker()), ([], []))

    def test_candidate_mismatch_blocks_unless_justified(self):
        other = {"choice": "New reporter module",
                 "probabilities": {"Flag in the script": 0.2, "New reporter module": 0.8}, "confidence": 0.8}
        blocks, _ = self.gate(PLAN, asker(best_candidate=other))
        self.assertIn("Why not New reporter module", blocks[0])
        justified = PLAN + "\nWhy not New reporter module: a module for one flag is too much.\n"
        self.assertEqual(self.gate(justified, asker(best_candidate=other))[0], [])

    def test_new_module_step_is_advisory_only(self):
        blocks, warns = self.gate(PLAN, asker(step_altitude_0={"score": 3.2}))
        self.assertEqual(blocks, [])
        self.assertIn("altitude 3.2", warns[0])

    def test_repo_extra_can_block(self):
        extras_dir = Path(self.tmp.name) / ".claude"
        extras_dir.mkdir()
        (extras_dir / "jev-rubric.json").write_text(json.dumps([{
            "id": "gate_weakened", "action": "block", "above": 0.5,
            "instructions": "Does any entry in `steps` loosen a gate?",
            "message": "A step loosens a gate."}]), encoding="utf-8")
        blocks, _ = self.gate(PLAN, asker(extra_gate_weakened={"noul": 0.9}))
        self.assertEqual(blocks, ["A step loosens a gate. (0.90)"])


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        transcript = Path(self.tmp.name) / "t.jsonl"
        lines = [
            {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>x</local-command-caveat>"}},
            {"type": "user", "message": {"content": REQUEST}},
        ]
        transcript.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        self.payload = {"session_id": "s1", "cwd": self.tmp.name, "tool_name": "ExitPlanMode",
                        "transcript_path": str(transcript), "tool_input": {"plan": PLAN}}
        patcher = mock.patch.object(plan_gate.tempfile, "gettempdir", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, ask):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = plan_gate.run_hook(self.payload, ask=ask)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def decision(stdout):
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] if stdout.strip() else None

    def test_reads_request_from_transcript(self):
        seen = {}

        def ask(state, questions, model):
            seen.update(state)
            return canned(state, questions, model)

        self.run_hook(ask)
        self.assertEqual(seen["request"], REQUEST)

    def test_pass_prints_nothing(self):
        self.assertEqual(self.run_hook(asker())[:2], (0, ""))

    def test_three_denies_then_ask_then_counter_resets(self):
        bad = asker(req_covered_0={"noul": 0.1})
        first = self.run_hook(bad)[1]
        self.assertIn("bounce 1 of 3", first)
        decisions = [self.decision(first)] + [self.decision(self.run_hook(bad)[1]) for _ in range(3)]
        self.assertEqual(decisions, ["deny", "deny", "deny", "ask"])
        self.assertEqual(self.decision(self.run_hook(bad)[1]), "deny")

    def test_fails_open_when_jev_unavailable(self):
        def down(state, questions, model):
            raise plan_gate.JevUnavailable("HTTP 503")

        code, out, err = self.run_hook(down)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("skipped (HTTP 503)", err)

    def test_fails_open_without_key(self):
        with mock.patch.dict(plan_gate.os.environ, {}, clear=True):
            code, out, err = self.run_hook(None)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("TYPESAFE_API_KEY", err)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd C:/Users/HKhir/repos/llm-plugins && python -m unittest plugins/jev-planning/hooks/test_plan_gate.py -v`
Expected: import error, `No module named 'plan_gate'`.

- [ ] **Step 4: Write the hook**

`plugins/jev-planning/hooks/plan_gate.py`:

```python
"""Jev plan gate: a Claude Code PreToolUse hook on ExitPlanMode.

Reads the plan from the hook payload and the original request from the
session transcript, asks TypeSafe's Jev one batch of typed questions, applies
the thresholds in rubric.json, and denies the plan-mode exit with the failing
items when the plan falls short. Denies are capped per session so a plan can
never loop forever; a missing key or any Jev failure lets the exit through,
because an outage must never block planning. On a pass the hook prints
nothing, so the operator's normal plan-approval prompt is unchanged.

Also runnable as ``plan_gate.py --dry-run <plan.md> ["<request>"]`` to print
the state, every answer, and the verdict without the hook protocol.
"""

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
HERE = Path(__file__).resolve().parent
MAX_REQUIREMENTS = 20  # ponytail: hard cap on request sentences; rank by relevance if requests outgrow it
MAX_TEXT = 2000  # ponytail: per-candidate/step truncation keeps state far below Jev's 32k-token budget
SHAPE_HELP = (
    "Jev plan gate: the plan needs two sections before it can be scored.\n"
    "## Candidates — 2 to 3 approaches, each as `### <name>` with what changes, files touched, "
    "new-code size, and risk; then one line `Chosen: <name>`.\n"
    "## Plan — numbered steps, each naming its check (a test, a command, or an observable result)."
)


class JevUnavailable(Exception):
    """Jev could not answer: no key, network failure, or a non-retryable HTTP error."""


def section(plan, title):
    """Text under ``## <title>`` up to the next ``## `` heading, or an empty string."""
    m = re.search(rf"^##\s+{re.escape(title)}\b[^\n]*\n(.*?)(?=^##\s|\Z)", plan, re.M | re.S)
    return m.group(1) if m else ""


def parse_candidates(plan):
    out = []
    for m in re.finditer(r"^###\s+(.+?)\s*$\n(.*?)(?=^###\s|\Z)", section(plan, "Candidates"), re.M | re.S):
        name = re.sub(r"\s*\((chosen|recommended)\)\s*$", "", m.group(1), flags=re.I)
        out.append({"name": name.strip("*_` "), "text": m.group(2).strip()[:MAX_TEXT]})
    return out


def parse_steps(plan):
    steps = []
    for line in section(plan, "Plan").splitlines():
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)", line)
        if m:
            steps.append(f"{m.group(1)}. {m.group(2).strip()}")
        elif steps and line.strip() and not line.lstrip().startswith("#"):
            steps[-1] = (steps[-1] + " " + line.strip())[:MAX_TEXT]
    return steps


def parse_marker(plan, label):
    """``<label> [name]: value`` lines as (name, value), tolerating markdown bold on either side."""
    pattern = rf"^[\s*_]*{label}\s*(.*?)[\s*_]*:[\s*_]*(.+?)\s*$"
    return [(m.group(1).strip("*_` "), m.group(2).strip("*_ ")) for m in re.finditer(pattern, plan, re.M | re.I)]


def split_requirements(request):
    pieces = re.split(r"(?<=[.!?])\s+|\n+", request or "")
    return [p.strip(" -*") for p in pieces if len(p.split()) >= 4][:MAX_REQUIREMENTS]


def parse_plan(plan, request):
    chosen = parse_marker(plan, "Chosen")
    return {
        "request": request or "",
        "requirements": split_requirements(request),
        "candidates": parse_candidates(plan),
        "chosen": chosen[0][1] if chosen else "",
        "why_not": dict(parse_marker(plan, "Why not")),
        "steps": parse_steps(plan),
    }


def shape_problems(state):
    missing = []
    if len(state["candidates"]) < 2:
        missing.append("a `## Candidates` section with 2 to 3 `### <name>` approaches")
    if not state["chosen"]:
        missing.append("a `Chosen: <name>` line")
    if not state["steps"]:
        missing.append("a `## Plan` section with numbered steps")
    return missing


def original_request(transcript_path):
    """The first substantive prompt the user typed this session, or None."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str) and not content.lstrip().startswith("<") and len(content.strip()) >= 20:
                    return content.strip()
    except OSError:
        return None
    return None


def first_paragraph(plan):
    for para in re.split(r"\n\s*\n", plan):
        if para.strip() and not para.lstrip().startswith("#"):
            return para.strip()
    return plan.strip()[:500]


def load_rubric():
    return json.loads((HERE / "rubric.json").read_text(encoding="utf-8"))


def load_extras(cwd):
    path = Path(cwd) / ".claude" / "jev-rubric.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def build_questions(state, rubric, extras):
    """One question per candidate, requirement, and step dimension, all over the same state."""
    templates = rubric["questions"]

    def fill(key, **index):
        q = dict(templates[key])
        q["instructions"] = q["instructions"].format(**index)
        return q

    out = {}
    names = [c["name"] for c in state["candidates"]]
    if len(names) >= 2:
        out["best_candidate"] = {**templates["best_candidate"], "criteria": {n: None for n in names}}
    for i in range(len(names)):
        for key in ("cand_solves", "cand_size", "cand_risk"):
            out[f"{key}_{i}"] = fill(key, i=i)
    for j in range(len(state["requirements"])):
        out[f"req_covered_{j}"] = fill("req_covered", j=j)
    out["scope_creep"] = fill("scope_creep")
    out["unsafe_without_guard"] = fill("unsafe_without_guard")
    for k in range(len(state["steps"])):
        for key in ("step_needed", "step_checked", "step_altitude"):
            out[f"{key}_{k}"] = fill(key, k=k)
    for extra in extras:
        q = {"type": "noul", "instructions": extra["instructions"]}
        if extra.get("criteria"):
            q["criteria"] = extra["criteria"]
        out[f"extra_{extra['id']}"] = q
    return out


def ask_jev(state, questions, model):
    """One POST carrying every question; retries 429/529 twice, fails fast on anything else."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevUnavailable("TYPESAFE_API_KEY is not set")
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(API_URL, body, headers), timeout=30) as resp:
                return json.load(resp)["answers"]
        except urllib.error.HTTPError as err:
            if err.code not in (429, 529) or attempt == 2:
                raise JevUnavailable(f"HTTP {err.code}: {err.read(300).decode('utf-8', 'replace')}") from err
        except OSError as err:
            if attempt == 2:
                raise JevUnavailable(str(err)) from err
        time.sleep(1 + 2 * attempt)


def short(text, limit=160):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate(state, answers, rubric, extras):
    """Apply the rubric thresholds; return (blocking findings, advisory findings)."""
    t = rubric["thresholds"]
    blocks, warns = [], []
    reqs, steps = state["requirements"], state["steps"]

    def noul(key):
        return answers[key]["noul"]

    def listing(header, items):
        return header + "\n" + "\n".join(f"  - {item}" for item in items)

    bad = [f'"{reqs[j]}" ({noul(f"req_covered_{j}"):.2f})' for j in range(len(reqs)) if noul(f"req_covered_{j}") < t["covered_min"]]
    if bad:
        blocks.append(listing("Not covered by any step:", bad))
    if noul("scope_creep") > t["scope_creep_max"]:
        blocks.append(f"Steps do work the request did not ask for ({noul('scope_creep'):.2f}). Drop them, or restate the request.")
    if noul("unsafe_without_guard") > t["unsafe_max"]:
        blocks.append(f"A step changes prod config, a validation boundary, or a tasking gate with no verifying step ({noul('unsafe_without_guard'):.2f}).")
    bad = [f'{short(steps[k])} ({noul(f"step_needed_{k}"):.2f})' for k in range(len(steps)) if noul(f"step_needed_{k}") < t["needed_min"]]
    if bad:
        blocks.append(listing("Steps not needed for the request (drop, or restate the request):", bad))
    bad = [f'{short(steps[k])} ({noul(f"step_checked_{k}"):.2f})' for k in range(len(steps)) if noul(f"step_checked_{k}") < t["checked_min"]]
    if bad:
        blocks.append(listing("Steps with no check named:", bad))
    best = answers.get("best_candidate")
    if (best and best["choice"].lower() != state["chosen"].lower() and best["confidence"] >= t["mismatch_confidence_min"]
            and best["choice"].lower() not in {n.lower() for n in state["why_not"]}):
        blocks.append(f'Jev prefers candidate "{best["choice"]}" over "{state["chosen"]}" ({best["probabilities"][best["choice"]]:.2f}). '
                      f'Switch, or add a line "Why not {best["choice"]}: <reason>".')
    for k in range(len(steps)):
        a = answers[f"step_altitude_{k}"]
        if a["score"] >= t["altitude_warn_min"]:
            level = a["legend"].get(str(round(a["score"])), "").lower()
            warns.append(f'{short(steps[k])} — {level} (altitude {a["score"]:.1f}). A smaller change may do.')
    for extra in extras:
        p = noul(f"extra_{extra['id']}")
        if p > extra["above"]:
            (blocks if extra.get("action") == "block" else warns).append(f'{extra.get("message", extra["instructions"])} ({p:.2f})')
    return blocks, warns


def gate(plan, request, cwd, rubric, ask=None):
    """Verdict for one plan as (blocks, warns); raises JevUnavailable."""
    state = parse_plan(plan, request)
    missing = shape_problems(state)
    if missing:
        return [SHAPE_HELP + "\nMissing: " + "; ".join(missing) + "."], []
    extras = load_extras(cwd)
    answers = (ask or ask_jev)(state, build_questions(state, rubric, extras), rubric["model"])
    return evaluate(state, answers, rubric, extras)


def counter_file(session_id):
    folder = Path(tempfile.gettempdir()) / "jev-plan-gate"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{session_id or 'nosession'}.count"


def bump_bounces(session_id):
    f = counter_file(session_id)
    n = (int(f.read_text() or 0) if f.exists() else 0) + 1
    f.write_text(str(n))
    return n


def reset_bounces(session_id):
    f = counter_file(session_id)
    if f.exists():
        f.unlink()


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}))


def run_hook(payload, rubric=None, ask=None):
    if payload.get("tool_name") != "ExitPlanMode":
        return 0
    rubric = rubric or load_rubric()
    plan = (payload.get("tool_input") or {}).get("plan") or ""
    request = original_request(payload.get("transcript_path")) or first_paragraph(plan)
    session = payload.get("session_id", "")
    try:
        blocks, warns = gate(plan, request, payload.get("cwd") or ".", rubric, ask)
    except JevUnavailable as err:
        print(f"Jev plan gate: skipped ({err})", file=sys.stderr)
        return 0
    if not blocks:
        reset_bounces(session)
        for w in warns:
            print(f"Jev plan gate advisory: {w}", file=sys.stderr)
        return 0
    n = bump_bounces(session)
    findings = "\n".join(blocks + [f"Advisory: {w}" for w in warns])
    if n > rubric["max_bounces"]:
        reset_bounces(session)
        emit("ask", f"Jev plan gate: {n - 1} bounces used; passing this plan to you with its open findings.\n{findings}")
        return 0
    emit("deny", f"Jev plan gate — bounce {n} of {rubric['max_bounces']}. Fix these and exit plan mode again.\n{findings}")
    return 0


def dry_run(plan_path, request):
    rubric = load_rubric()
    plan = Path(plan_path).read_text(encoding="utf-8")
    state = parse_plan(plan, request or first_paragraph(plan))
    print(json.dumps(state, indent=2)[:4000])
    missing = shape_problems(state)
    if missing:
        print("SHAPE:", "; ".join(missing))
        return 1
    extras = load_extras(os.getcwd())
    answers = ask_jev(state, build_questions(state, rubric, extras), rubric["model"])
    for key, a in answers.items():
        value = a.get("choice") if a["type"] == "choice" else f'{a.get("noul", a.get("score")):.2f}'
        conf = f' conf={a["confidence"]:.2f}' if "confidence" in a else ""
        print(f"{key:28} {value}{conf}")
    blocks, warns = evaluate(state, answers, rubric, extras)
    print("\nBLOCK" if blocks else "\nPASS")
    print("\n".join(blocks + [f"warn: {w}" for w in warns]))
    return 1 if blocks else 0


def main(argv):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(argv) >= 3 and argv[1] == "--dry-run":
        return dry_run(argv[2], argv[3] if len(argv) > 3 else None)
    try:
        return run_hook(json.load(sys.stdin))
    except Exception as err:  # a bug in the gate must never block planning
        print(f"Jev plan gate: skipped ({type(err).__name__}: {err})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd C:/Users/HKhir/repos/llm-plugins && python -m unittest plugins/jev-planning/hooks/test_plan_gate.py -v`
Expected: 13 tests, `OK`.

- [ ] **Step 6: Add the plugin manifest**

`plugins/jev-planning/.claude-plugin/plugin.json`:

```json
{
  "$schema": "https://json.schemastore.org/claude-code-plugin-manifest.json",
  "name": "jev-planning",
  "displayName": "Jev Plan Gate",
  "version": "0.1.0",
  "description": "Scores every plan-mode plan with TypeSafe's Jev before Claude may leave plan mode: ranks the candidate approaches, checks each step against the request, and bounces weak plans back with the failing items.",
  "author": {
    "name": "Hadi Khir",
    "email": "khir.hadi@gmail.com"
  },
  "repository": "https://github.com/hadi-khir/llm-plugins",
  "license": "MIT",
  "keywords": ["hook", "planning", "typesafe", "jev"]
}
```

- [ ] **Step 7: Commit**

```bash
cd C:/Users/HKhir/repos/llm-plugins
git add plugins/jev-planning docs/superpowers/plans/2026-09-22-jev-plan-gate.md
git commit -m "feat(jev-planning): plan gate hook scoring plan-mode plans with Jev"
```

---

### Task 2: Wire the hook into this machine

**Files:**
- Modify: `C:/Users/HKhir/.claude/settings.json` (add a `hooks` key; every other key unchanged)
- Create: `C:/Users/HKhir/.claude/CLAUDE.md`
- Create: `C:/Users/HKhir/repos/lambda-argus/.claude/jev-rubric.json` (left untracked; its own PR later)

**Interfaces:**
- Consumes: `plugins/jev-planning/hooks/plan_gate.py` by absolute path.

- [ ] **Step 1: Add the hook entry without disturbing other settings**

Run:

```bash
python - <<'EOF'
import json, pathlib
p = pathlib.Path.home() / ".claude" / "settings.json"
s = json.loads(p.read_text(encoding="utf-8"))
s.setdefault("hooks", {})["PreToolUse"] = [{
    "matcher": "ExitPlanMode",
    "hooks": [{"type": "command", "timeout": 90,
               "command": "python C:/Users/HKhir/repos/llm-plugins/plugins/jev-planning/hooks/plan_gate.py"}]}]
p.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8", newline="\n")
print(json.dumps(s["hooks"], indent=2))
EOF
```

Expected: the printed `hooks` block matches the spec's hook entry. Then `python -c "import json;json.load(open(r'C:/Users/HKhir/.claude/settings.json'))"` prints nothing (valid JSON).

- [ ] **Step 2: Teach Claude the plan shape**

`C:/Users/HKhir/.claude/CLAUDE.md`:

```markdown
# Plan mode

Every plan written in plan mode has two sections, because a Jev hook scores the plan on exit and bounces it (up to 3 times) when they are missing or weak:

- `## Candidates` — 2 to 3 approaches, each `### <name>` with what changes, files touched, new-code size, and risk. Then one line `Chosen: <name>`. To keep a candidate Jev ranks lower, add `Why not <other>: <reason>`.
- `## Plan` — numbered steps, each naming its check (a test, a command, or an observable result).
```

- [ ] **Step 3: Add the lambda-argus repo extras**

`C:/Users/HKhir/repos/lambda-argus/.claude/jev-rubric.json`:

```json
[
  {
    "id": "tests_outside_tests_dir",
    "action": "block",
    "above": 0.6,
    "instructions": "Does any entry in `steps` create or move a Python test file to a path that is not under the top-level `tests/` directory?",
    "message": "A step puts a test file outside tests/ (all Python tests live under tests/)."
  },
  {
    "id": "gate_weakened",
    "action": "block",
    "above": 0.5,
    "instructions": "Does any entry in `steps` remove or loosen a validation check, a tasking gate, or a countermeasure gate?",
    "message": "A step removes or loosens a validation check, tasking gate, or countermeasure gate."
  },
  {
    "id": "prod_touched",
    "action": "warn",
    "above": 0.6,
    "instructions": "Does any entry in `steps` edit a file or host whose name contains `prod`, or deploy to the prod hub?",
    "message": "A step touches prod. Prod changes are operator-initiated; confirm this was asked for."
  }
]
```

- [ ] **Step 4: Smoke the hook protocol end to end without Jev**

Run (no key in this shell, so the hook must fail open and print nothing):

```bash
cd C:/Users/HKhir/repos/lambda-argus
echo '{"session_id":"smoke","cwd":"C:/Users/HKhir/repos/lambda-argus","tool_name":"ExitPlanMode","tool_input":{"plan":"# x\n\n## Candidates\n\n### A\nsmall\n\n### B\nbig\n\nChosen: A\n\n## Plan\n\n1. Do it. Check: run it.\n"}}' \
  | TYPESAFE_API_KEY= python C:/Users/HKhir/repos/llm-plugins/plugins/jev-planning/hooks/plan_gate.py; echo "exit=$?"
```

Expected: stderr `Jev plan gate: skipped (TYPESAFE_API_KEY is not set)`, empty stdout, `exit=0`.

---

### Task 3: Live check against Jev

**Files:** none (verification only).

- [ ] **Step 1: Set the key**

The operator sets `TYPESAFE_API_KEY` as a user environment variable (`setx TYPESAFE_API_KEY <key>` in a PowerShell window) and restarts Claude Code so the hook process inherits it.

- [ ] **Step 2: Dry-run a past plan**

Run, with the key in the shell:

```bash
cd C:/Users/HKhir/repos/lambda-argus
python C:/Users/HKhir/repos/llm-plugins/plugins/jev-planning/hooks/plan_gate.py --dry-run C:/Users/HKhir/.claude/plans/kind-scribbling-plum.md "Investigate Portainer for fleet container visibility and propose whether to adopt it"
```

Expected: `SHAPE: ...` (an old plan without the two sections), proving the shape check and the CLI. Then write a small plan file in the scratchpad with both sections and re-run; expected: one line per answer and `PASS` or `BLOCK` with findings.

- [ ] **Step 3: Live bounce in plan mode**

In a fresh Claude Code session in lambda-argus, ask for a small planned change and enter plan mode. Expected: a plan without the two sections bounces once with the shape message; a complete plan passes or bounces with quoted findings. Tune thresholds in `rubric.json` only if a clean plan bounces on a finding that reads wrong.
