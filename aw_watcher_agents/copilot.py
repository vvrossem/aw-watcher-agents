"""GitHub Copilot session events -> ActivityWatch events."""
import datetime as dt
import glob
import hashlib
import json
import os
import re
import time
from typing import Callable, Iterable, Optional

AGENT = "github-copilot"


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "aw-watcher-agents", "copilot")


def prompt_value(text: str, mode: str, chars: int) -> Optional[str]:
    if mode == "none":
        return None
    if mode == "hash":
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    if mode == "truncate" and len(text) > chars:
        return text[:chars] + "…"
    return text


def timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def workspace(session_id: str) -> dict:
    path = os.path.expanduser(f"~/.copilot/session-state/{session_id}/workspace.yaml")
    result = {}
    try:
        with open(path) as f:
            for line in f:
                match = re.match(r"^([^:#]+):\s*(.*)$", line.rstrip())
                if match:
                    key, value = match.groups()
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def event(start: dt.datetime, end: dt.datetime, data: dict) -> dict:
    return {
        "timestamp": start.isoformat(),
        "duration": max((end - start).total_seconds(), 0),
        "data": {k: v for k, v in data.items() if v not in (None, "")},
    }


def close_active(session_id: str, active: dict, end: dt.datetime, outcome: str, prompt_mode: str,
                 prompt_chars: int) -> Optional[dict]:
    if not active:
        return None
    start = timestamp(active["start"])
    meta = workspace(session_id)
    prompt = active.get("prompt") or ""
    data = {
        "agent": AGENT,
        "session_id": session_id,
        "cwd": meta.get("cwd"),
        "project": meta.get("repository") or (os.path.basename(meta["cwd"].rstrip("/")) if meta.get("cwd") else None),
        "turn_id": active.get("turn_id"),
        "trigger": "human",
        "git_branch": meta.get("branch"),
        "prompt": prompt_value(prompt, prompt_mode, prompt_chars),
        "outcome": outcome,
        "tool_calls": active.get("tool_calls", 0) or None,
        "title": meta.get("name"),
    }
    return event(start, end, data)


def sessions(state_home: Optional[str] = None) -> Iterable[tuple[str, str]]:
    base = state_home or os.path.expanduser("~/.copilot/session-state")
    for path in glob.glob(os.path.join(base, "*", "events.jsonl")):
        session_id = os.path.basename(os.path.dirname(path))
        yield session_id, path


def scan(state_home: Optional[str] = None, prompt_mode: str = "full", prompt_chars: int = 200,
         max_gap: dt.timedelta = dt.timedelta(minutes=30)) -> list[dict]:
    """Read newly appended Copilot events and return completed turn events."""
    os.makedirs(state_dir(), exist_ok=True)
    offsets_path = os.path.join(state_dir(), "offsets.json")
    active_path = os.path.join(state_dir(), "active.json")
    try:
        with open(offsets_path) as f:
            offsets = json.load(f)
    except (OSError, ValueError):
        offsets = {}
    try:
        with open(active_path) as f:
            active_sessions = json.load(f)
    except (OSError, ValueError):
        active_sessions = {}

    now = dt.datetime.now(dt.timezone.utc)
    written = []
    for session_id, path in sessions(state_home):
        offset = offsets.get(session_id, 0)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                lines = f.readlines()
                offsets[session_id] = f.tell()
        except OSError:
            continue
        active = active_sessions.get(session_id)
        for raw in lines:
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            typ = item.get("type")
            data = item.get("data") or {}
            stamp = item.get("timestamp")
            if not stamp:
                continue
            if typ == "session.start":
                active = active_sessions.get(session_id)
            elif typ == "session.idle" and active:
                end = timestamp(stamp)
                written.append(close_active(session_id, active, end, "completed",
                                            prompt_mode, prompt_chars))
                active = None
            elif typ == "session.task_complete" and active:
                end = timestamp(stamp)
                written.append(close_active(session_id, active, end, "completed",
                                            prompt_mode, prompt_chars))
                active = None
            elif typ == "session.error" and active:
                end = timestamp(active.get("last", stamp))
                written.append(close_active(session_id, active, end, "failed",
                                            prompt_mode, prompt_chars))
                active = None
            elif typ == "user.message":
                if active:
                    end = timestamp(active.get("last", stamp))
                    silent = timestamp(stamp) - end > max_gap
                    written.append(close_active(session_id, active, end,
                                                "completed" if active.get("ended") else
                                                ("stalled" if silent else "interrupted"),
                                                prompt_mode, prompt_chars))
                active = {
                    "start": stamp,
                    "last": stamp,
                    "turn_id": data.get("turnId") or data.get("messageId"),
                    "prompt": data.get("content") or data.get("transformedContent") or "",
                    "tool_calls": 0,
                }
            elif typ == "tool.execution_start" and active:
                active["tool_calls"] = active.get("tool_calls", 0) + 1
            elif typ == "assistant.turn_end" and active:
                active["last"] = stamp
                active["ended"] = True
            elif typ == "session.shutdown" and active:
                end = timestamp(active.get("last", stamp))
                written.append(close_active(session_id, active, end, "completed" if active.get("ended") else "interrupted",
                                            prompt_mode, prompt_chars))
                active = None
            if active:
                active_sessions[session_id] = active
            else:
                active_sessions.pop(session_id, None)

    os.makedirs(state_dir(), exist_ok=True)
    with open(offsets_path, "w") as f:
        json.dump(offsets, f)
    with open(active_path, "w") as f:
        json.dump(active_sessions, f)
    return [e for e in written if e]


def run(write: Callable[[list[dict]], None], interval: float = 2.0, once: bool = False,
        prompt_mode: str = "full", prompt_chars: int = 200) -> None:
    while True:
        write(scan(prompt_mode=prompt_mode, prompt_chars=prompt_chars))
        if once:
            return
        time.sleep(interval)
