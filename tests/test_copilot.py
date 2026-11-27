import json
import os

from aw_watcher_agents import copilot


def event_line(timestamp, typ, data=None):
    return json.dumps({"timestamp": timestamp, "type": typ, "data": data or {}}) + "\n"


def test_copilot_events_become_one_completed_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    path = session_dir / "events.jsonl"
    path.write_text(
        event_line("2026-11-27T14:00:00Z", "session.start", {"sessionId": "s1"})
        + event_line("2026-11-27T14:00:01Z", "user.message", {
            "content": "fix ABC-1",
            "turnId": "0",
            "messageId": "m1",
        })
        + event_line("2026-11-27T14:00:02Z", "tool.execution_start", {"turnId": "0"})
        + event_line("2026-11-27T14:00:10Z", "assistant.turn_end", {"turnId": "0"})
    )

    out = copilot.scan(str(tmp_path / "sessions"))
    assert out == []

    with path.open("a") as f:
        f.write(event_line("2026-11-27T14:00:11Z", "user.message", {
            "content": "next task",
            "turnId": "1",
            "messageId": "m2",
        }))
    out = copilot.scan(str(tmp_path / "sessions"))
    assert len(out) == 1
    assert out[0]["timestamp"] == "2026-11-27T14:00:01+00:00"
    assert out[0]["duration"] == 9
    assert out[0]["data"]["agent"] == "github-copilot"
    assert out[0]["data"]["turn_id"] == "0"
    assert out[0]["data"]["prompt"] == "fix ABC-1"
    assert out[0]["data"]["tool_calls"] == 1
    assert out[0]["data"]["outcome"] == "completed"


def test_copilot_session_idle_closes_the_human_task(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    path = session_dir / "events.jsonl"
    path.write_text(
        event_line("2026-11-27T14:00:00Z", "user.message", {"content": "fix it", "turnId": "0"})
        + event_line("2026-11-27T14:00:01Z", "assistant.turn_end", {"turnId": "0"})
        + event_line("2026-11-27T14:00:10Z", "session.idle", {})
    )

    out = copilot.scan(str(tmp_path / "sessions"))
    assert len(out) == 1
    assert out[0]["duration"] == 10
    assert out[0]["data"]["outcome"] == "completed"


def test_copilot_prompt_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    path = session_dir / "events.jsonl"
    path.write_text(
        event_line("2026-11-27T14:00:00Z", "user.message", {"content": "a very long prompt", "turnId": "0"})
        + event_line("2026-11-27T14:00:01Z", "assistant.turn_end", {"turnId": "0"})
    )
    out = copilot.scan(str(tmp_path / "sessions"), prompt_mode="truncate", prompt_chars=5)
    assert out == []

    with path.open("a") as f:
        f.write(event_line("2026-11-27T14:00:02Z", "session.shutdown", {"reason": "user_exit"}))
    out = copilot.scan(str(tmp_path / "sessions"), prompt_mode="truncate", prompt_chars=5)
    assert len(out) == 1
    assert out[0]["data"]["prompt"] == "a ver…"
