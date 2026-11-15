import json
import os
import plistlib
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
import install  # noqa: E402

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd",
          "SubagentStart", "SubagentStop"]


def commands(settings, event):
    return [h["command"] for g in settings.get("hooks", {}).get(event, []) for h in g.get("hooks", [])]


class HookSettingsTests(unittest.TestCase):
    OTHER = {"hooks": [{"type": "command", "command": "python3 /opt/other-tool/notify.sh"}]}

    def test_adds_hook_for_every_event_and_keeps_other_hooks(self):
        settings = {"model": "opus", "hooks": {"SessionStart": [self.OTHER]}}
        cmd = install.hook_command(Path("/apps/cam"))
        out = install.update_hooks(settings, cmd)
        self.assertEqual(out["model"], "opus")
        for ev in EVENTS:
            self.assertEqual(commands(out, ev).count(cmd), 1, ev)
        self.assertIn("python3 /opt/other-tool/notify.sh", commands(out, "SessionStart"))

    def test_is_idempotent(self):
        cmd = install.hook_command(Path("/apps/cam"))
        once = install.update_hooks({}, cmd)
        twice = install.update_hooks(json.loads(json.dumps(once)), cmd)
        self.assertEqual(once, twice)

    def test_moving_the_app_replaces_old_hook_paths(self):
        legacy = install.update_hooks({}, "python3 /home/u/.claude/agent-monitor/hook.py")
        moved_from = install.update_hooks(legacy, install.hook_command(Path("/old/cam")))
        new_cmd = install.hook_command(Path("/new place/cam"))
        new = install.update_hooks(moved_from, new_cmd)
        for ev in EVENTS:
            self.assertEqual(commands(new, ev), [new_cmd], ev)

    def test_remove_hooks_leaves_others_and_drops_empty_events(self):
        settings = install.update_hooks({"hooks": {"SessionStart": [self.OTHER]}}, install.hook_command(Path("/a")))
        out = install.remove_hooks(settings)
        self.assertEqual(commands(out, "SessionStart"), ["python3 /opt/other-tool/notify.sh"])
        self.assertNotIn("PreToolUse", out["hooks"])

    def test_hook_command_with_explicit_python_quotes_it(self):
        self.assertEqual(
            install.hook_command(Path("/c/apps/cam"), python="C:/Program Files/Python312/python.exe"),
            '"C:/Program Files/Python312/python.exe" "/c/apps/cam/hook.py" # claude-agent-monitor',
        )

    def test_hook_command_quotes_path(self):
        self.assertEqual(install.hook_command(Path("/my apps/cam")), 'python3 "/my apps/cam/hook.py" # claude-agent-monitor')


class PlatformTests(unittest.TestCase):
    def test_detects_wsl(self):
        self.assertTrue(install.is_wsl("5.15.167.4-microsoft-standard-WSL2"))
        self.assertFalse(install.is_wsl("6.8.0-45-generic"))

    def test_desktop_entry(self):
        text = install.desktop_entry("/usr/bin/python3", Path("/my apps/cam/monitor.pyw"))
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("Name=Claude Agent Monitor", text)
        self.assertIn('Exec="/usr/bin/python3" "/my apps/cam/monitor.pyw"', text)
        self.assertIn("Type=Application", text)

    def test_windows_shortcut_arguments(self):
        args = install.windows_arguments(r"\\wsl.localhost\Ubuntu\home\u\cam\monitor.pyw", r"\\wsl.localhost\Ubuntu\home\u\.claude\agent-monitor\sessions")
        self.assertEqual(args, r'"\\wsl.localhost\Ubuntu\home\u\cam\monitor.pyw" --sessions "\\wsl.localhost\Ubuntu\home\u\.claude\agent-monitor\sessions"')


