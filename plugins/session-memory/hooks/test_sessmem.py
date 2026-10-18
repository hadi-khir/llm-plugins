"""Tests for sessmem.py, the session memory hooks and store, with Haiku replaced by a fake."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sessmem  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
CWD = r"C:\Users\me\repos\lambda-argus"


def user(text, **extra):
    return {"type": "user", "timestamp": "2026-10-12T14:00:00Z", "cwd": CWD, "gitBranch": "main",
            "message": {"role": "user", "content": text}, **extra}


def claude(*blocks):
    return {"type": "assistant", "timestamp": "2026-10-12T14:05:00Z",
            "message": {"role": "assistant", "content": list(blocks)}}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


SESSION = [
    user("fix the radar tee <system-reminder>injected context</system-reminder>"),
    claude({"type": "thinking", "thinking": "private"},
           {"type": "text", "text": "Looking at the tee."},
           {"type": "tool_use", "name": "Edit", "input": {"file_path": "ops/tee.py", "old_string": "x"}}),
    tool_result("HUGE TOOL OUTPUT"),
    user("Launching skill body", isMeta=True),
    {"type": "ai-title", "aiTitle": "Radar tee fix"},
    {"type": "custom-title", "customTitle": "EchoGuard tee storm"},
    {"type": "pr-link", "prUrl": "https://github.com/o/r/pull/9"},
    user("also add a watchdog"),
    user("ship it"),
]


def fake_summary(prompt):
    return "**Goal:** fix tee\n**Outcome:** done - watchdog added\nTOPICS: radar, tee"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SESSMEM_HOME": str(root / "store"),
                                                "CLAUDE_CONFIG_DIR": str(root / "claude")})
        self.env.start()
        os.environ.pop(sessmem.CHILD_ENV, None)
        self.transcript = root / "claude" / "projects" / "proj" / f"{SID}.jsonl"
        self.transcript.parent.mkdir(parents=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, entries, mode="w"):
        with open(self.transcript, mode, encoding="utf-8") as f:
            f.writelines(json.dumps(e) + "\n" for e in entries)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sessmem.main(list(argv))
        return out.getvalue()


class TranscriptTests(StoreCase):
    def test_keeps_prompts_replies_and_tool_lines_only(self):
        self.write(SESSION)
        meta = sessmem.parse(self.transcript, 0)
        self.assertIn("USER: fix the radar tee", meta["text"])
        self.assertIn("CLAUDE: Looking at the tee.", meta["text"])
        self.assertIn("  [Edit] ops/tee.py", meta["text"])
        for dropped in ("injected context", "HUGE TOOL OUTPUT", "Launching skill", "private"):
            self.assertNotIn(dropped, meta["text"])
        self.assertEqual(meta["prompts"], 3)
        self.assertEqual(sessmem.title_of(meta), "EchoGuard tee storm")
        self.assertEqual(meta["prs"], ["https://github.com/o/r/pull/9"])

    def test_partial_trailing_line_is_left_for_next_run(self):
        self.write(SESSION)
        complete = self.transcript.stat().st_size
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write('{"type": "user", "message": {"content": "half wri')
        self.assertEqual(sessmem.parse(self.transcript, 0)["size"], complete)

    def test_redacts_tokens_and_assigned_secrets(self):
        text = sessmem.redact("key ghp_abcdefghijklmnopqrstuvwxyz0123456789 and password=hunter22x")
        self.assertNotIn("ghp_abc", text)
        self.assertNotIn("hunter22x", text)
        self.assertIn("password=[REDACTED]", text)

    def test_worktrees_fold_into_their_repo(self):
        self.assertEqual(sessmem.project_of(r"C:\r\lambda-argus\.claude\worktrees\x"), "lambda-argus")
        self.assertEqual(sessmem.project_of("/home/me/repos/oz/"), "oz")


class IndexTests(StoreCase):
    def test_indexed_session_is_searchable_and_shown(self):
        self.write(SESSION)
        with mock.patch.object(sessmem, "summarize", side_effect=fake_summary):
            sessmem.index(self.transcript)
        hits = self.run_cli("search", "watchdog")
        self.assertIn("2026-10-12__lambda-argus__echoguard-tee-storm", hits)
        shown = self.run_cli("show", SID[:8])
        self.assertIn("topics: radar, tee", shown)
        self.assertNotIn("TOPICS:", shown)

    def test_second_run_sends_only_new_text_with_prior_summary(self):
        self.write(SESSION)
        with mock.patch.object(sessmem, "summarize", side_effect=fake_summary):
            sessmem.index(self.transcript)
        self.write([user("now add the alert rule")], mode="a")
        with mock.patch.object(sessmem, "summarize", side_effect=fake_summary) as call:
            sessmem.index(self.transcript)
        prompt = call.call_args.args[0]
        self.assertIn("now add the alert rule", prompt)
        self.assertNotIn("fix the radar tee", prompt)
        self.assertIn("**Outcome:** done - watchdog added\nTOPICS: radar, tee", prompt)

    def test_short_session_is_stored_without_a_summary_call(self):
        self.write(SESSION[:2])
        with mock.patch.object(sessmem, "summarize") as call:
            sessmem.index(self.transcript)
        call.assert_not_called()
        with sessmem.store() as db:
            self.assertEqual(sessmem.recent(db, None, 5), [])
        self.assertIn("too short", self.run_cli("show", SID))

    def test_long_single_prompt_session_is_summarized(self):
        self.write([user("audit the repo"), claude({"type": "text", "text": "finding " * 200})])
        with mock.patch.object(sessmem, "summarize", side_effect=fake_summary) as call:
            sessmem.index(self.transcript)
        call.assert_called_once()
        self.assertIn("watchdog added", self.run_cli("show", SID))


class HookTests(StoreCase):
    def test_session_end_inside_the_summarizer_spawns_nothing(self):
        self.write(SESSION)
        payload = {"transcript_path": str(self.transcript)}
        with mock.patch.object(sessmem, "spawn") as spawn:
            with mock.patch.dict(os.environ, {sessmem.CHILD_ENV: "1"}):
                sessmem.hook_end(payload)
            spawn.assert_not_called()
            sessmem.hook_end(payload)
            spawn.assert_called_once_with("index", str(self.transcript))

    def test_session_start_lists_this_projects_recent_sessions(self):
        self.write(SESSION)
        with mock.patch.object(sessmem, "summarize", side_effect=fake_summary):
            sessmem.index(self.transcript)
        out = io.StringIO()
        with mock.patch.object(sessmem, "spawn"), contextlib.redirect_stdout(out):
            sessmem.hook_start({"session_id": "other", "cwd": CWD + r"\.claude\worktrees\w"})
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("EchoGuard tee storm - done - watchdog added", context)
        self.assertIn("search <words>", context)


if __name__ == "__main__":
    unittest.main()
