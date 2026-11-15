# Running on macOS

How to install Claude Agent Monitor on a Mac and have it open automatically at login.

## 1. Get a Python with a working Tk

The window needs Tk 8.6 or newer. Apple's own `/usr/bin/python3` (from the Xcode Command Line Tools)
ships Tk 8.5, which doesn't work properly on current macOS. Use one of these:

- **python.org installer** (includes Tk): <https://www.python.org/downloads/macos/>
- **Homebrew**: `brew install python python-tk`

Check it:

```bash
python3 -c "import tkinter; print(tkinter.TkVersion)"   # should print 8.6 or 9.0
```

## 2. Install

From the folder you put the monitor in, run the installer **with that Python**:

```bash
python3 install.py                 # hooks + app + open at login
python3 install.py --no-autostart  # same, but don't open at login
python3 install.py uninstall
```

The hook is registered with the full path of the Python that ran the installer, so Claude Code
doesn't hit Apple's `python3` stub. The installer creates:

| What | Where |
|---|---|
| App to start the window (Spotlight / Launchpad) | `~/Applications/Claude Agent Monitor.app` |
| Login item | `~/Library/LaunchAgents/com.github.aggepap.claude-agent-monitor.plist` |

**Moved the folder or switched Python?** Run `install.py` again. It replaces the old paths.

## 3. Permissions

- **Accessibility** (for click-to-open): the first time you click a card, macOS asks to let Python
  control your computer. Allow it under **System Settings → Privacy & Security → Accessibility**. Without
  it, a click only switches the tmux pane, and the header says *allow Accessibility for Python*.
- **Notifications**: desktop notifications are sent through `osascript` and show up as **Script Editor**.
  If **Send a test notification** in Settings shows nothing, allow notifications for Script Editor
  under **System Settings → Notifications**. They have no **Open** button on macOS; click the card instead.

## Using it

- **Right-click** (or **Ctrl-click**) the header for the menu and **Settings…**, and a card to dismiss it.
- **Click a card** to bring its terminal window (Terminal, iTerm2, Ghostty, VS Code, …) to the front.
  The window is found by its title, like on Linux. In Terminal and iTerm2 only the selected tab's title
  is visible, so a session in a background tab may not be found. Use one window per session, or tmux.

## tmux

Install it with `brew install tmux`. Clicking a card switches tmux to that session's pane. The monitor
adds `/opt/homebrew/bin` and `/usr/local/bin` to its PATH so it finds Homebrew's tmux even when started at
login. Add the title settings from the main README to `~/.tmux.conf` so the terminal title shows Claude's
session title.