class LinuxInstallTests(unittest.TestCase):
    """Full install/uninstall on native Linux into a throwaway HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(install, "is_wsl", return_value=False),
            mock.patch.object(install.sys, "platform", "linux"),  # also when the suite runs on macOS CI
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_then_uninstall(self):
        self.assertEqual(install.main(["install"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertIn(install.hook_command(APP), commands(settings, "Stop"))
        menu = self.home / ".local/share/applications/claude-agent-monitor.desktop"
        auto = self.home / ".config/autostart/claude-agent-monitor.desktop"
        self.assertIn(str(APP / "monitor.pyw"), menu.read_text())
        self.assertTrue(auto.exists())
        self.assertTrue((self.home / ".claude/agent-monitor/sessions").is_dir())
        self.assertTrue(list((self.home / ".claude").glob("settings.json.bak-*")))

        self.assertEqual(install.main(["uninstall"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(commands(settings, "Stop"), [])
        self.assertFalse(menu.exists())
        self.assertFalse(auto.exists())

    def test_no_autostart_flag(self):
        install.main(["install", "--no-autostart"])
        self.assertFalse((self.home / ".config/autostart/claude-agent-monitor.desktop").exists())
        self.assertTrue((self.home / ".local/share/applications/claude-agent-monitor.desktop").exists())

    def test_missing_settings_file_is_created(self):
        (self.home / ".claude" / "settings.json").unlink()
        self.assertEqual(install.main(["install"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertIn(install.hook_command(APP), commands(settings, "SessionStart"))

    def test_corrupt_settings_aborts_without_writing(self):
        (self.home / ".claude" / "settings.json").write_text("{not json")
        self.assertNotEqual(install.main(["install"]), 0)
        self.assertEqual((self.home / ".claude" / "settings.json").read_text(), "{not json")


class NativeWindowsInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.scripts = []
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(install, "platform_kind", return_value="windows"),
            mock.patch.object(install, "windows_python", return_value=("C:/Py/python.exe", "C:/Py/pythonw.exe")),
            mock.patch.object(install, "_ps", side_effect=self.fake_ps),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_ps(self, script):
        self.scripts.append(script)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_registers_windows_python_hook_and_local_shortcut(self):
        self.assertEqual(install.main(["install"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        cmd = commands(settings, "Stop")[0]
        self.assertTrue(cmd.startswith('"C:/Py/python.exe" "'), cmd)
        self.assertTrue(cmd.endswith('hook.py" # claude-agent-monitor'), cmd)
        script = self.scripts[-1]
        self.assertIn("C:/Py/pythonw.exe", script)
        self.assertIn("monitor.pyw", script)
        self.assertNotIn("--sessions", script)

    def test_uninstall_removes_shortcuts(self):
        install.main(["install"])
        self.assertEqual(install.main(["uninstall"]), 0)
        self.assertIn("Remove-Item", self.scripts[-1])


class MacHelperTests(unittest.TestCase):
    PY = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    MON = "/Users/me/My \"Tools\"/$cam/monitor.pyw"

    def test_darwin_is_detected_as_macos(self):
        with mock.patch.object(install.sys, "platform", "darwin"):
            self.assertEqual(install.platform_kind(), "macos")

    def test_launch_agent_runs_monitor_at_login(self):
        plist = plistlib.loads(install.launch_agent_plist(self.PY, self.MON))
        self.assertEqual(plist["Label"], install.LAUNCH_LABEL)
        self.assertEqual(plist["ProgramArguments"], [self.PY, self.MON])
        self.assertTrue(plist["RunAtLoad"])

    def test_app_bundle_launcher_quotes_paths(self):
        files = install.app_bundle_files(self.PY, self.MON)
        info = plistlib.loads(files["Contents/Info.plist"])
        self.assertEqual(info["CFBundleExecutable"], "launcher")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        script = files["Contents/MacOS/launcher"]
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        exec_line = [line for line in script.splitlines() if line.startswith("exec ")][0]
        self.assertEqual(shlex.split(exec_line), ["exec", self.PY, self.MON])

    def test_tk_hint(self):
        self.assertIn("python-tk", install.macos_tk_hint(None))
        self.assertIn("8.5", install.macos_tk_hint(8.5))
        self.assertIsNone(install.macos_tk_hint(8.6))
        self.assertIsNone(install.macos_tk_hint(9.0))


class MacInstallTests(unittest.TestCase):
    PY = "/opt/homebrew/bin/python3.12"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.launchctl = []
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(install, "platform_kind", return_value="macos"),
            mock.patch.object(install.sys, "executable", self.PY),
            mock.patch.object(install, "_launchctl", side_effect=self.launchctl.append),
            mock.patch.object(install, "_ps", side_effect=AssertionError("Windows code used on macOS")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = self.home / "Applications" / "Claude Agent Monitor.app"
        self.agent = self.home / "Library" / "LaunchAgents" / f"{install.LAUNCH_LABEL}.plist"

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_registers_hook_with_this_python_and_creates_app_and_login_item(self):
        self.assertEqual(install.main(["install"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(commands(settings, "Stop"), [install.hook_command(APP, python=self.PY)])
        launcher = self.app / "Contents" / "MacOS" / "launcher"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn(str(APP / "monitor.pyw"), launcher.read_text())
        self.assertTrue((self.app / "Contents" / "Info.plist").exists())
        self.assertEqual(plistlib.loads(self.agent.read_bytes())["ProgramArguments"], [self.PY, str(APP / "monitor.pyw")])
        self.assertFalse((self.home / ".local/share/applications").exists())

    def test_no_autostart_removes_login_item(self):
        install.main(["install"])
        install.main(["install", "--no-autostart"])
        self.assertFalse(self.agent.exists())
        self.assertTrue(self.app.exists())

    def test_uninstall_removes_app_and_login_item(self):
        install.main(["install"])
        self.assertEqual(install.main(["uninstall"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(commands(settings, "Stop"), [])
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())
        self.assertTrue(any("bootout" in call for call in self.launchctl))


if __name__ == "__main__":
    unittest.main()
