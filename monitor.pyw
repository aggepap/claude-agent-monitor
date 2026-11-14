"""Floating always-on-top window listing Claude Code sessions.

Linux:    python3 monitor.pyw
Windows:  pythonw.exe monitor.pyw --sessions \\\\wsl.localhost\\<distro>\\home\\<you>\\.claude\\agent-monitor\\sessions
(install.py creates these launchers for you.)

Drag the header to move · double-click it to collapse · right-click for menu.
Click a session card to jump to its terminal (Windows Terminal, tmux, VS Code,
X11 windows via wmctrl, GNOME Wayland via the "Activate Window By Title" extension).
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def resolve_sessions_dir(argv, home):
    if "--sessions" in argv:
        i = argv.index("--sessions")
        if i + 1 < len(argv):
            return Path(argv[i + 1])
    return Path(home) / ".claude" / "agent-monitor" / "sessions"


SESSIONS_DIR = resolve_sessions_dir(sys.argv[1:], Path.home())
PREFS = Path.home() / ".claude-agent-monitor.json"
STATE_ORDER = {"waiting": 0, "working": 1, "done": 2, "idle": 3}
STATE_STYLE = {  # label, accent colour
    "waiting": ("NEEDS YOU", "#f5a524"),
    "working": ("WORKING", "#4c9aff"),
    "done": ("DONE", "#3ecf8e"),
    "idle": ("IDLE", "#7a7f8c"),
}
BG, CARD, CARD_HOT, FG, MUTED, HEADER = "#16181d", "#1f2229", "#2c2618", "#e6e8ee", "#8b90a0", "#101216"


# ---------- model (pure, unit-tested) ----------

def load_sessions(directory=None):
    sessions = []
    try:
        paths = list(Path(directory or SESSIONS_DIR).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id"):
            sessions.append(data)
    sessions.sort(key=lambda s: (STATE_ORDER.get(s.get("state"), 9), -float(s.get("updated_at") or 0)))
    return sessions


def format_age(ts, now=None):
    if not ts:
        return ""
    secs = max(0, int((now or time.time()) - float(ts)))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def detail_line(s):
    state = s.get("state")
    if state == "waiting":
        return s.get("message") or "Waiting for input"
    if state == "working":
        return s.get("activity") or "Thinking…"
    if state == "done":
        return s.get("summary") or ""
    return ""


def parse_entries(raw):
    """PowerShell's list of terminal tabs / top-level windows -> validated dicts."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    items = data if isinstance(data, list) else [data]
    return [
        e for e in items
        if isinstance(e, dict)
        and isinstance(e.get("k"), int) and isinstance(e.get("hwnd"), int)
        and e.get("kind") in ("tab", "window") and isinstance(e.get("name"), str)
    ]


def choose_target(session, entries):
    """Pick the terminal tab (or VS Code window) that hosts this session."""
    tabs = [e for e in entries if e["kind"] == "tab" and e["name"]]
    for needle in (session.get("custom_title"), session.get("title")):
        if needle:
            hits = [e for e in tabs if needle.lower() in e["name"].lower()]
            if hits:
                return min(hits, key=lambda e: len(e["name"]))
    project = (session.get("project") or "").lower()
    if len(project) >= 3:
        hits = [e for e in tabs if project in e["name"].lower()]
        if hits:
            return min(hits, key=lambda e: len(e["name"]))
        for e in entries:
            name = e["name"].lower()
            if e["kind"] == "window" and "visual studio code" in name and project in name:
                return e
    return None


def distro_from_path(path):
    m = re.match(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)", str(path))
    return m.group(1) if m else None


def system_exe(name):
    """Absolute path: GUI apps started from a shortcut may lack System32 subdirs on PATH."""
    root = os.environ.get("SystemRoot", r"C:\Windows") + "\\System32\\"
    return root + ("WindowsPowerShell\\v1.0\\" if name == "powershell.exe" else "") + name


