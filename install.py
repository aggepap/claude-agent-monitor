#!/usr/bin/env python3
"""Install or uninstall Claude Agent Monitor on this machine.

    python3 install.py                 # install (re-run after moving this folder)
    python3 install.py --no-autostart  # install without starting at login
    python3 install.py uninstall

Run it from WSL or native Linux, wherever Claude Code runs. It:
  * registers hook.py (from wherever this folder lives) in ~/.claude/settings.json
  * WSL:   adds Windows Start Menu + Startup shortcuts that open the window
  * Linux: adds an app-menu entry + login autostart entry
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NAME = "claude-agent-monitor"
MARKER = "# claude-agent-monitor"
LEGACY = re.compile(r"/\.claude/agent-monitor/hook\.py\b")
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd"]


# ---------- pure helpers (unit-tested) ----------

def hook_command(app_dir):
    return f'python3 "{Path(app_dir) / "hook.py"}" {MARKER}'


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


def _windows(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)


def _wslpath(path):
    return _windows(["wslpath", "-w", str(path)]).stdout.strip()


def _ps(script):
    return _windows(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def _ps_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def install_windows(autostart):
    out = _windows(["cmd.exe", "/c", "where", "pythonw"]).stdout
    candidates = [l.strip() for l in out.splitlines() if l.strip().lower().endswith("pythonw.exe") and "windowsapps" not in l.lower()]
    if not candidates:
        print("  ! No Windows Python found (pythonw.exe). Install Python for Windows from python.org, then re-run.")
        return False
    args = windows_arguments(_wslpath(APP_DIR / "monitor.pyw"), _wslpath(sessions_dir()))
    folders = '[Environment]::GetFolderPath("Programs")' + (', [Environment]::GetFolderPath("Startup")' if autostart else "")
    script = (
        f"$s = New-Object -ComObject WScript.Shell; foreach ($d in @({folders})) {{"
        f" $l = $s.CreateShortcut((Join-Path $d 'Claude Agent Monitor.lnk'));"
        f" $l.TargetPath = {_ps_str(candidates[0])}; $l.Arguments = {_ps_str(args)};"
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

    wsl = is_wsl()
    if opts.action == "uninstall":
        write_settings(path, remove_hooks(settings))
        uninstall_windows() if wsl else uninstall_linux()
        print(f"Uninstalled. Session data is left in {sessions_dir()} (safe to delete).")
        return 0

    sessions_dir().mkdir(parents=True, exist_ok=True)
    write_settings(path, update_hooks(settings, hook_command(APP_DIR)))
    print(f"Claude Agent Monitor installed from {APP_DIR}")
    print(f"  hooks registered in {path} (new Claude sessions pick them up)")
    ok = install_windows(not opts.no_autostart) if wsl else (install_linux(not opts.no_autostart) or True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
