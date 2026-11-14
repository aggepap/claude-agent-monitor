import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("monitor", str(ROOT / "monitor.pyw"))
spec = importlib.util.spec_from_loader("monitor", loader)
monitor = importlib.util.module_from_spec(spec)
loader.exec_module(monitor)


class LoadSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, sid, **fields):
        (self.dir / f"{sid}.json").write_text(json.dumps({"session_id": sid, **fields}))

    def test_missing_directory_returns_empty_list(self):
        self.assertEqual(monitor.load_sessions(self.dir / "nope"), [])

    def test_corrupt_and_non_json_files_are_skipped(self):
        self.write("ok", state="working", updated_at=1)
        (self.dir / "bad.json").write_text("{half")
        (self.dir / "list.json").write_text("[1, 2]")
        (self.dir / "x.tmp").write_text("{}")
        self.assertEqual([s["session_id"] for s in monitor.load_sessions(self.dir)], ["ok"])

    def test_sorted_waiting_first_then_working_done_idle_newest_first(self):
        self.write("d", state="done", updated_at=50)
        self.write("w_old", state="working", updated_at=10)
        self.write("q", state="waiting", updated_at=1)
        self.write("w_new", state="working", updated_at=20)
        self.write("i", state="idle", updated_at=99)
        self.assertEqual(
            [s["session_id"] for s in monitor.load_sessions(self.dir)],
            ["q", "w_new", "w_old", "d", "i"],
        )


class FormatTests(unittest.TestCase):
    def test_age_formatting(self):
        now = 10_000
        self.assertEqual(monitor.format_age(now - 5, now), "5s")
        self.assertEqual(monitor.format_age(now - 125, now), "2m")
        self.assertEqual(monitor.format_age(now - 7300, now), "2h")
        self.assertEqual(monitor.format_age(None, now), "")

    def test_detail_line_prefers_message_when_waiting(self):
        self.assertEqual(monitor.detail_line({"state": "waiting", "message": "Needs permission", "activity": "Bash: x"}), "Needs permission")
        self.assertEqual(monitor.detail_line({"state": "working", "activity": "Bash: x"}), "Bash: x")
        self.assertEqual(monitor.detail_line({"state": "done", "summary": "All good"}), "All good")
        self.assertEqual(monitor.detail_line({"state": "idle"}), "")

    def test_counts_summary(self):
        sessions = [{"state": "waiting"}, {"state": "working"}, {"state": "working"}, {"state": "done"}]
        self.assertEqual(monitor.counts_summary(sessions), "1 waiting · 2 working · 1 done")
        self.assertEqual(monitor.counts_summary([]), "no sessions")


class ChooseTargetTests(unittest.TestCase):
    ENTRIES = [
        {"k": 0, "kind": "tab", "hwnd": 1, "name": "✳ Fix checkout bug"},
        {"k": 1, "kind": "tab", "hwnd": 1, "name": "⠂ Refactor shop models"},
        {"k": 2, "kind": "tab", "hwnd": 2, "name": "user@host: ~/Projects/blog"},
        {"k": 3, "kind": "window", "hwnd": 9, "name": "app.php - crm [WSL: Ubuntu-22.04] - Visual Studio Code"},
        {"k": 4, "kind": "tab", "hwnd": 2, "name": ""},
    ]

    def pick(self, **session):
        target = monitor.choose_target(session, self.ENTRIES)
        return target and target["k"]

    def test_matches_tab_by_ai_title(self):
        self.assertEqual(self.pick(project="shop", title="Refactor shop models"), 1)

    def test_custom_title_wins_over_ai_title(self):
        self.assertEqual(self.pick(project="x", title="Refactor shop models", custom_title="Fix checkout bug"), 0)

    def test_falls_back_to_project_name_in_tab(self):
        self.assertEqual(self.pick(project="blog", title="Some title not shown yet"), 2)

    def test_falls_back_to_vscode_window_for_project(self):
        self.assertEqual(self.pick(project="crm"), 3)

    def test_no_match_returns_none(self):
        self.assertIsNone(self.pick(project="nothing-here", title="Nope"))
        self.assertIsNone(self.pick())

    def test_title_match_is_case_insensitive(self):
        self.assertEqual(self.pick(title="fix CHECKOUT bug"), 0)