def tmux_args(session):
    pane, sock = session.get("tmux_pane") or "", session.get("tmux_socket") or ""
    if not (re.fullmatch(r"%\d+", pane) and re.fullmatch(r"/[\w./-]+", sock)):
        return None
    return ["tmux", "-S", sock, "select-window", "-t", pane, ";", "select-pane", "-t", pane, ";", "switch-client", "-t", pane]


def tmux_command(session, distro):
    """Same tmux call, run inside a WSL distro from Windows."""
    args = tmux_args(session)
    if not (args and distro and re.fullmatch(r"[\w.-]+", distro)):
        return None
    return ["wsl.exe", "-d", distro, "-e", *args]


def title_needles(session):
    """Strings a terminal window title may contain for this session, best first."""
    needles = [session.get("custom_title"), session.get("title")]
    project = session.get("project") or ""
    if len(project) >= 3:
        needles.append(project)
    return [n for n in needles if n]


def parse_wmctrl(text):
    entries = []
    for line in (text or "").splitlines():
        m = re.match(r"^(0x[0-9a-fA-F]+)\s+-?\d+\s+\S+\s?(.*)$", line)
        if m:
            entries.append({"k": len(entries), "kind": "tab", "hwnd": int(m.group(1), 16), "name": m.group(2)})
    return entries


def gnome_activate_command(needle):
    """D-Bus call provided by the GNOME extension "Activate Window By Title"."""
    return ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
            "--object-path", "/de/lucaswerkmeister/ActivateWindowByTitle",
            "--method", "de.lucaswerkmeister.ActivateWindowByTitle.activateBySubstring", needle]


def counts_summary(sessions):
    if not sessions:
        return "no sessions"
    parts = []
    for state in ("waiting", "working", "done", "idle"):
        n = sum(1 for s in sessions if s.get("state") == state)
        if n:
            parts.append(f"{n} {state}")
    return " · ".join(parts)


# ---------- focusing a session's terminal ----------

