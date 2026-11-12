import datetime as dt
import json
import os
import types

import pytest

from aw_watcher_agents import __main__ as cli
from aw_watcher_agents import hooks

DAY = dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)
SETTINGS = hooks.Settings()


def at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return DAY.replace(hour=h, minute=m)


@pytest.fixture
def run(tmp_path, monkeypatch):
    """feed hook events (hhmm, name, fields); returns the events written"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    written = []

    def feed(*steps, session="s1", settings=SETTINGS):
        for hhmm, name, fields in steps:
            hook = {"session_id": session, "hook_event_name": name, "cwd": str(tmp_path), **fields}
            hooks.handle(hook, at(hhmm), settings, written.extend)
        return written
    return feed


def summary(e):
    start = dt.datetime.fromisoformat(e["timestamp"])
    end = start + dt.timedelta(seconds=e["duration"])
    d = e["data"]
    return f"{start:%H:%M}", f"{end:%H:%M}", d.get("trigger"), d.get("outcome")


def test_a_turn_runs_from_the_prompt_to_stop(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "fix ABC-1", "prompt_id": "p1"}),
              ("09:01", "PreToolUse", {"tool_name": "Bash"}), ("09:02", "PostToolUse", {"tool_name": "Bash"}),
              ("09:03", "PreToolUse", {"tool_name": "Edit"}),
              ("09:05", "Stop", {}))
    assert [summary(e) for e in out] == [("09:00", "09:05", "human", "completed")]
    data = out[0]["data"]
    assert (data["turn_id"], data["prompt"], data["tool_calls"], data["agent"]) == ("p1", "fix ABC-1", 2, "claude-code")
    assert "waiting" not in data


def test_an_interrupted_turn_ends_at_its_last_activity(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "a"}), ("09:04", "PostToolUse", {}),
              ("09:10", "UserPromptSubmit", {"prompt": "b"}), ("09:12", "Stop", {}))
    assert [summary(e) for e in out] == [
        ("09:00", "09:04", "human", "interrupted"), ("09:10", "09:12", "human", "completed")]


def test_a_turn_left_silent_long_before_the_next_prompt_is_stalled(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "a"}), ("09:01", "PreToolUse", {}),
              ("10:00", "UserPromptSubmit", {"prompt": "b"}), ("10:01", "Stop", {}))
    assert [summary(e) for e in out] == [
        ("09:00", "09:01", "human", "stalled"), ("10:00", "10:01", "human", "completed")]


def test_a_long_wait_for_your_approval_doesnt_cut_the_turn(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "deploy"}), ("09:01", "PreToolUse", {}),
              ("09:01", "PermissionRequest", {}), ("09:45", "PostToolUse", {}), ("09:46", "PreToolUse", {}),
              ("09:50", "Stop", {}))
    assert [summary(e) for e in out] == [("09:00", "09:50", "human", "completed")]
    assert (out[0]["data"]["waiting"], out[0]["data"]["tool_calls"]) == (44 * 60, 2)


def test_work_after_a_blocked_stop_continues_the_turn(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "fix it", "prompt_id": "p1"}),
              ("09:05", "Stop", {}),
              ("09:04", "PostToolUse", {}),  # the last tool's background hook, arriving late: no new work
              ("09:06", "PreToolUse", {}), ("09:20", "Stop", {"stop_hook_active": True}))
    assert [summary(e) for e in out] == [
        ("09:00", "09:05", "human", "completed"), ("09:06", "09:20", "human", "completed")]
    assert [(e["data"]["turn_id"], e["data"].get("continued")) for e in out] == [("p1", None), ("p1", True)]


def test_a_rejected_tool_ends_the_wait_at_the_next_tool(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "x"}),
              ("09:01", "PermissionRequest", {}), ("09:01", "PermissionRequest", {}),  # a second one doesn't restart it
              ("09:03", "PreToolUse", {}), ("09:20", "PostToolUse", {}), ("09:21", "Stop", {}))
    assert out[0]["data"]["waiting"] == 120


def test_slash_commands_and_notifications_are_told_apart(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "/loop 5m check CI"}),
              ("09:00", "UserPromptExpansion", {"command_name": "loop", "prompt": "/loop 5m check CI"}),
              ("09:01", "Stop", {}),
              ("09:02", "UserPromptSubmit", {"prompt": "<task-notification>done</task-notification>"}),
              ("09:03", "Stop", {}),
              ("09:04", "UserPromptSubmit", {"prompt": "<cross-session-message from=x>hi</cross-session-message>"}),
              ("09:05", "Stop", {}),
              ("09:06", "UserPromptSubmit", {"prompt": "<field name='x'/> why is this invisible?"}),
              ("09:07", "Stop", {}))
    assert [(e["data"]["trigger"], e["data"].get("command")) for e in out] == [
        ("command", "/loop"), ("task-notification", None), ("message", None), ("human", None)]


def test_waiting_for_your_approval_is_measured(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "deploy"}), ("09:01", "PreToolUse", {"tool_name": "Bash"}),
              ("09:01", "PermissionRequest", {"tool_name": "Bash"}), ("09:05", "PostToolUse", {"tool_name": "Bash"}),
              ("09:06", "PreToolUse", {"tool_name": "Bash"}), ("09:06", "PermissionRequest", {"tool_name": "Bash"}),
              ("09:08", "PostToolUse", {"tool_name": "Bash"}), ("09:09", "Stop", {}))
    assert out[0]["data"]["waiting"] == 360
    # each wait with its times: what reads the bucket can take them out of the agent's work
    assert [[w[11:16] for w in wait] for wait in out[0]["data"]["waits"]] == [["09:01", "09:05"], ["09:06", "09:08"]]


def test_a_subagent_is_written_on_its_own_and_its_tools_arent_the_turns(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "research"}),
              ("09:01", "PreToolUse", {"tool_name": "Agent", "tool_input": {"description": "find the PRs"}}),
              ("09:01", "SubagentStart", {"agent_id": "ag1", "agent_type": "Explore"}),
              ("09:02", "PreToolUse", {"tool_name": "Grep", "agent_id": "ag1"}),
              ("09:09", "SubagentStop", {"agent_id": "ag1", "agent_type": "Explore"}),
              ("09:10", "Stop", {}))
    assert [summary(e) for e in out] == [
        ("09:01", "09:09", "subagent", "completed"), ("09:00", "09:10", "human", "completed")]
    assert (out[0]["data"]["description"], out[0]["data"]["agent_type"]) == ("find the PRs", "Explore")
    assert out[1]["data"]["tool_calls"] == 1


def test_parallel_subagents_get_their_own_descriptions(run):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "research"}),
              ("09:01", "PreToolUse", {"tool_name": "Agent", "tool_input": {"description": "A"}}),
              ("09:01", "PreToolUse", {"tool_name": "Agent", "tool_input": {"description": "B"}}),
              ("09:01", "SubagentStart", {"agent_id": "a1"}), ("09:01", "SubagentStart", {"agent_id": "a2"}),
              ("09:05", "SubagentStart", {"agent_id": "a3"}),  # resumed later: no Agent call of its own
              ("09:06", "SubagentStop", {"agent_id": "a1"}), ("09:07", "SubagentStop", {"agent_id": "a2"}),
              ("09:08", "SubagentStop", {"agent_id": "a3"}))
    assert [(e["data"]["subagent_id"], e["data"].get("description")) for e in out] == [
        ("a1", "A"), ("a2", "B"), ("a3", None)]


def test_the_title_comes_from_a_rename_or_the_transcript(run, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [{"type": "user"}, {"type": "ai-title", "aiTitle": "Invoices"}]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    out = run(("09:00", "UserPromptSubmit", {"prompt": "a"}), ("09:01", "Stop", {"transcript_path": str(transcript)}),
              ("09:02", "UserPromptSubmit", {"prompt": "b", "session_title": "my-name"}), ("09:03", "Stop", {}))
    assert [e["data"]["title"] for e in out] == ["Invoices", "my-name"]


def assistant(message_id, **tokens):
    return {"type": "assistant", "message": {"id": message_id, "usage": tokens}}


def test_a_turn_carries_the_tokens_of_its_messages(run, tmp_path):
    transcript = tmp_path / "s1.jsonl"
    # before the watcher saw the session (installed or resumed mid-session): not this turn's
    write = lambda *lines: transcript.open("a").write("".join(json.dumps(line) + "\n" for line in lines))
    write(assistant("m0", input_tokens=99, output_tokens=99))
    path = {"transcript_path": str(transcript)}
    run(("08:59", "SessionStart", path), ("09:00", "UserPromptSubmit", {"prompt": "a", **path}))
    # a message takes a line per content block, each with the same usage
    write({"type": "user"}, assistant("m1", input_tokens=10, output_tokens=5, cache_read_input_tokens=1000),
          assistant("m1", input_tokens=10, output_tokens=5, cache_read_input_tokens=1000),
          assistant("m2", input_tokens=3, output_tokens=7, cache_creation_input_tokens=200))
    run(("09:05", "Stop", path), ("09:06", "UserPromptSubmit", {"prompt": "b", **path}))
    write(assistant("m3", input_tokens=1, output_tokens=2))
    out = run(("09:07", "Stop", path))
    tokens = [{k: v for k, v in e["data"].items() if k.endswith("_tokens")} for e in out]
    assert tokens == [
        {"input_tokens": 13, "output_tokens": 12, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200},
        {"input_tokens": 1, "output_tokens": 2}]


def test_a_subagent_carries_the_tokens_of_its_own_transcript(run, tmp_path):
    (tmp_path / "s1" / "subagents").mkdir(parents=True)
    (tmp_path / "s1" / "subagents" / "agent-a1.jsonl").write_text("")
    path = {"transcript_path": str(tmp_path / "s1.jsonl"), "agent_id": "a1"}
    run(("09:00", "UserPromptSubmit", {"prompt": "a"}), ("09:01", "SubagentStart", path))
    (tmp_path / "s1" / "subagents" / "agent-a1.jsonl").write_text(json.dumps(assistant("x", output_tokens=40)) + "\n")
    out = run(("09:03", "SubagentStop", path))
    assert out[-1]["data"]["output_tokens"] == 40


def test_session_end_closes_the_turn_and_its_state(run, tmp_path):
    out = run(("09:00", "UserPromptSubmit", {"prompt": "a"}), ("09:02", "PreToolUse", {}),
              ("09:30", "SessionEnd", {"reason": "prompt_input_exit"}))
    assert [summary(e) for e in out] == [("09:00", "09:02", "human", "interrupted")]
    assert os.listdir(hooks.state_dir()) == []


def test_events_wait_while_aw_server_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    sent = []

    class Down:
        def __init__(self, *a, **kw):
            pass

        def info(self):
            raise OSError("refused")

    class Up(Down):
        def info(self):
            return {"hostname": "h"}

        def ensure_bucket(self, *a):
            pass

        def insert(self, bucket, events):
            sent.append((bucket, len(events)))

    a = types.SimpleNamespace(host="localhost", port=None, testing=False)
    monkeypatch.setattr(cli, "Client", Down)
    cli.writer(a)([{"timestamp": "x", "duration": 1, "data": {}}])
    cli.writer(a)([{"timestamp": "y", "duration": 1, "data": {}}])
    assert sent == []
    monkeypatch.setattr(cli, "Client", Up)
    assert cli.flush(a) == 2 and sent == [("aw-watcher-agents_h", 2)]
    assert cli.flush(a) == 0


def test_a_broken_line_doesnt_block_the_pending_events(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    os.makedirs(hooks.state_dir())
    with open(cli.pending_path(), "w") as f:
        f.write('{"timestamp": "x", "dur')  # a hook killed while writing
    sent = []

    class Up:
        def __init__(self, *a, **kw):
            pass

        def info(self):
            return {"hostname": "h"}

        def ensure_bucket(self, *a):
            pass

        def insert(self, bucket, events):
            sent.extend(events)

    monkeypatch.setattr(cli, "Client", Up)
    cli.writer(types.SimpleNamespace(host="localhost", port=None, testing=False))([{"timestamp": "y", "data": {}}])
    assert sent == [{"timestamp": "y", "data": {}}]


def test_install_carries_the_options_into_the_hook_command():
    a = cli.parser().parse_args(["--testing", "--port", "5777", "install", "--prompt-mode", "truncate",
                                 "--max-gap", "60"])
    assert cli.hook_command(a).split()[1:] == [
        "--port", "5777", "--testing", "hook", "--prompt-mode", "truncate", "--max-gap", "60"]


def test_install_adds_the_hooks_and_keeps_the_others():
    settings = {"model": "x", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]},
                 {"hooks": [{"type": "command", "command": "/old/aw-watcher-agents hook", "async": True}]}]}}
    new = cli.merged(settings, "/new/aw-watcher-agents hook")
    assert new["model"] == "x"
    assert [h["command"] for g in new["hooks"]["Stop"] for h in g["hooks"]] == [
        "notify-send done", "/new/aw-watcher-agents hook"]
    assert set(new["hooks"]) == set(cli.EVENTS)
    background = [name for name in cli.EVENTS if new["hooks"][name][-1]["hooks"][0].get("async")]
    assert background == list(cli.BACKGROUND)
    assert [(g.get("matcher"), g["hooks"][0].get("async", False)) for g in new["hooks"]["PreToolUse"]] == [
        ("^(Agent|Task)$", False), ("^(?!(Agent|Task)$)", True)]
