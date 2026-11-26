# Pull Requests

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/pull-request-plugin/actions/workflows/plugin-ci.yml/badge.svg)](https://github.com/mindroom-ai/pull-request-plugin/actions/workflows/plugin-ci.yml)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Tools](https://img.shields.io/badge/docs-tools-blue)](https://docs.mindroom.chat/tools/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) plugin that links GitHub pull requests to Matrix threads as room state, so agents and chat clients always know which PRs belong to each conversation.

- **Auto-linking on creation:** whenever an agent creates a PR with `gh pr create` (or `gh pr new`) in its shell tool, or with the `github` toolkit's `create_pull_request`, a hook links it to the thread automatically.
- **Manual linking tools:** agents with the `pull_requests` toolkit can link and unlink PRs on the current thread, and list linked PRs for the current thread, a room, or all shared rooms (with optional `resolved` filtering).
- **MindRoom Chat UI:** the bundled [`pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin) reads these links to display thread card PR chips, hover details, the thread PR menu, and an in-app diff side panel.

```text
Agent runs:   gh pr create --title "Fix retry backoff"
Thread state: linked to https://github.com/owner/repo/pull/42
```

## Quick start

```bash
mindroom plugins install pull-request-plugin
```

```yaml
plugins:
  - path: plugins/pull-request-plugin

agents:
  dev:
    tools: [pull_requests]
```

The auto-linking hook works for every agent automatically once the plugin is loaded, and no plugin `settings:` are needed. Add `pull_requests` to `agents.<name>.tools` for any agent that should also link, unlink, or list PRs manually.

Update later with `mindroom plugins update pull-request-plugin`.
Needs a MindRoom version supporting plugin API 1.35 or later.

## Tools

Toolkit name: `pull_requests`.

| Tool | Purpose |
|------|---------|
| `link_pull_request(url)` | Link a GitHub pull request URL to the current thread |
| `unlink_pull_request(url)` | Remove a pull request link from the current thread |
| `list_pull_requests(scope=None, room_id=None, resolved=None)` | List linked pull requests for the current thread (default), a room (`scope="room"`, optional `room_id`), or all shared rooms (`scope="all"` or `room_id="all"`), optionally filtered by thread `resolved` status |

`link_pull_request` and `unlink_pull_request` are thread-scoped and require an active thread. `list_pull_requests` defaults to the current thread when called with no arguments, while room-wide and cross-room (`scope="room"` / `scope="all"`) listings also work outside a thread and check requester room access. Because they require room context, these tools always run in MindRoom's primary process and are omitted where there is no room (such as the MCP gateway).

## Hooks

| Hook | Event | Timeout | Purpose |
|------|-------|---------|---------|
| `link-created-pull-request` | `tool:after_call` | `5000 ms` | Link the pull request created by a shell or `github` tool call to its thread |

<details>
<summary><b>How it works</b></summary>

1. After a tool call finishes inside a thread without raising or being blocked (and with a non-`None` result), `link-created-pull-request` inspects the call:
   - **`run_shell_command`:** matches commands containing `gh pr create` or `gh pr new`.
     - Skips saved-output file receipts (`mindroom_tool_output` JSON/dict payloads).
     - Strips any leading `[cwd: ...]` line produced by older MindRoom versions.
     - On success, takes the last PR URL from stdout (before the last `Stderr:` section), falling back to the full output if stdout has none.
     - On non-zero exit (output starting with `Exit code `), scans the full output so an existing PR URL printed by `gh` for the branch is still linked.
   - **`create_pull_request` (`github` toolkit):** parses the JSON result and extracts a valid GitHub PR `html_url`, ignoring error payloads or non-JSON results.
2. If the PR is not yet linked to that thread, the hook writes one room-state event.
3. Calls outside a thread, unrelated commands, and outputs without a PR URL are ignored. Hook failures are logged as warnings and never interrupt the turn.
4. Writing a link requires permission to send `com.mindroom.thread.pull_request` state events in the room; if the agent's Matrix account lacks permission, MindRoom retries through the router account.

</details>

<details>
<summary><b>Matrix room-state storage</b></summary>

- **Event type:** `com.mindroom.thread.pull_request`
- **State key:** `["<thread root event ID>","<owner>/<repo>#<number>"]` as compact JSON (no spaces), with owner and repository lowercased
- **Payload fields:**
  - `url`: `https://github.com/<owner>/<repo>/pull/<number>` (owner and repository lowercased)
  - `set_by`: `ctx.agent_name` when linked by the `tool:after_call` hook; agent name (falling back to requester ID when agent name is absent) when linked manually via `link_pull_request`
  - `set_at`: UTC ISO 8601 timestamp
- **Ordering:** `list_pull_requests()` returns thread links sorted by `set_at`, room links sorted by `(set_at, thread_id, name)`, and cross-room links sorted by `(set_at, room_id, thread_id, name)` (with `resolved` status read from `com.mindroom.thread.tags`)
- **Unlinking:** writes an empty tombstone payload (`{}`) for that state key

</details>
