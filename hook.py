#!/usr/bin/env python3
"""Claude Code hook: record each session's state for the floating agent monitor.

Registered for SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
Notification, Stop and SessionEnd (install.py does that). Writes
~/.claude/agent-monitor/sessions/<session_id>.json. Never prints and always
exits 0 so it cannot disturb Claude.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STATUS_DIR = Path.home() / ".claude" / "agent-monitor" / "sessions"
SNIPPET = 160
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def snippet(text, limit=SNIPPET):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def proc_starttime(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2 :].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _proc_info(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return comm, ppid, cmdline
    except (OSError, ValueError, IndexError):
        return None


def find_claude_process():
    """Walk up the parent chain to the long-lived claude process."""
    pid = os.getppid()
    for _ in range(8):
        info = _proc_info(pid)
        if not info:
            break
        comm, ppid, cmdline = info
        if comm == "claude" or re.search(r"(^|/)claude(\s|$)|claude-code", cmdline):
            return pid, proc_starttime(pid)
        if ppid <= 1:
            break
        pid = ppid
    pid = os.getppid()
    return pid, proc_starttime(pid)


def describe_tool(name, tool_input):
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    short = name.split("__")[-1] if name.startswith("mcp__") else name
    if "file_path" in tool_input or "notebook_path" in tool_input:
        detail = os.path.basename(str(tool_input.get("file_path") or tool_input.get("notebook_path")))
    else:
        detail = next(
            (tool_input[k] for k in ("description", "command", "pattern", "query", "url", "skill", "prompt") if tool_input.get(k)),
            "",
        )
    return snippet(f"{short}: {detail}" if detail else short, 120)


def first_question(tool_input):
    try:
        return tool_input["questions"][0]["question"]
    except (KeyError, IndexError, TypeError):
        return ""


def _tail_lines(path, max_bytes=256_000):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            return fh.read().decode(errors="replace").splitlines()
    except (OSError, TypeError):
        return []


def last_assistant_text(transcript_path):
    for line in reversed(_tail_lines(transcript_path)):
        try:
            entry = json.loads(line)
            content = entry["message"]["content"] if entry.get("type") == "assistant" else None
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                return " ".join(texts)
    return ""


TITLE_KEYS = {"ai-title": ("aiTitle", "title"), "custom-title": ("customTitle", "custom_title")}


def transcript_titles(transcript_path):
    """Latest session titles; Claude shows these as the terminal/tab title."""
    found = {}
    for line in reversed(_tail_lines(transcript_path)):
        if "-title" not in line:
            continue
        try:
            entry = json.loads(line)
            src, dest = TITLE_KEYS[entry.get("type")]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if dest not in found and isinstance(entry.get(src), str) and entry[src].strip():
            found[dest] = entry[src].strip()
        if len(found) == len(TITLE_KEYS):
            break
    return found


def load(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def prune_dead(status_dir, keep):
    for path in status_dir.glob("*.json"):
        if path.name == keep:
            continue
        data = load(path)
        pid = data.get("pid")
        if isinstance(pid, int) and proc_starttime(pid) != data.get("pid_start"):
            try:
                path.unlink()
            except OSError:
                pass


def apply_event(status, event, payload):
    if event == "SessionStart":
        status.update(state="idle", message="", activity="")
    elif event == "UserPromptSubmit":
        status.update(state="working", task=snippet(payload.get("prompt")), activity="", message="", summary="")
    elif event == "PreToolUse":
        tool, tool_input = str(payload.get("tool_name") or "tool"), payload.get("tool_input")
        if tool == "AskUserQuestion":
            status.update(state="waiting", message=snippet("Question: " + first_question(tool_input)))
        else:
            status.update(state="working", activity=describe_tool(tool, tool_input), message="")
    elif event == "PostToolUse":
        status.update(state="working", message="")
    elif event == "Notification":
        idle = payload.get("notification_type") == "idle_prompt"
        if not (idle and status.get("state") in ("done", "idle")):
            status.update(state="waiting", message=snippet(payload.get("message")))
    elif event == "Stop":
        text = payload.get("last_assistant_message") or last_assistant_text(payload.get("transcript_path"))
        status.update(state="done", summary=snippet(text), activity="", message="")
    return status


def main(stdin=sys.stdin, stdout=sys.stdout, status_dir=STATUS_DIR):
    try:
        payload = json.loads(stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        sid, event = str(payload.get("session_id") or ""), payload.get("hook_event_name")
        if not SAFE_ID.match(sid) or not event:
            return 0
        status_dir = Path(status_dir)
        path = status_dir / f"{sid}.json"
        if event == "SessionEnd":
            path.unlink(missing_ok=True)
            return 0
        status_dir.mkdir(parents=True, exist_ok=True)
        status = load(path)
        cwd = str(payload.get("cwd") or status.get("cwd") or "")
        status.update(session_id=sid, cwd=cwd, project=os.path.basename(cwd.rstrip("/")) or cwd)
        if payload.get("transcript_path"):
            status.update(transcript_titles(payload["transcript_path"]))
        if os.environ.get("TMUX_PANE") and os.environ.get("TMUX"):
            status.update(tmux_pane=os.environ["TMUX_PANE"], tmux_socket=os.environ["TMUX"].split(",")[0])
        if "pid" not in status:
            status["pid"], status["pid_start"] = find_claude_process()
        apply_event(status, event, payload)
        status["updated_at"] = time.time()
        save(path, status)
        if event in ("SessionStart", "Stop"):
            prune_dead(status_dir, keep=path.name)
    except Exception:  # a status hook must never break or block Claude
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
