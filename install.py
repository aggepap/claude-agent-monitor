#!/usr/bin/env python3
"""Install or uninstall Claude Agent Monitor on this machine.

    python3 install.py                 # install (re-run after moving this folder)
    python3 install.py --no-autostart  # install without starting at login
    python3 install.py uninstall

Run it with the Python of the system where Claude Code runs (WSL, Linux,
macOS or Windows). It:
  * registers hook.py (from wherever this folder lives) in ~/.claude/settings.json
  * WSL / Windows: adds Start Menu + Startup shortcuts that open the window
  * Linux:         adds an app-menu entry + login autostart entry
  * macOS:         adds ~/Applications/Claude Agent Monitor.app + a login LaunchAgent
"""
import argparse
import copy
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NAME = "claude-agent-monitor"
MARKER = "# claude-agent-monitor"
LAUNCH_LABEL = "com.github.aggepap.claude-agent-monitor"
APP_NAME = "Claude Agent Monitor"
LEGACY = re.compile(r"/\.claude/agent-monitor/hook\.py\b")
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd",
          "SubagentStart", "SubagentStop"]


# ---------- pure helpers (unit-tested) ----------

def hook_command(app_dir, python="python3"):
    exe = python if python == "python3" else f'"{python}"'
    return f'{exe} "{(Path(app_dir) / "hook.py").as_posix()}" {MARKER}'


def _is_ours(command):
    return MARKER in command or bool(LEGACY.search(command))


def _without_ours(settings):
    out = copy.deepcopy(settings)
    hooks = out.setdefault("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not _is_ours(str(h.get("command", "")))]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    return out


def update_hooks(settings, command):
    out = _without_ours(settings)
    for event in EVENTS:
        out["hooks"].setdefault(event, []).append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    return out


def remove_hooks(settings):
    return _without_ours(settings)


def platform_kind():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "wsl" if is_wsl() else "linux"


def is_wsl(osrelease=None):
    if osrelease is None:
        try:
            osrelease = Path("/proc/sys/kernel/osrelease").read_text()
        except OSError:
            return False
    return "microsoft" in osrelease.lower()


def desktop_entry(python, monitor):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Claude Agent Monitor",
        "Comment=Floating window showing your Claude Code sessions",
        f'Exec="{python}" "{monitor}"',
        "Icon=utilities-system-monitor",
        "Terminal=false",
        "Categories=Development;Utility;",
        "X-GNOME-Autostart-enabled=true",
        "",
    ])


