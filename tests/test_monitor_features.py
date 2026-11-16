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


class CardViewTests(unittest.TestCase):
    def test_waiting_card_is_highlighted_with_state_label(self):
        v = monitor.card_view({"state": "waiting", "project": "api", "updated_at": NOW}, NOW, 5)
        self.assertEqual(v["bg"], monitor.CARD_HOT)
        self.assertEqual((v["label"], v["accent"]), monitor.STATE_STYLE["waiting"])
        self.assertEqual(v["fg"], monitor.FG)

    def test_quiet_working_card_is_muted(self):
        v = monitor.card_view({"state": "working", "updated_at": NOW - 600}, NOW, 5)
        self.assertEqual((v["label"], v["accent"], v["fg"]), ("QUIET?", monitor.MUTED, monitor.MUTED))
        self.assertEqual(v["bg"], monitor.CARD)

    def test_name_includes_title_and_is_truncated(self):
        self.assertEqual(monitor.card_view({"project": "api", "title": "fix"}, NOW, 5)["name"], "api · fix")
        self.assertEqual(monitor.card_view({"project": "api", "custom_title": "mine", "title": "x"}, NOW, 5)["name"], "api · mine")
        long = monitor.card_view({"project": "p" * 60}, NOW, 5)["name"]
        self.assertEqual(long, "p" * 44 + "…")
        self.assertEqual(monitor.card_view({}, NOW, 5)["name"], "?")

    def test_optional_lines_are_empty_when_absent(self):
        v = monitor.card_view({"state": "done"}, NOW, 5)
        self.assertEqual((v["agents"], v["task"]), ("", ""))
        v = monitor.card_view({"state": "working", "task": "tests", "subagents": ["a"], "updated_at": NOW}, NOW, 5)
        self.assertEqual((v["agents"], v["task"]), ("+1 agent", "▸ tests"))


class MacTests(unittest.TestCase):
    HOSTILE = 'x" & (do shell script "touch /tmp/pwned") & "'

    def scripts(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]

    def test_notification_passes_text_as_arguments_not_script(self):
        argv, env = monitor.notify_command("darwin", self.HOSTILE, "body " + self.HOSTILE)
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], [self.HOSTILE, "body " + self.HOSTILE])
        self.assertEqual(env, {})
        self.assertTrue(all("pwned" not in script for script in self.scripts(argv)))
        self.assertIn("on run argv", self.scripts(argv))

    def test_parse_window_listing(self):
        text = "Terminal\t✳ shop · Fix login\niTerm2\tother — zsh\n\nno-tab-line\nCode\t\nFinder\tmissing value\n"
        entries = monitor.parse_macos_windows(text)
        self.assertEqual([(e["proc"], e["name"]) for e in entries],
                         [("Terminal", "✳ shop · Fix login"), ("iTerm2", "other — zsh")])
        self.assertEqual([e["k"] for e in entries], [0, 1])
        self.assertEqual(monitor.parse_entries(json.dumps(entries)), entries)  # same shape as other platforms
        target = monitor.choose_target({"title": "Fix login", "project": "shop"}, entries)
        self.assertEqual(target["proc"], "Terminal")

    def test_raise_command_passes_names_as_arguments(self):
        argv = monitor.macos_raise_command("Terminal", self.HOSTILE)
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], ["Terminal", self.HOSTILE])
        self.assertTrue(all("pwned" not in script for script in self.scripts(argv)))

    def test_missing_accessibility_permission_gets_a_readable_hint(self):
        for err in ["execution error: System Events got an error: osascript is not allowed assistive access. (-1719)",
                    "execution error: Not authorized to send Apple events to System Events. (-1743)",
                    "(-25211)"]:
            self.assertIn("Accessibility", monitor.macos_focus_error(err), err)
        self.assertEqual(monitor.macos_focus_error("something else"), "could not list windows")

    def test_right_click_events(self):
        self.assertEqual(monitor.right_click_events("darwin"), ("<Button-2>", "<Control-Button-1>"))
        self.assertEqual(monitor.right_click_events("linux"), ("<Button-3>",))
        self.assertEqual(monitor.right_click_events("win32"), ("<Button-3>",))

    def test_fonts_per_platform(self):
        mac, linux, win = (monitor.ui_fonts(p) for p in ("darwin", "linux", "win32"))
        self.assertEqual(mac["font"][0], "Helvetica Neue")
        self.assertGreater(mac["font"][1], linux["font"][1])
        self.assertEqual(linux["font"], ("Ubuntu", 9))
        self.assertEqual(win["bold"], ("Segoe UI Semibold", 9))
        self.assertEqual(set(mac), {"font", "bold", "small", "tiny_bold"})

    def test_extend_path_adds_homebrew_dirs_once(self):
        once = monitor.extend_path("/usr/bin:/bin")
        self.assertEqual(once.split(":")[:2], ["/usr/bin", "/bin"])
        self.assertIn("/opt/homebrew/bin", once.split(":"))
        self.assertIn("/usr/local/bin", once.split(":"))
        self.assertEqual(monitor.extend_path(once), once)
        self.assertEqual(monitor.extend_path(""), "/opt/homebrew/bin:/usr/local/bin")


if __name__ == "__main__":
    unittest.main()


XRANDR = """Monitors: 2
 0: +*DP-2 3440/800x1440/330+1440+715  DP-2
 1: +HDMI-4 1440/540x2560/300+0+0  HDMI-4
"""


class OnScreenTests(unittest.TestCase):
    MONITORS = [(1440, 715, 3440, 1440), (0, 0, 1440, 2560)]  # primary first

    def test_parse_xrandr_puts_primary_first(self):
        self.assertEqual(monitor.parse_xrandr_monitors(XRANDR), self.MONITORS)

    def test_parse_xrandr_garbage_gives_empty_list(self):
        self.assertEqual(monitor.parse_xrandr_monitors("not xrandr output"), [])

    def test_visible_position_is_kept(self):
        self.assertEqual(monitor.on_screen_position(2000, 800, self.MONITORS), (2000, 800))
        self.assertEqual(monitor.on_screen_position(100, 2000, self.MONITORS), (100, 2000))

    def test_position_in_gap_between_monitors_moves_to_primary(self):
        # the real bug: saved at (3033, 68), above DP-2 and right of HDMI-4
        self.assertEqual(monitor.on_screen_position(3033, 68, self.MONITORS), (1480, 755))

    def test_position_hanging_off_the_edge_moves_to_primary(self):
        self.assertEqual(monitor.on_screen_position(4870, 800, self.MONITORS), (1480, 755))
        self.assertEqual(monitor.on_screen_position(-500, -500, self.MONITORS), (1480, 755))

    def test_no_monitor_info_keeps_position(self):
        self.assertEqual(monitor.on_screen_position(3033, 68, []), (3033, 68))
