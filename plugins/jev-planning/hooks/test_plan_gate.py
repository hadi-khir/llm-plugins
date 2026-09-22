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
