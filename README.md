# aw-watcher-agents

An [ActivityWatch](https://activitywatch.net/) watcher for AI coding agents. ActivityWatch only
sees the window in front of you; this records what your agents do, one event per turn, even when
several run at once or you're away.

It works with [Claude Code](https://claude.com/claude-code) through its
[hooks](https://code.claude.com/docs/en/hooks.md). Nothing runs in the background.

## Install

```sh
pipx install git+ssh://git@github.com/vvrossem/aw-watcher-agents.git
aw-watcher-agents install --write
```

```console
# hooks written to /home/you/.claude/settings.json (backup in /home/you/.claude/settings.json.bak)
```

Without `--write`, `install` only prints the hooks, for you to check. Your other settings and hooks
stay as they are. From then on, every Claude Code session is recorded: CLI, VS Code, `claude -p`
and subagents. `aw-watcher-agents --help` lists the other options (aw-server host and port).

## What you get

One event per turn, from your prompt to the agent's answer. This one waited 2 minutes for an
approval:

```json
{
  "timestamp": "2026-11-10T08:20:00+00:00",
  "duration": 1800.0,
  "data": {
    "agent": "claude-code",
    "session_id": "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f",
    "cwd": "/home/you/acme/shop_odoo",
    "project": "shop_odoo",
    "turn_id": "p-0920",
    "trigger": "human",
    "git_branch": "ABC-641-discount",
    "prompt": "Implement the discount on invoice lines of ABC-641, as the card says",
    "outcome": "completed",
    "tool_calls": 4,
    "waiting": 119,
    "title": "Discount on invoice lines"
  }
}
```

The bucket is `aw-watcher-agents_<host>`. A subagent's run is an event of its own, with
`"trigger": "subagent"` and its `description`. The examples come from a replayed session.

<details>
<summary>All fields</summary>

| Field | |
|---|---|
| `trigger` | `human` (your prompt), `command` (a slash command, also in `command`), `task-notification` (a background task reporting back), `message` (from another session), `subagent` |
| `outcome` | `completed`, `interrupted` (Esc), `failed` (API error), `stalled` (interrupted after more than 30 min of silence, `--max-gap`) |
| `tool_calls` | the turn's tool calls, without its subagents' |
| `waiting` | seconds spent waiting for your approval |
| `waits` | each of those waits, as `[start, end]`: the agent wasn't working |
| `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` | the tokens of the turn's messages, read from the session's transcript (a subagent's run: from its own) |
| `title` | the session's title, yours (`/rename`) or the generated one |
| `continued` | `true` when another Stop hook kept the turn going: a second event, same `turn_id` |
| `subagent_id`, `agent_type`, `description` | for a subagent's run |

Empty fields are left out.

</details>

## Privacy

Events only go to your own aw-server, whose database isn't encrypted. Prompts are stored in full by
default, so tools reading the bucket can find a card key in them. To store less, install with
`--prompt-mode`:

| `--prompt-mode` | `prompt` stored |
|---|---|
| `full` | `Implement the discount on invoice lines of ABC-641, as the card says` |
| `truncate --prompt-chars 40` | `Implement the discount on invoice lines …` |
| `hash` | `9f0b62b62e02df47` |
| `none` | nothing |

## When aw-server is down

Events wait in `~/.local/state/aw-watcher-agents/` and go out with the next one, or now with
`flush`:

```console
$ aw-watcher-agents flush
1 events sent
```

## Limits

Hooks carry no time, so a turn starts and ends when Claude Code runs them. Pressing Esc fires no
hook: the turn ends at its last activity, once your next prompt or the end of the session closes
it. A `/loop` session looks like any other, apart from its `command`. Cloud sessions aren't
recorded.

## Development

```sh
python3 -m pytest
```

MPL-2.0