def launch_agent_plist(python, monitor):
    return plistlib.dumps({
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [str(python), str(monitor)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    })


def app_bundle_files(python, monitor):
    """Relative path inside the .app -> file content, for a minimal launcher app."""
    info = plistlib.dumps({
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": LAUNCH_LABEL,
        "CFBundleExecutable": "launcher",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1.0",
        "LSUIElement": True,  # no Dock icon of its own; Python's Tk window still shows
    })
    launcher = f"#!/bin/sh\nexec {shlex.quote(str(python))} {shlex.quote(str(monitor))}\n"
    return {"Contents/Info.plist": info, "Contents/MacOS/launcher": launcher}


def macos_tk_hint(tk_version):
    """Warning for a missing or too-old Tk (Apple's Command Line Tools Python ships 8.5), or None."""
    fix = "Use Python from python.org, or Homebrew:  brew install python-tk"
    if tk_version is None:
        return f"Tk is missing. {fix}"
    if tk_version < 8.6:
        return f"This Python has Tk {tk_version}, which is too old for macOS. {fix}, then re-run install.py with it."
    return None


def windows_arguments(monitor_unc, sessions_unc):
    return f'"{monitor_unc}" --sessions "{sessions_unc}"'


# ---------- side effects ----------

def settings_path():
    return Path.home() / ".claude" / "settings.json"


def sessions_dir():
    return Path.home() / ".claude" / "agent-monitor" / "sessions"


def load_settings(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text())  # raises on corrupt JSON; caller aborts
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def write_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_name(".settings.json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def linux_desktop_files():
    return (Path.home() / ".local/share/applications" / f"{NAME}.desktop",
            Path.home() / ".config/autostart" / f"{NAME}.desktop")


def install_linux(autostart):
    menu, auto = linux_desktop_files()
    entry = desktop_entry(sys.executable or "/usr/bin/python3", APP_DIR / "monitor.pyw")
    targets = [menu] + ([auto] if autostart else [])
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry)
    if not autostart and auto.exists():
        auto.unlink()
    print(f"  app menu entry: {menu}" + (f"\n  autostart:      {auto}" if autostart else ""))
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("  ! Tk is missing. Install it with:  sudo apt install python3-tk")
    if os.environ.get("WAYLAND_DISPLAY"):
        print("  Wayland: clicking a card switches tmux panes. To also raise the terminal window,\n"
              "  install the GNOME extension 'Activate Window By Title' (see README).")
    elif not shutil.which("wmctrl"):
        print("  ! For click-to-focus on X11 install wmctrl:  sudo apt install wmctrl")


def uninstall_linux():
    for path in linux_desktop_files():
        if path.exists():
            path.unlink()


def macos_paths():
    return (Path.home() / "Applications" / f"{APP_NAME}.app",
            Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist")


def _launchctl(args):
    try:
        subprocess.run(["launchctl", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def install_macos(autostart):
    app, agent = macos_paths()
    monitor = APP_DIR / "monitor.pyw"
    if app.exists():
        shutil.rmtree(app)
    for rel, content in app_bundle_files(sys.executable, monitor).items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)
    (app / "Contents/MacOS/launcher").chmod(0o755)
    print(f"  app:       {app}")
    if autostart:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(launch_agent_plist(sys.executable, monitor))
        print(f"  autostart: {agent}")
    elif agent.exists():
        _launchctl(["bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"])
        agent.unlink()
    try:
        import tkinter
        hint = macos_tk_hint(float(tkinter.TkVersion))
    except ImportError:
        hint = macos_tk_hint(None)
    if hint:
        print(f"  ! {hint}")
    print("  Click-to-open needs Accessibility access for Python: you'll be asked on the first click\n"
          "  (System Settings > Privacy & Security > Accessibility). See docs/MACOS.md.")


def uninstall_macos():
    app, agent = macos_paths()
    _launchctl(["bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"])
    if agent.exists():
        agent.unlink()
    if app.exists():
        shutil.rmtree(app)


def _windows(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)


def _wslpath(path):
    return _windows(["wslpath", "-w", str(path)]).stdout.strip()


def _ps(script):
    return _windows(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def _ps_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def windows_python():
    """(python.exe, pythonw.exe) on Windows, as forward-slash paths, or None."""
    if sys.platform == "win32":
        exe = Path(sys.executable)
        return exe.as_posix(), exe.with_name("pythonw.exe").as_posix()
    out = _windows(["cmd.exe", "/c", "where", "pythonw"]).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.lower().endswith("pythonw.exe") and "windowsapps" not in line.lower():
            pythonw = line.replace("\\", "/")
            return pythonw[: -len("pythonw.exe")] + "python.exe", pythonw
    return None


def install_windows(autostart, kind):
    found = windows_python()
    if not found:
        print("  ! No Windows Python found (pythonw.exe). Install Python for Windows from python.org, then re-run.")
        return False
    pythonw = found[1]
    if kind == "wsl":
        args = windows_arguments(_wslpath(APP_DIR / "monitor.pyw"), _wslpath(sessions_dir()))
    else:
        args = f'"{APP_DIR / "monitor.pyw"}"'
    folders = '[Environment]::GetFolderPath("Programs")' + (', [Environment]::GetFolderPath("Startup")' if autostart else "")
    script = (
        f"$s = New-Object -ComObject WScript.Shell; foreach ($d in @({folders})) {{"
        f" $l = $s.CreateShortcut((Join-Path $d 'Claude Agent Monitor.lnk'));"
        f" $l.TargetPath = {_ps_str(pythonw)}; $l.Arguments = {_ps_str(args)};"
        f" $l.WorkingDirectory = $env:USERPROFILE; $l.Description = 'Floating Claude Code agent monitor';"
        f" $l.Save(); $l.FullName }}"
    )
    if not autostart:
        script += "; Remove-Item -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath('Startup')) 'Claude Agent Monitor.lnk')"
    result = _ps(script)
    for line in result.stdout.splitlines():
        print(f"  shortcut: {line.strip()}")
    if result.returncode != 0:
        print(f"  ! Could not create Windows shortcuts: {result.stderr.strip()[:300]}")
        return False
    return True


def uninstall_windows():
    _ps("foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {"
        " Remove-Item -ErrorAction SilentlyContinue (Join-Path $d 'Claude Agent Monitor.lnk') }")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install Claude Agent Monitor for this machine.")
    parser.add_argument("action", nargs="?", default="install", choices=["install", "uninstall"])
    parser.add_argument("--no-autostart", action="store_true", help="don't open the window at login")
    opts = parser.parse_args(argv)

    path = settings_path()
    try:
        settings = load_settings(path)
    except (OSError, ValueError) as exc:
        print(f"Could not read {path}: {exc}\nFix or remove that file, then re-run. Nothing was changed.")
        return 1

    kind = platform_kind()
    if opts.action == "uninstall":
        write_settings(path, remove_hooks(settings))
        {"linux": uninstall_linux, "macos": uninstall_macos}.get(kind, uninstall_windows)()
        print(f"Uninstalled. Session data is left in {sessions_dir()} (safe to delete).")
        return 0

    if kind == "windows":
        found = windows_python()
        if not found:
            print("Could not locate this Python's executable. Nothing was changed.")
            return 1
        command = hook_command(APP_DIR, python=found[0])
    elif kind == "macos":
        command = hook_command(APP_DIR, python=sys.executable)  # bare python3 may be Apple's Xcode stub
    else:
        command = hook_command(APP_DIR)
    sessions_dir().mkdir(parents=True, exist_ok=True)
    write_settings(path, update_hooks(settings, command))
    print(f"Claude Agent Monitor installed from {APP_DIR}")
    print(f"  hooks registered in {path} (new Claude sessions pick them up)")
    autostart = not opts.no_autostart
    if kind in ("linux", "macos"):
        (install_linux if kind == "linux" else install_macos)(autostart)
        return 0
    return 0 if install_windows(autostart, kind) else 1


if __name__ == "__main__":
    sys.exit(main())
