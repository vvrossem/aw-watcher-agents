"""Claude Code hooks to agent turns: Claude Code runs `aw-watcher-agents hook` on each event, with
the event's JSON on stdin (https://code.claude.com/docs/en/hooks.md). A turn opens on
UserPromptSubmit and is written to the bucket on Stop; a subagent's run is written on SubagentStop.

Hooks give no time and no event when you interrupt a turn: the time is the hook's own, and an
interrupted turn is written by the next prompt or the end of the session ("stalled" when it was
silent longer than the max gap). The turn in progress lives in a small state file per session,
locked since Claude Code runs the tool hooks of one session in the background, side by side.
"""
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, List, Optional

AGENT = "claude-code"
AGENT_TOOLS = ("Agent", "Task")  # the tool that starts a subagent, under its new and old name
DESCRIPTION_AGE = dt.timedelta(seconds=10)  # an Agent call's description, waiting for its subagent to start
LEFTOVER_AGE = 7 * 86400  # seconds after which a session's files are swept: it ended without SessionEnd


@dataclass
class Settings:
    prompt_mode: str = "full"  # full, truncate, hash or none
    prompt_chars: int = 200
    max_gap: dt.timedelta = dt.timedelta(minutes=30)


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "aw-watcher-agents")


@contextmanager
def session_state(session_id: str):
    """the session's state, read under a lock and written back on exit; an emptied state takes its
    files with it"""
    os.makedirs(state_dir(), exist_ok=True)
    path = os.path.join(state_dir(), f"{session_id}.json")
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        yield state
        if state:
            with open(path + ".tmp", "w") as f:
                json.dump(state, f)
            os.replace(path + ".tmp", path)
            return
        for leftover in (path, path + ".lock"):
            if os.path.exists(leftover):
                os.remove(leftover)


def sweep() -> None:
    """removes the files of sessions silent for a week: they ended without SessionEnd"""
    for path in glob.glob(os.path.join(state_dir(), "*.json*")):
        try:
            if time.time() - os.path.getmtime(path) > LEFTOVER_AGE:
                os.remove(path)
        except OSError:
            pass


def prompt_value(text: str, settings: Settings) -> Optional[str]:
    if settings.prompt_mode == "none":
        return None
    if settings.prompt_mode == "hash":
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    if settings.prompt_mode == "truncate" and len(text) > settings.prompt_chars:
        return text[: settings.prompt_chars] + "…"
    return text


def trigger_of(prompt: str) -> str:
    """UserPromptSubmit also fires for a background task or subagent reporting back and for a
    message from another session: Claude Code wraps those in their own tags"""
    if prompt.startswith("<task-notification>"):
        return "task-notification"
    if prompt.startswith("<cross-session-message"):
        return "message"
    return "human"


