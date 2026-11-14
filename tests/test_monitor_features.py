import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("monitor_f", str(ROOT / "monitor.pyw"))
spec = importlib.util.spec_from_loader("monitor_f", loader)
monitor = importlib.util.module_from_spec(spec)
loader.exec_module(monitor)

NOW = 100_000.0


class PrefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "prefs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_defaults_with_notifications_off(self):
        prefs = monitor.load_prefs(self.path)
        self.assertEqual(prefs, monitor.DEFAULT_PREFS)
        self.assertFalse(prefs["notify_waiting"])
        self.assertFalse(prefs["notify_done"])
        self.assertTrue(prefs["sound"])
        self.assertTrue(prefs["flash"])
        self.assertFalse(prefs["hide_done"])
        self.assertEqual(prefs["quiet_minutes"], 10)

    def test_saved_values_are_kept_and_bad_ones_replaced(self):
        self.path.write_text(json.dumps({
            "notify_waiting": True, "sound": "loud", "quiet_minutes": 0, "alpha": 5, "x": 300, "unknown": 1,
        }))
        prefs = monitor.load_prefs(self.path)
        self.assertTrue(prefs["notify_waiting"])
        self.assertTrue(prefs["sound"])            # wrong type -> default
        self.assertEqual(prefs["quiet_minutes"], 1)  # clamped
        self.assertEqual(prefs["alpha"], 1.0)        # clamped
        self.assertEqual(prefs["x"], 300)
        self.assertNotIn("unknown", prefs)

    def test_corrupt_file_gives_defaults(self):
        self.path.write_text("{oops")
        self.assertEqual(monitor.load_prefs(self.path), monitor.DEFAULT_PREFS)

    def test_save_round_trip(self):
        prefs = dict(monitor.DEFAULT_PREFS, notify_done=True, quiet_minutes=25)
        monitor.save_prefs(self.path, prefs)
        self.assertEqual(monitor.load_prefs(self.path), prefs)


class HeaderStateTests(unittest.TestCase):
    def test_most_urgent_state_wins(self):
        self.assertEqual(monitor.top_state([{"state": "done"}, {"state": "waiting"}, {"state": "working"}]), "waiting")
        self.assertEqual(monitor.top_state([{"state": "done"}, {"state": "working"}]), "working")
        self.assertEqual(monitor.top_state([{"state": "idle"}, {"state": "done"}]), "done")
        self.assertIsNone(monitor.top_state([]))


class TimeAndQuietTests(unittest.TestCase):
    def test_working_shows_time_since_prompt(self):
        s = {"state": "working", "turn_started_at": NOW - 720, "updated_at": NOW - 3}
        self.assertEqual(monitor.card_time(s, NOW), "12m")

    def test_waiting_shows_time_since_prompt_falls_back_to_update(self):
        self.assertEqual(monitor.card_time({"state": "waiting", "updated_at": NOW - 30}, NOW), "30s")

    def test_done_shows_time_ago(self):
        s = {"state": "done", "turn_started_at": NOW - 3600, "updated_at": NOW - 180}
        self.assertEqual(monitor.card_time(s, NOW), "3m ago")

    def test_quiet_only_for_working_sessions_past_threshold(self):
        self.assertTrue(monitor.is_quiet({"state": "working", "updated_at": NOW - 601}, NOW, 10))
        self.assertFalse(monitor.is_quiet({"state": "working", "updated_at": NOW - 599}, NOW, 10))
        self.assertFalse(monitor.is_quiet({"state": "done", "updated_at": NOW - 9999}, NOW, 10))
        self.assertFalse(monitor.is_quiet({"state": "working"}, NOW, 10))


class VisibilityTests(unittest.TestCase):
    SESSIONS = [
        {"session_id": "a", "state": "working", "updated_at": 5},
        {"session_id": "b", "state": "done", "updated_at": 6},
        {"session_id": "c", "state": "waiting", "updated_at": 7},
    ]

    def ids(self, prefs, dismissed):
        return [s["session_id"] for s in monitor.visible_sessions(self.SESSIONS, prefs, dismissed)]

    def test_hide_done(self):
        self.assertEqual(self.ids({"hide_done": True}, {}), ["a", "c"])
        self.assertEqual(self.ids({"hide_done": False}, {}), ["a", "b", "c"])

    def test_dismissed_until_session_changes(self):
        self.assertEqual(self.ids({}, {"b": 6}), ["a", "c"])
        self.assertEqual(self.ids({}, {"b": 5}), ["a", "b", "c"])  # updated since dismissal


class TransitionTests(unittest.TestCase):
    def test_reports_new_waiting_and_new_done(self):
        prev = {"a": "working", "b": "working", "c": "waiting"}
        sessions = [{"session_id": "a", "state": "waiting"}, {"session_id": "b", "state": "done"}, {"session_id": "c", "state": "waiting"}]
        waiting, done = monitor.transitions(prev, sessions)
        self.assertEqual([s["session_id"] for s in waiting], ["a"])
        self.assertEqual([s["session_id"] for s in done], ["b"])

    def test_nothing_on_first_load(self):
        self.assertEqual(monitor.transitions(None, [{"session_id": "a", "state": "waiting"}]), ([], []))

    def test_new_session_already_waiting_counts(self):
        waiting, _ = monitor.transitions({"x": "done"}, [{"session_id": "n", "state": "waiting"}])
        self.assertEqual([s["session_id"] for s in waiting], ["n"])


class NotificationTests(unittest.TestCase):
    def test_text_for_waiting_and_done(self):
        title, body = monitor.notification_text({"state": "waiting", "project": "shop", "title": "Fix bug", "message": "Needs permission to use Bash"})
        self.assertEqual(title, "shop needs you")
        self.assertIn("Needs permission", body)
        title, body = monitor.notification_text({"state": "done", "project": "shop", "summary": "All tests pass."})
        self.assertEqual(title, "shop finished")
        self.assertEqual(body, "All tests pass.")

    def test_linux_command_passes_text_as_plain_arguments(self):
        argv, env = monitor.notify_command("linux", "shop needs you", "$(rm -rf ~) `x`")
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(argv[-2:], ["shop needs you", "$(rm -rf ~) `x`"])
        self.assertIn("open=Open", argv)
        self.assertEqual(env, {})

    def test_windows_command_passes_text_through_environment(self):
        argv, env = monitor.notify_command("win32", "shop needs you", "it's \"quoted\"; & stuff")
        self.assertTrue(argv[0].lower().endswith("powershell.exe"))
        script = " ".join(argv)
        self.assertNotIn("quoted", script)
        self.assertNotIn("shop needs you", script)
        self.assertEqual(env, {"CAM_TITLE": "shop needs you", "CAM_BODY": "it's \"quoted\"; & stuff"})


class SubagentLabelTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(monitor.subagent_label({"subagents": ["a", "b"]}), "+2 agents")
        self.assertEqual(monitor.subagent_label({"subagents": ["a"]}), "+1 agent")
        self.assertEqual(monitor.subagent_label({"subagents": []}), "")
        self.assertEqual(monitor.subagent_label({}), "")


if __name__ == "__main__":
    unittest.main()
