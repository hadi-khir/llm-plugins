"""Tests for sessmem.py, the session memory hooks and store, with Haiku replaced by a fake."""

import contextlib
import io
import json
import os
import re
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


class BrainTests(StoreCase):
    """Two laptops sharing one brain: clones of a local bare repo, each with its own store."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        config = self.root / "gitconfig"
        config.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
        self.git_env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config),
                                                    "GIT_CONFIG_NOSYSTEM": "1"})
        self.git_env.start()
        seed = self.root / "seed"
        sessmem.git(self.root, "init", "-q", "-b", "main", str(seed))
        (seed / ".gitattributes").write_text("MEMORY.md merge=union\n")
        (seed / "memory" / "p").mkdir(parents=True)
        (seed / "memory" / "p" / "MEMORY.md").write_text("- [Base](base.md) - first\n")
        sessmem.git(seed, "add", "-A")
        sessmem.git(seed, "commit", "-q", "-m", "seed")
        self.origin = self.root / "origin.git"
        sessmem.git(self.root, "clone", "-q", "--bare", str(seed), str(self.origin))

    def tearDown(self):
        self.git_env.stop()
        super().tearDown()

    def laptop(self, name):
        clone = self.root / name / "brain"
        sessmem.git(self.root, "clone", "-q", str(self.origin), str(clone))
        return clone

    def on(self, clone):
        return mock.patch.dict(os.environ, {"SESSMEM_HOME": str(clone.parent / "store"),
                                            sessmem.BRAIN_ENV: str(clone)})

    def test_session_summarized_on_one_laptop_is_searchable_on_another(self):
        a, b = self.laptop("a"), self.laptop("b")
        self.write(SESSION)
        with self.on(a), mock.patch.object(sessmem, "summarize", side_effect=fake_summary):
            sessmem.index(self.transcript)
            sessmem.sync()
        self.transcript.unlink()
        with self.on(b):
            sessmem.refresh()
            self.assertIn("2026-10-12__lambda-argus__echoguard-tee-storm", self.run_cli("search", "watchdog"))

    def test_import_keeps_this_laptops_own_row(self):
        a = self.laptop("a")
        self.write(SESSION)
        with self.on(a), mock.patch.object(sessmem, "summarize", side_effect=fake_summary):
            sessmem.index(self.transcript)
            sessmem.sync()
            copy = next((a / "sessions").glob("*.md"))
            stale = copy.read_text(encoding="utf-8").replace("watchdog added", "stale")
            copy.write_text(stale.replace("updated_at: ", "updated_at: 1999"), encoding="utf-8")
            with sessmem.store() as db:
                sessmem.import_sessions(db, a)
            self.assertIn("watchdog added", self.run_cli("show", SID))

    def test_index_lines_added_on_two_laptops_both_survive(self):
        a, b = self.laptop("a"), self.laptop("b")
        for clone, line in ((a, "- [Radar](radar.md) - tee\n"), (b, "- [Hub](hub.md) - dev\n")):
            with open(clone / "memory" / "p" / "MEMORY.md", "a", encoding="utf-8") as f:
                f.write(line)
        for clone in (a, b, a):
            with self.on(clone):
                sessmem.sync()
        for clone in (a, b):
            index = (clone / "memory" / "p" / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn("Radar", index)
            self.assertIn("Hub", index)

    def test_link_moves_notes_into_the_brain_and_points_claude_there(self):
        a = self.laptop("a")
        repo = self.root / "repos" / "argus"
        repo.mkdir(parents=True)
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
        notes = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / slug / "memory"
        notes.mkdir(parents=True)
        (notes / "tee.md").write_text("tee facts\n")
        with self.on(a), contextlib.redirect_stdout(io.StringIO()):
            sessmem.link(str(repo))
        self.assertEqual(os.path.realpath(notes), os.path.realpath(a / "memory" / "argus"))
        self.assertEqual((notes / "tee.md").read_text(), "tee facts\n")
        self.assertTrue((notes.parent / "memory.pre-brain" / "tee.md").is_file())


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
