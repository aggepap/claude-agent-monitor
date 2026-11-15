import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hook  # noqa: E402


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # Pretend this test process is the claude process; never prune it.
        patcher = mock.patch.object(hook, "find_claude_process", return_value=(os.getpid(), hook.proc_starttime(os.getpid())))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fire(self, event, **fields):
        payload = {"session_id": "s1", "cwd": "/home/u/Projects/shop", "hook_event_name": event, **fields}
        out = io.StringIO()
        code = hook.main(io.StringIO(json.dumps(payload)), out, status_dir=self.dir)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "", "hook must not print anything (it would inject context)")

    def status(self, sid="s1"):
        return json.loads((self.dir / f"{sid}.json").read_text())


class LifecycleTests(HookTestCase):
    def test_session_start_creates_idle_status_with_project(self):
        self.fire("SessionStart", source="startup")
        s = self.status()
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["project"], "shop")
        self.assertEqual(s["cwd"], "/home/u/Projects/shop")
        self.assertIn("updated_at", s)

    def test_prompt_submit_sets_working_and_task_snippet(self):
        self.fire("UserPromptSubmit", prompt="Add a checkout page\nwith stripe " + "x" * 500)
        s = self.status()
        self.assertEqual(s["state"], "working")
        self.assertTrue(s["task"].startswith("Add a checkout page with stripe"))
        self.assertLessEqual(len(s["task"]), 160)

    def test_pre_tool_use_records_activity(self):
        self.fire("UserPromptSubmit", prompt="do it")
        self.fire("PreToolUse", tool_name="Bash", tool_input={"command": "npm test", "description": "Run tests"})
        s = self.status()
        self.assertEqual(s["state"], "working")
        self.assertEqual(s["activity"], "Bash: Run tests")
        self.assertEqual(s["task"], "do it")

        self.fire("PreToolUse", tool_name="Edit", tool_input={"file_path": "/home/u/Projects/shop/app/models.py"})
        self.assertEqual(self.status()["activity"], "Edit: models.py")

    def test_ask_user_question_sets_waiting(self):
        self.fire("PreToolUse", tool_name="AskUserQuestion", tool_input={"questions": [{"question": "Which DB?"}]})
        s = self.status()
        self.assertEqual(s["state"], "waiting")
        self.assertIn("Which DB?", s["message"])

    def test_permission_notification_sets_waiting(self):
        self.fire("UserPromptSubmit", prompt="go")
        self.fire("Notification", message="Claude needs your permission to use Bash", notification_type="permission_prompt")
        s = self.status()
        self.assertEqual(s["state"], "waiting")
        self.assertEqual(s["message"], "Claude needs your permission to use Bash")

    def test_idle_notification_does_not_override_done(self):
        self.fire("Stop", last_assistant_message="All done.")
        self.fire("Notification", message="Claude is waiting for your input", notification_type="idle_prompt")
        self.assertEqual(self.status()["state"], "done")

    def test_post_tool_use_clears_waiting(self):
        self.fire("Notification", message="Claude needs your permission to use Bash", notification_type="permission_prompt")
        self.fire("PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        s = self.status()
        self.assertEqual(s["state"], "working")
        self.assertEqual(s.get("message", ""), "")

    def test_stop_sets_done_with_last_message(self):
        self.fire("UserPromptSubmit", prompt="go")
        self.fire("Stop", last_assistant_message="Implemented the page.\nTests pass.")
        s = self.status()
        self.assertEqual(s["state"], "done")
        self.assertEqual(s["summary"], "Implemented the page. Tests pass.")

    def test_stop_falls_back_to_transcript(self):
        transcript = self.dir / "t.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Finished the refactor."}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}},
        ]
        transcript.write_text("\n".join(json.dumps(l) for l in lines) + "\n{broken")
        self.fire("Stop", transcript_path=str(transcript))
        self.assertEqual(self.status()["summary"], "Finished the refactor.")

    def test_title_is_read_from_transcript(self):
        transcript = self.dir / "t.jsonl"
        lines = [
            {"type": "ai-title", "aiTitle": "Old title", "sessionId": "s1"},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "ai-title", "aiTitle": "Fix \"checkout\" bug", "sessionId": "s1"},
        ]
        transcript.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        self.fire("UserPromptSubmit", prompt="go", transcript_path=str(transcript))
        self.assertEqual(self.status()["title"], 'Fix "checkout" bug')

    def test_custom_title_is_stored(self):
        transcript = self.dir / "t.jsonl"
        lines = [
            {"type": "ai-title", "aiTitle": "Auto", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "my-shop-work", "sessionId": "s1"},
        ]
        transcript.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        self.fire("PostToolUse", tool_name="Bash", transcript_path=str(transcript))
        s = self.status()
        self.assertEqual(s["title"], "Auto")
        self.assertEqual(s["custom_title"], "my-shop-work")

    def test_title_is_kept_when_not_found_later(self):
        transcript = self.dir / "t.jsonl"
        transcript.write_text(json.dumps({"type": "ai-title", "aiTitle": "Keep me"}) + "\n")
        self.fire("UserPromptSubmit", prompt="go", transcript_path=str(transcript))
        self.fire("PreToolUse", tool_name="Bash", tool_input={}, transcript_path=str(self.dir / "missing.jsonl"))
        self.fire("Stop", last_assistant_message="ok")
        self.assertEqual(self.status()["title"], "Keep me")

    def test_tmux_pane_is_recorded(self):
        env = {"TMUX": "/tmp/tmux-1000/default,1234,0", "TMUX_PANE": "%7"}
        with mock.patch.dict(os.environ, env):
            self.fire("SessionStart")
        s = self.status()
        self.assertEqual(s["tmux_pane"], "%7")
        self.assertEqual(s["tmux_socket"], "/tmp/tmux-1000/default")

    def test_no_tmux_fields_outside_tmux(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            os.environ.pop("TMUX_PANE", None)
            self.fire("SessionStart")
        self.assertNotIn("tmux_pane", self.status())

    def test_prompt_records_turn_start(self):
        with mock.patch.object(hook.time, "time", return_value=1000.0):
            self.fire("UserPromptSubmit", prompt="go")
        with mock.patch.object(hook.time, "time", return_value=1500.0):
            self.fire("PreToolUse", tool_name="Bash", tool_input={})
        s = self.status()
        self.assertEqual(s["turn_started_at"], 1000.0)
        self.assertEqual(s["updated_at"], 1500.0)

    def test_subagents_are_counted_by_id(self):
        self.fire("UserPromptSubmit", prompt="go")
        self.fire("SubagentStart", agent_id="a1", agent_type="Explore")
        self.fire("SubagentStart", agent_id="a2", agent_type="Plan")
        self.fire("SubagentStart", agent_id="a2", agent_type="Plan")  # duplicate event
        self.assertEqual(self.status()["subagents"], ["a1", "a2"])
        self.fire("SubagentStop", agent_id="a1")
        self.fire("SubagentStop", agent_id="unknown")
        self.assertEqual(self.status()["subagents"], ["a2"])
        self.assertEqual(self.status()["state"], "working")

    def test_session_start_resets_subagents(self):
        self.fire("SubagentStart", agent_id="a1")
        self.fire("SessionStart", source="resume")
        self.assertEqual(self.status()["subagents"], [])

    def test_session_end_removes_file(self):
        self.fire("SessionStart")
        self.fire("SessionEnd", reason="exit")
        self.assertFalse((self.dir / "s1.json").exists())


class LocationTests(unittest.TestCase):
    def test_status_dir_is_fixed_under_claude_home_not_next_to_code(self):
        self.assertEqual(hook.STATUS_DIR, Path.home() / ".claude" / "agent-monitor" / "sessions")


class RobustnessTests(HookTestCase):
    def run_raw(self, raw, status_dir=None):
        out = io.StringIO()
        code = hook.main(io.StringIO(raw), out, status_dir=status_dir or self.dir)
        return code, out.getvalue()

    def test_empty_or_garbage_input_exits_zero_silently(self):
        for raw in ["", "not json", "[]", '{"hook_event_name": "Stop"}']:
            self.assertEqual(self.run_raw(raw), (0, ""))

    def test_session_id_cannot_escape_directory(self):
        payload = json.dumps({"session_id": "../../evil", "hook_event_name": "SessionStart", "cwd": "/x"})
        self.assertEqual(self.run_raw(payload), (0, ""))
        self.assertEqual(list(Path(self.tmp.name).parent.glob("evil*")), [])
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_unwritable_dir_exits_zero(self):
        blocker = self.dir / "file"
        blocker.write_text("x")
        payload = json.dumps({"session_id": "s1", "hook_event_name": "SessionStart", "cwd": "/x"})
        self.assertEqual(self.run_raw(payload, status_dir=blocker / "sub"), (0, ""))

    def test_writes_are_atomic_no_temp_files_left(self):
        self.fire("SessionStart")
        self.fire("UserPromptSubmit", prompt="x")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["s1.json"])

    def test_sessions_without_pid_are_pruned_when_stale(self):
        old = time.time() - 2 * 86400
        (self.dir / "old.json").write_text(json.dumps({"session_id": "old", "pid": None, "updated_at": old}))
        (self.dir / "fresh.json").write_text(json.dumps({"session_id": "fresh", "pid": None, "updated_at": time.time()}))
        self.fire("SessionStart")
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")), ["fresh.json", "s1.json"])

    def test_dead_sessions_are_pruned(self):
        (self.dir / "dead.json").write_text(json.dumps({"session_id": "dead", "pid": 999999999, "pid_start": "1"}))
        (self.dir / "reused.json").write_text(json.dumps({"session_id": "reused", "pid": os.getpid(), "pid_start": "not-the-real-start"}))
        self.fire("SessionStart")
        names = sorted(p.name for p in self.dir.glob("*.json"))
        self.assertEqual(names, ["s1.json"])


