# Running on Ubuntu

How to install Claude Agent Monitor on a native Ubuntu desktop and have it open automatically.

> **Login, not boot.** The monitor is a desktop window, so it can only start once you're logged in to a
> desktop session. It opens every time you log in. If you turn on automatic login
> (**Settings → Users → Automatic Login**), that's effectively every boot.

## 1. Get the code

```bash
cd ~/Projects
git clone https://github.com/aggepap/claude-agent-monitor.git
```

The folder can live anywhere. If you move it later, re-run the installer from the new location (step 3).

## 2. Install what it needs

```bash
sudo apt install python3-tk tmux
```

`python3-tk` is needed for the window; Ubuntu doesn't include it by default. `tmux` is optional but
lets click-to-open switch straight to the right session.

## 3. Run the installer

```bash
cd ~/Projects/claude-agent-monitor
python3 install.py
```

This:

- registers the hooks in `~/.claude/settings.json` (backed up first), so Claude reports each session's
  state. Re-run it if you copied `~/.claude` from another machine; it replaces any old paths
- adds **Claude Agent Monitor** to the app menu
- adds `~/.config/autostart/claude-agent-monitor.desktop`, so the window opens at every login

Use `python3 install.py --no-autostart` if you only want the app-menu entry.

## 4. Start it now, without logging out

```bash
python3 ~/Projects/claude-agent-monitor/monitor.pyw &
```

Or open **Claude Agent Monitor** from the app menu. Then start `claude` in a terminal; its card
appears after your first message.

## 5. Click-to-open on Wayland (Ubuntu's default)

Clicking a card jumps to that session's terminal. On Wayland:

**tmux:** clicking selects the session's window and pane in the terminal already showing it; if no
terminal shows that session, a new terminal window opens attached to it. Add this to `~/.tmux.conf`
(before the `run '~/.tmux/plugins/tpm/tpm'` line, if you use tpm) so the terminal title shows
Claude's session title:

```
set -g set-titles on
set -g set-titles-string '#T'
```

Reload with `tmux source ~/.tmux.conf`.

**Raising the terminal window:** GNOME on Wayland doesn't let apps bring other windows to the
front. Install the GNOME extension that allows it:

```bash
sudo apt install gnome-shell-extension-manager
```

Open **Extension Manager**, search **Activate Window By Title** and install it. Without it, a click
only switches the tmux pane.

**On X11** ("Ubuntu on Xorg" at the login screen), install `wmctrl` instead:

```bash
sudo apt install wmctrl
```

## Turning it off

| Goal | Command |
|---|---|
| Stop it opening at login | `python3 install.py --no-autostart` |
| Remove everything (hooks, menu entry, autostart) | `python3 install.py uninstall` |

Session data stays in `~/.claude/agent-monitor/sessions/` after uninstalling. It's safe to delete.

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named 'tkinter'` | `sudo apt install python3-tk` |
| Window opens but shows no sessions | Re-run `python3 install.py`, then send a message in a Claude session |
| Clicking a card does nothing useful | Check `~/.cache/claude-agent-monitor.log`; on Wayland install the extension above |
| Window doesn't appear at all | Run `python3 monitor.pyw` in a terminal to see the error |