class TmuxTests(unittest.TestCase):
    def test_distro_from_unc_path(self):
        self.assertEqual(monitor.distro_from_path(r"\\wsl.localhost\Ubuntu-22.04\home\u\x"), "Ubuntu-22.04")
        self.assertEqual(monitor.distro_from_path(r"\\wsl$\Debian\home"), "Debian")
        self.assertIsNone(monitor.distro_from_path("/home/u/.claude/agent-monitor/sessions"))

    def test_tmux_command_selects_pane(self):
        argv = monitor.tmux_command({"tmux_pane": "%7", "tmux_socket": "/tmp/tmux-1000/default"}, "Ubuntu-22.04")
        self.assertEqual(argv[:5], ["wsl.exe", "-d", "Ubuntu-22.04", "-e", "tmux"])
        self.assertIn("/tmp/tmux-1000/default", argv)
        for cmd in ("switch-client", "select-window", "select-pane"):
            self.assertIn(cmd, argv)
        self.assertEqual(argv.count("%7"), 3)

    def test_tmux_command_none_without_pane_or_bad_values(self):
        self.assertIsNone(monitor.tmux_command({}, "Ubuntu"))
        self.assertIsNone(monitor.tmux_command({"tmux_pane": "%7"}, None))
        self.assertIsNone(monitor.tmux_command({"tmux_pane": "%7; rm -rf ~", "tmux_socket": "/tmp/x"}, "Ubuntu"))
        self.assertIsNone(monitor.tmux_command({"tmux_pane": "%7", "tmux_socket": "/tmp/x"}, "Ubuntu & calc"))


class SystemExeTests(unittest.TestCase):
    def test_system_exe_uses_absolute_system32_path(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"SystemRoot": r"C:\WINDOWS"}):
            self.assertEqual(monitor.system_exe("wsl.exe"), r"C:\WINDOWS\System32\wsl.exe")
            self.assertEqual(monitor.system_exe("powershell.exe"), r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe")


class LinuxTests(unittest.TestCase):
    def test_sessions_dir_from_argument_or_default(self):
        self.assertEqual(monitor.resolve_sessions_dir(["--sessions", "/x/y"], Path("/home/u")), Path("/x/y"))
        self.assertEqual(monitor.resolve_sessions_dir([], Path("/home/u")), Path("/home/u/.claude/agent-monitor/sessions"))
        self.assertEqual(monitor.resolve_sessions_dir(["--sessions"], Path("/home/u")), Path("/home/u/.claude/agent-monitor/sessions"))

    def test_tmux_args_runs_tmux_directly(self):
        argv = monitor.tmux_args({"tmux_pane": "%3", "tmux_socket": "/tmp/tmux-1000/default"})
        self.assertEqual(argv[:3], ["tmux", "-S", "/tmp/tmux-1000/default"])
        self.assertEqual(argv.count("%3"), 3)
        self.assertIsNone(monitor.tmux_args({"tmux_pane": "%3;x", "tmux_socket": "/tmp/s"}))

    def test_parse_wmctrl(self):
        text = "0x03a00007  0 host ✳ Fix checkout bug\n0x04000003 -1 host Desktop\ngarbage line\n"
        self.assertEqual(monitor.parse_wmctrl(text), [
            {"k": 0, "kind": "tab", "hwnd": 0x03a00007, "name": "✳ Fix checkout bug"},
            {"k": 1, "kind": "tab", "hwnd": 0x04000003, "name": "Desktop"},
        ])

    def test_gnome_activate_command(self):
        argv = monitor.gnome_activate_command("Fix checkout bug")
        self.assertEqual(argv[0], "gdbus")
        self.assertIn("de.lucaswerkmeister.ActivateWindowByTitle.activateBySubstring", argv)
        self.assertEqual(argv[-1], "Fix checkout bug")

    def test_title_needles_order(self):
        self.assertEqual(monitor.title_needles({"custom_title": "c", "title": "t", "project": "shop"}), ["c", "t", "shop"])
        self.assertEqual(monitor.title_needles({"project": "ab"}), [])


class ParseEntriesTests(unittest.TestCase):
    def test_parses_powershell_json(self):
        raw = '[{"k":0,"kind":"tab","hwnd":5,"name":"✳ Hi"}]'
        self.assertEqual(monitor.parse_entries(raw), [{"k": 0, "kind": "tab", "hwnd": 5, "name": "✳ Hi"}])

    def test_single_object_and_garbage(self):
        self.assertEqual(monitor.parse_entries('{"k":0,"kind":"tab","hwnd":5,"name":"a"}'), [{"k": 0, "kind": "tab", "hwnd": 5, "name": "a"}])
        self.assertEqual(monitor.parse_entries(""), [])
        self.assertEqual(monitor.parse_entries("Exception: boom"), [])
        self.assertEqual(monitor.parse_entries('[1, "x", {"k": "bad"}]'), [])


if __name__ == "__main__":
    unittest.main()
