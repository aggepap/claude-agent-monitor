# Claude Agent Monitor

A small floating, always-on-top window that lists every running Claude Code session and what it's doing:

| Card | Meaning |
|---|---|
| 🟠 **NEEDS YOU** | It asked a question or wants a permission (the window flashes and beeps) |
| 🔵 **WORKING** | Shows the current step, e.g. `Bash: Run tests`, `Edit: models.py` |
| 🟢 **DONE** | Finished; shows the start of its last reply |

Click a card to jump to that session's terminal. Drag the header to move the window, double-click it to collapse, right-click for opacity / quit.

## Install

The folder is self-contained. Put it anywhere and run the installer from the machine where Claude Code runs:

```bash
python3 install.py                 # sets up hooks + launchers
python3 install.py --no-autostart  # same, but don't open at login
python3 install.py uninstall
```

**Moved the folder?** Run `python3 install.py` again from the new place. It replaces the old paths.

Claude sessions that were already running pick up the hooks automatically; the window shows a session from its next event.

### WSL (window runs on Windows)

Needs Python for Windows (python.org, includes Tk). The installer adds **Claude Agent Monitor** to the Start Menu and the Startup folder.

### Native Ubuntu

```bash
sudo apt install python3-tk tmux   # Tk for the window
python3 install.py
```

It adds **Claude Agent Monitor** to the app menu and to login autostart.

Click-to-open on Ubuntu:

- **tmux**: clicking always switches tmux to that session's window and pane.
- **X11** ("Ubuntu on Xorg" login): `sudo apt install wmctrl` and the terminal window is raised too.
- **Wayland** (Ubuntu's default): GNOME doesn't let apps raise other windows. Install the GNOME
  extension **Activate Window By Title** (extensions.gnome.org, by Lucas Werkmeister) and the
  terminal window is raised too. Without it, clicking only switches the tmux pane.

### tmux

Add this to `~/.tmux.conf` (before the `run tpm` line) so the terminal title shows Claude's
session title. Click-to-open uses that title to find the right terminal window:

```
set -g set-titles on
set -g set-titles-string '#T'
```

## How it works

- `hook.py`: a Claude Code hook (stdlib Python, ~10 ms) that writes one JSON file per session to
  `~/.claude/agent-monitor/sessions/`. It never prints and always exits 0, so it can't disturb Claude.
  Files of sessions whose process died are cleaned up on the next session start/stop.
- `monitor.pyw`: the Tk window. It reads that folder every second.
- `install.py`: registers the hook in `~/.claude/settings.json` (backing it up first) and creates the launchers.

Click errors are logged to `%TEMP%\claude-agent-monitor.log` (Windows) or `~/.cache/claude-agent-monitor.log` (Linux).

## Tests

```bash
python3 -m unittest discover -s tests
```