class NoProcFallbackTests(unittest.TestCase):
    """macOS has no /proc: process info comes from `ps` instead."""

    def setUp(self):
        patcher = mock.patch.object(hook, "HAS_PROC", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_ps_splits_parent_start_time_and_command(self):
        line = "  412 Sun Nov  1 09:05:03 2026     node /opt/homebrew/bin/claude --resume\n"
        self.assertEqual(hook.parse_ps(line), (412, "Sun Nov 1 09:05:03 2026", "node /opt/homebrew/bin/claude --resume"))

    def test_parse_ps_rejects_empty_or_garbage(self):
        for text in ["", "\n", "abc def", "12 Sun Nov"]:
            self.assertIsNone(hook.parse_ps(text), text)

    def test_start_time_of_this_process_is_stable(self):
        first = hook.proc_starttime(os.getpid())
        self.assertIsNotNone(first)
        self.assertEqual(hook.proc_starttime(os.getpid()), first)

    def test_start_time_of_missing_process_is_none(self):
        self.assertIsNone(hook.proc_starttime(999999999))

    def test_ps_failure_gives_none_without_raising(self):
        with mock.patch.object(hook.subprocess, "run", side_effect=OSError("no ps")):
            self.assertIsNone(hook.proc_starttime(os.getpid()))

    def test_parent_walk_finds_claude_by_command_line(self):
        chain = {
            300: (200, "Sun Nov 1 09:00:00 2026", "/bin/zsh -c python3 hook.py"),
            200: (100, "Sun Nov 1 08:00:00 2026", "node /opt/homebrew/bin/claude"),
        }
        with mock.patch.object(hook.os, "getppid", return_value=300), \
                mock.patch.object(hook, "_ps_info", side_effect=chain.get):
            self.assertEqual(hook.find_claude_process(), (200, "Sun Nov 1 08:00:00 2026"))

    def test_live_session_kept_and_dead_or_reused_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            me = hook.proc_starttime(os.getpid())
            for name, pid, start in [("live", os.getpid(), me), ("dead", 999999999, "x"), ("reused", os.getpid(), "x")]:
                (d / f"{name}.json").write_text(json.dumps({"session_id": name, "pid": pid, "pid_start": start}))
            hook.prune_dead(d, keep="none.json")
            self.assertEqual(sorted(p.name for p in d.glob("*.json")), ["live.json"])


if __name__ == "__main__":
    unittest.main()