# Lists Windows Terminal tabs + other top-level windows as JSON, then reads the
# chosen index from stdin and selects that tab. Python does the choosing.
FOCUS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]; $TS = [System.Windows.Automation.TreeScope]
$tabc = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
$els = @(); $out = @()
foreach ($w in $A::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
  $hwnd = $w.Current.NativeWindowHandle
  if ($w.Current.ClassName -eq 'CASCADIA_HOSTING_WINDOW_CLASS') {
    foreach ($t in $w.FindAll($TS::Descendants, $tabc)) {
      $out += [pscustomobject]@{k=$els.Count; kind='tab'; hwnd=$hwnd; name=$t.Current.Name}; $els += $t }
  } elseif ($w.Current.Name) {
    $out += [pscustomobject]@{k=$els.Count; kind='window'; hwnd=$hwnd; name=$w.Current.Name}; $els += $w }
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($out) -Compress)); [Console]::Out.Flush()
$k = [int][Console]::In.ReadLine()
if ($k -ge 0 -and $k -lt $els.Count -and $out[$k].kind -eq 'tab') {
  try { $els[$k].GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() } catch {}
}
"""


LOG = Path(os.environ.get("TEMP") or Path.home() / ".cache") / "claude-agent-monitor.log"


def log_error():
    import traceback
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
    except OSError:
        pass


def bring_to_front(hwnd):
    import ctypes
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if not user32.SetForegroundWindow(hwnd):
        user32.keybd_event(0x12, 0, 0, 0)  # tap Alt to satisfy the foreground lock
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)


def focus_session(session):
    """Returns a short status string for the header."""
    return focus_windows(session) if IS_WINDOWS else focus_linux(session)


def focus_linux(session):
    import shutil
    import subprocess
    switched = False
    args = tmux_args(session)
    if args and shutil.which("tmux"):
        switched = subprocess.run(args, capture_output=True, timeout=5).returncode == 0
        time.sleep(0.2)  # let tmux push the pane title to the terminal window
    pane_note = "tmux pane selected" if switched else ""
    if os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("gdbus"):
            for needle in title_needles(session):
                r = subprocess.run(gnome_activate_command(needle), capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and "true" in r.stdout:
                    return ""
                if r.returncode != 0:
                    break  # extension not installed
        return pane_note or "can't raise windows on Wayland (see README)"
    if not shutil.which("wmctrl"):
        return pane_note or "install wmctrl to jump to terminals"
    listing = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
    target = choose_target(session, parse_wmctrl(listing))
    if not target:
        return pane_note or "terminal not found"
    subprocess.run(["wmctrl", "-i", "-a", hex(target["hwnd"])], capture_output=True, timeout=5)
    return ""


def focus_windows(session):
    import subprocess
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = tmux_command(session, distro_from_path(SESSIONS_DIR))
    if cmd:
        subprocess.run([system_exe(cmd[0]), *cmd[1:]], capture_output=True, timeout=5, creationflags=no_window)
        time.sleep(0.2)  # let tmux push the pane title to the terminal tab
    proc = subprocess.Popen(
        [system_exe("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", FOCUS_PS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=no_window, encoding="utf-8", errors="replace",
    )
    try:
        target = choose_target(session, parse_entries(proc.stdout.readline()))
        proc.stdin.write(f"{target['k'] if target else -1}\n")
        proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    if not target:
        return "terminal not found"
    bring_to_front(target["hwnd"])
    return ""


# ---------- view ----------

def main():
    import tkinter as tk

    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    prefs = {}
    try:
        prefs = json.loads(PREFS.read_text())
    except (OSError, ValueError):
        pass

    root = tk.Tk()
    root.title("Claude agents")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", prefs.get("alpha", 0.95))
    root.configure(bg=BG)
    root.geometry(f"+{prefs.get('x', 40)}+{prefs.get('y', 40)}")

    width = 380
    family = "Segoe UI" if IS_WINDOWS else "Ubuntu"
    font = (family, 9)
    bold = ("Segoe UI Semibold", 9) if IS_WINDOWS else (family, 9, "bold")
    small = (family, 8)
    tiny_bold = ("Segoe UI Semibold", 8) if IS_WINDOWS else (family, 8, "bold")
    ui = {"collapsed": prefs.get("collapsed", False), "sig": None, "prev": {}, "ages": [], "notice": ("", 0)}

    header = tk.Frame(root, bg=HEADER, width=width)
    header.pack(fill="x")
    title = tk.Label(header, text="◆ Claude agents", bg=HEADER, fg=FG, font=bold, padx=10, pady=6)
    title.pack(side="left")
    close = tk.Label(header, text="✕", bg=HEADER, fg=MUTED, font=bold, padx=10, cursor="hand2")
    close.pack(side="right")
    summary = tk.Label(header, text="", bg=HEADER, fg=MUTED, font=small)
    summary.pack(side="right")
    body = tk.Frame(root, bg=BG, width=width)

    def save_prefs():
        try:
            PREFS.write_text(json.dumps({"x": root.winfo_x(), "y": root.winfo_y(), "collapsed": ui["collapsed"], "alpha": root.attributes("-alpha")}))
        except OSError:
            pass

    def quit_app(_=None):
        save_prefs()
        root.destroy()

    drag = {}

    def start_drag(e):
        drag.update(x=e.x_root - root.winfo_x(), y=e.y_root - root.winfo_y())

    def do_drag(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    def toggle(_=None):
        ui["collapsed"] = not ui["collapsed"]
        body.pack_forget() if ui["collapsed"] else body.pack(fill="both")
        save_prefs()

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Collapse / expand", command=toggle)
    menu.add_command(label="Opacity 100%", command=lambda: root.attributes("-alpha", 1.0))
    menu.add_command(label="Opacity 80%", command=lambda: root.attributes("-alpha", 0.8))
    menu.add_separator()
    menu.add_command(label="Quit", command=quit_app)

    for w in (header, title, summary):
        w.bind("<Button-1>", start_drag)
        w.bind("<B1-Motion>", do_drag)
        w.bind("<ButtonRelease-1>", lambda e: save_prefs())
        w.bind("<Double-Button-1>", toggle)
        w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
    close.bind("<Button-1>", quit_app)

    def flash(times=6):
        if times <= 0:
            title.configure(bg=HEADER)
            header.configure(bg=HEADER)
            return
        colour = STATE_STYLE["waiting"][1] if times % 2 else HEADER
        title.configure(bg=colour)
        header.configure(bg=colour)
        root.after(250, flash, times - 1)

    def open_terminal(session):
        ui["notice"] = ("opening…", time.time() + 5)

        def work():
            try:
                msg = focus_session(session)
            except Exception:
                msg = "could not open terminal (see log)"
                log_error()
            ui["notice"] = (msg, time.time() + 3)

        threading.Thread(target=work, daemon=True).start()

    def bind_click(widget, session):
        widget.configure(cursor="hand2")
        widget.bind("<Button-1>", lambda e: open_terminal(session))
        for child in widget.winfo_children():
            bind_click(child, session)

    def render(sessions):
        for child in body.winfo_children():
            child.destroy()
        ui["ages"] = []
        if not sessions:
            tk.Label(body, text="No Claude Code sessions running", bg=BG, fg=MUTED, font=font, pady=14).pack(fill="x")
            return
        for s in sessions[:15]:
            label, accent = STATE_STYLE.get(s.get("state"), ("?", MUTED))
            card_bg = CARD_HOT if s.get("state") == "waiting" else CARD
            card = tk.Frame(body, bg=card_bg)
            card.pack(fill="x", padx=6, pady=(6, 0))
            tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
            inner = tk.Frame(card, bg=card_bg, padx=8, pady=5)
            inner.pack(side="left", fill="both", expand=True)
            top = tk.Frame(inner, bg=card_bg)
            top.pack(fill="x")
            name = s.get("project") or "?"
            heading = s.get("custom_title") or s.get("title")
            if heading:
                name = f"{name} · {heading}"
            tk.Label(top, text=name[:48] + ("…" if len(name) > 48 else ""), bg=card_bg, fg=FG, font=bold).pack(side="left")
            age = tk.Label(top, text="", bg=card_bg, fg=MUTED, font=small)
            age.pack(side="right")
            ui["ages"].append((age, s.get("updated_at")))
            tk.Label(top, text=label, bg=card_bg, fg=accent, font=tiny_bold).pack(side="right", padx=6)
            detail = detail_line(s)
            if detail:
                tk.Label(inner, text=detail, bg=card_bg, fg=FG, font=font, anchor="w", justify="left", wraplength=width - 40).pack(fill="x")
            if s.get("task"):
                tk.Label(inner, text="▸ " + s["task"], bg=card_bg, fg=MUTED, font=small, anchor="w", justify="left", wraplength=width - 40).pack(fill="x")
            bind_click(card, s)
        tk.Frame(body, bg=BG, height=6).pack()

    def tick():
        sessions = load_sessions()
        sig = [(s.get("session_id"), s.get("state"), detail_line(s), s.get("task"), s.get("title"), s.get("custom_title"), s.get("tmux_pane")) for s in sessions]
        if sig != ui["sig"]:
            ui["sig"] = sig
            render(sessions)
            now_waiting = {s["session_id"] for s in sessions if s.get("state") == "waiting"}
            if now_waiting - {k for k, v in ui["prev"].items() if v == "waiting"} and ui["prev"]:
                flash()
                try:
                    if IS_WINDOWS:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    else:
                        root.bell()
                except Exception:
                    pass
            ui["prev"] = {s["session_id"]: s.get("state") for s in sessions}
        now = time.time()
        for lbl, ts in ui["ages"]:
            lbl.configure(text=format_age(ts, now))
        notice, until = ui["notice"]
        summary.configure(text=notice if notice and now < until else counts_summary(sessions))
        root.after(1000, tick)

    if not ui["collapsed"]:
        body.pack(fill="both")
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
