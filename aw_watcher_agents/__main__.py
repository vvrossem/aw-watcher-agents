"""aw-watcher-agents: write AI coding agent turns to ActivityWatch, from Claude Code's hooks."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shlex
import shutil
import sys
import urllib.error

from . import hooks
from .client import Client

CLIENT = "aw-watcher-agents"
BUCKET_TYPE = "agent.turn"
# the events that open or close a turn, a subagent's run or a wait for your approval: Claude Code
# waits for these hooks, so they're handled in order (async, Stop and SessionEnd race at the end of
# a `claude -p` run)
IN_ORDER = ("UserPromptSubmit", "UserPromptExpansion", "SubagentStart", "SubagentStop", "PermissionRequest",
            "Stop", "StopFailure", "SessionEnd")
# tool events only count calls and end waits: they run async, in the background, except an Agent
# call, whose description must be there when its subagent starts
BACKGROUND = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied")
EVENTS = IN_ORDER + BACKGROUND
AGENT_CALLS = "^(Agent|Task)$"


def bucket_id(hostname: str) -> str:
    return f"{CLIENT}_{hostname}"


def pending_path() -> str:
    return os.path.join(hooks.state_dir(), "pending.jsonl")


def writer(a):
    """writes events to the bucket; when aw-server doesn't answer, keeps them for the next write"""
    def write(events):
        with pending_lock():
            with open(pending_path(), "a+") as f:
                f.seek(0, os.SEEK_END)
                if f.tell():  # a hook killed mid-write may have left a line unfinished
                    f.seek(f.tell() - 1)
                    if f.read(1) != "\n":
                        f.write("\n")
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            send_pending(a)
    return write


@contextlib.contextmanager
def pending_lock():
    os.makedirs(hooks.state_dir(), exist_ok=True)
    with open(pending_path() + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def flush(a) -> int:
    with pending_lock():
        return send_pending(a)


def send_pending(a) -> int:
    """sends the events kept while aw-server didn't answer; returns how many were sent"""
    events = []
    try:
        with open(pending_path()) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass  # an unfinished line: lost, the others go out
    except OSError:
        return 0
    if not events:
        return 0
    client = Client(a.host, a.port or (5666 if a.testing else 5600), timeout=3)
    try:
        hostname = client.info()["hostname"]
        client.ensure_bucket(bucket_id(hostname), BUCKET_TYPE, hostname, CLIENT)
        client.insert(bucket_id(hostname), events)
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return 0
    os.remove(pending_path())
    return len(events)


def cmd_hook(a) -> None:
    """one hook event on stdin; never fails, so it can't get in Claude Code's way"""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        hook = json.load(sys.stdin)
        settings = hooks.Settings(a.prompt_mode, a.prompt_chars, dt.timedelta(minutes=a.max_gap))
        hooks.handle(hook, now, settings, writer(a))
    except Exception as e:  # noqa: BLE001 - a hook must never break the session
        print(f"{CLIENT}: {e}", file=sys.stderr)


def settings_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "settings.json")


def hook_command(a) -> str:
    """the command the hooks run, with the options install was given"""
    exe = shutil.which(CLIENT) or os.path.abspath(sys.argv[0])
    server = [*(["--host", a.host] if a.host != "localhost" else []), *(["--port", str(a.port)] if a.port else []),
              *(["--testing"] if a.testing else [])]
    hook = [*(["--prompt-mode", a.prompt_mode] if a.prompt_mode != "full" else []),
            *(["--prompt-chars", str(a.prompt_chars)] if a.prompt_chars != 200 else []),
            *(["--max-gap", str(a.max_gap)] if a.max_gap != 30 else [])]
    return " ".join(shlex.quote(x) for x in [exe, *server, "hook", *hook])


def is_ours(group: dict) -> bool:
    return any(f"{CLIENT} hook" in h.get("command", "") for h in group.get("hooks", []))


def merged(settings: dict, command: str) -> dict:
    """the settings with the watcher's hooks, replacing an earlier install's"""
    out = dict(settings)
    all_hooks = {k: list(v) for k, v in out.get("hooks", {}).items()}
    for name in EVENTS:
        groups = [g for g in all_hooks.get(name, []) if not is_ours(g)]
        handler = {"type": "command", "command": command}
        if name == "PreToolUse":  # an Agent call in order, the other tools in the background
            groups.append({"matcher": AGENT_CALLS, "hooks": [handler]})
            groups.append({"matcher": "^(?!(Agent|Task)$)", "hooks": [{**handler, "async": True}]})
        else:
            groups.append({"hooks": [{**handler, "async": True} if name in BACKGROUND else handler]})
        all_hooks[name] = groups
    out["hooks"] = all_hooks
    return out


def cmd_install(a) -> None:
    path = settings_path()
    try:
        with open(path) as f:
            settings = json.load(f)
    except OSError:
        settings = {}
    new = merged(settings, hook_command(a))
    if not a.write:
        print(json.dumps({"hooks": {k: new["hooks"][k] for k in EVENTS}}, indent=2))
        print(f"# --write adds them to {path}", file=sys.stderr)
        return
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak")
    with open(path, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    print(f"# hooks written to {path} (backup in {path}.bak)", file=sys.stderr)


def cmd_flush(a) -> None:
    print(f"{flush(a)} events sent")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=CLIENT, description=__doc__)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=None, help="aw-server port [5600, 5666 with --testing]")
    p.add_argument("--testing", action="store_true", help="use the testing aw-server (port 5666)")
    sub = p.add_subparsers(dest="cmd", required=True)
    hook = sub.add_parser("hook", help="record one Claude Code hook event, read from stdin")
    hook.add_argument("--prompt-mode", choices=("full", "truncate", "hash", "none"), default="full",
                      help="how much of your prompts goes to the bucket [full]")
    hook.add_argument("--prompt-chars", type=int, default=200, help="length kept by --prompt-mode truncate [200]")
    hook.add_argument("--max-gap", type=int, default=30,
                      help="minutes of silence after which an unfinished turn is closed [30]")
    install = sub.add_parser("install", help="the hooks to add to Claude Code's settings")
    install.add_argument("--write", action="store_true", help="add them to the user settings (with a backup)")
    install.add_argument("--prompt-mode", choices=("full", "truncate", "hash", "none"), default="full")
    install.add_argument("--prompt-chars", type=int, default=200)
    install.add_argument("--max-gap", type=int, default=30)
    sub.add_parser("flush", help="send the events kept while aw-server didn't answer")
    return p


def main(argv=None) -> None:
    a = parser().parse_args(argv)
    {"hook": cmd_hook, "install": cmd_install, "flush": cmd_flush}[a.cmd](a)


if __name__ == "__main__":
    main()