def git_branch(cwd: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True,
                             timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None


def ai_title(transcript: str, state: dict) -> Optional[str]:
    """the session's generated title: hooks only pass a custom one (/rename), so it's read from the
    transcript's latest "ai-title" line, from where the last read stopped; None when the format
    doesn't have it anymore"""
    try:
        with open(transcript, "rb") as f:
            f.seek(state.get("title_offset", 0))
            for line in f:
                if b'"ai-title"' in line:
                    try:
                        state["title"] = json.loads(line).get("aiTitle") or state.get("title")
                    except ValueError:
                        pass
            state["title_offset"] = f.tell()
    except OSError:
        pass
    return state.get("title")


TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def usage(transcript: Optional[str], state: dict) -> dict:
    """the tokens of the assistant messages written to the transcript since the last read: a message
    takes several lines, each with its usage, so each message counts once; empty when the transcript
    can't be read or doesn't have it anymore"""
    if not transcript:
        return {}
    offsets = state.setdefault("usage_offsets", {})
    seen, total = set(), dict.fromkeys(TOKENS, 0)
    try:
        with open(transcript, "rb") as f:
            f.seek(offsets.get(transcript, 0))
            for line in f:
                if b'"usage"' not in line:
                    continue
                try:
                    message = json.loads(line).get("message") or {}
                except ValueError:
                    continue
                if message.get("id") in seen or not isinstance(message.get("usage"), dict):
                    continue
                seen.add(message.get("id"))
                for key in TOKENS:
                    total[key] += message["usage"].get(key) or 0
            offsets[transcript] = f.tell()
    except OSError:
        return {}
    return {k: v for k, v in total.items() if v}


def usage_from_here(transcript: Optional[str], state: dict) -> None:
    """a turn's tokens are those written after its prompt: a transcript seen for the first time
    (installed or resumed mid-session) is read from its end"""
    offsets = state.setdefault("usage_offsets", {})
    if transcript and transcript not in offsets:
        try:
            offsets[transcript] = os.path.getsize(transcript)
        except OSError:
            pass


def subagent_transcript(hook: dict) -> Optional[str]:
    """a subagent's own transcript, next to its session's"""
    if hook.get("agent_transcript_path"):
        return hook["agent_transcript_path"]
    if not hook.get("transcript_path"):
        return None
    folder = os.path.splitext(hook["transcript_path"])[0]
    return os.path.join(folder, "subagents", f"agent-{hook['agent_id']}.jsonl")


def event(start: dt.datetime, end: dt.datetime, data: dict) -> dict:
    return {"timestamp": start.isoformat(), "duration": max((end - start).total_seconds(), 0),
            "data": {k: v for k, v in data.items() if v not in (None, "")}}


def stop_waiting(turn: dict, now: dt.datetime) -> None:
    if turn.get("waiting_since"):
        since = dt.datetime.fromisoformat(turn.pop("waiting_since"))
        turn["waiting"] += (now - since).total_seconds()
        turn.setdefault("waits", []).append([since.isoformat(), now.isoformat()])


def close_turn(state: dict, end: dt.datetime, outcome: str) -> Optional[dict]:
    turn = state.pop("turn", None)
    if not turn:
        return None
    stop_waiting(turn, end)
    data = {**turn["data"], "outcome": outcome, "tool_calls": turn["tool_calls"],
            "waiting": round(turn["waiting"]) or None, "waits": turn.get("waits"),
            "title": state.get("custom_title") or state.get("title"), **usage(state.get("transcript"), state)}
    return event(dt.datetime.fromisoformat(turn["start"]), end, data)


def new_turn(now: dt.datetime, data: dict) -> dict:
    return {"start": now.isoformat(), "last": now.isoformat(), "tool_calls": 0, "waiting": 0.0, "data": data}


def last_activity(turn: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(turn["last"])


def handle(hook: dict, now: dt.datetime, settings: Settings, write: Callable[[List[dict]], None]) -> None:
    """apply one hook event to its session's state, and write the turns or runs it ends"""
    session_id = hook.get("session_id")
    if not session_id:
        return
    name = hook.get("hook_event_name")
    own = not hook.get("agent_id")  # an event of the session itself, not of one of its subagents
    out: List[dict] = []
    with session_state(session_id) as state:
        turn = state.get("turn")
        if hook.get("session_title"):
            state["custom_title"] = hook["session_title"]
        if own and hook.get("transcript_path"):
            state["transcript"] = hook["transcript_path"]
        cwd = hook.get("cwd") or ""
        base = {"agent": AGENT, "session_id": session_id, "cwd": cwd, "project": os.path.basename(cwd.rstrip("/"))}
        stopped_at = state.get("stopped_at")
        if (name in ("PreToolUse", "SubagentStart") and own and not turn and stopped_at
                and now > dt.datetime.fromisoformat(stopped_at)):
            # a new tool call after Stop, without a new prompt: another Stop hook kept the turn going.
            # (a background hook of the turn's last tool can arrive after Stop: its time is earlier)
            state["turn"] = turn = new_turn(now, {**state["stopped"], "continued": True})
        if name == "UserPromptSubmit":
            if turn:  # no Stop came: interrupted, or stalled when it fell silent long ago
                silent = now - last_activity(turn) > settings.max_gap
                out.append(close_turn(state, last_activity(turn), "stalled" if silent else "interrupted"))
            state.pop("stopped", None)
            state.pop("stopped_at", None)
            prompt = hook.get("prompt") or ""
            usage_from_here(state.get("transcript"), state)
            state["turn"] = turn = new_turn(now, {
                **base, "turn_id": hook.get("prompt_id") or str(uuid.uuid4()), "trigger": trigger_of(prompt),
                "git_branch": git_branch(cwd), "prompt": prompt_value(prompt, settings)})
        elif name == "UserPromptExpansion" and turn:
            turn["data"].update(trigger="command", command=f"/{hook.get('command_name', '').lstrip('/')}")
        elif name == "SubagentStart" and hook.get("agent_id"):
            usage_from_here(subagent_transcript(hook), state)
            # the description of the Agent call that started it, if one is waiting (a resumed subagent
            # or a teammate's new message has none)
            waiting = [d for d in state.pop("descriptions", [])
                       if now - dt.datetime.fromisoformat(d["at"]) <= DESCRIPTION_AGE]
            state.setdefault("subagents", {})[hook["agent_id"]] = {
                "start": now.isoformat(), "agent_type": hook.get("agent_type"),
                "description": waiting.pop(0)["text"] if waiting else None}
            if waiting:
                state["descriptions"] = waiting
        elif name == "SubagentStop" and hook.get("agent_id"):
            run = state.get("subagents", {}).pop(hook["agent_id"], None)
            if run:
                parent = (turn or {}).get("data", {}) or state.get("stopped", {})
                out.append(event(dt.datetime.fromisoformat(run["start"]), now, {
                    **base, "turn_id": hook["agent_id"], "trigger": "subagent", "outcome": "completed",
                    "subagent_id": hook["agent_id"], "agent_type": run["agent_type"],
                    "description": run["description"], "git_branch": parent.get("git_branch"),
                    "title": state.get("custom_title") or state.get("title"),
                    **usage(subagent_transcript(hook), state)}))
        elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"):
            if name == "PreToolUse" and hook.get("tool_name") in AGENT_TOOLS:
                state.setdefault("descriptions", []).append(
                    {"at": now.isoformat(), "text": (hook.get("tool_input") or {}).get("description")})
            if turn and own:  # a subagent's own tools aren't the turn's
                if name == "PreToolUse":
                    turn["tool_calls"] += 1
                if name == "PermissionRequest":
                    turn.setdefault("waiting_since", now.isoformat())  # a wait already running goes on
                else:
                    # the tool ran, was denied, or Claude moved on to another tool: you rejected it
                    stop_waiting(turn, now)
        elif name in ("Stop", "StopFailure") and turn and own:
            if hook.get("transcript_path"):
                ai_title(hook["transcript_path"], state)
            turn["last"] = now.isoformat()
            state["stopped"], state["stopped_at"] = turn["data"], now.isoformat()
            out.append(close_turn(state, now, "completed" if name == "Stop" else "failed"))
            turn = None
        elif name == "SessionEnd":
            if turn:
                silent = now - last_activity(turn) > settings.max_gap
                out.append(close_turn(state, last_activity(turn), "stalled" if silent else "interrupted"))
            state.clear()  # its files go: a resumed session starts new ones
            turn = None
        if turn:
            turn["last"] = now.isoformat()
    if name == "SessionEnd":
        sweep()
    events = [e for e in out if e]
    if events:
        write(events)
