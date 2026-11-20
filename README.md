# Pull Requests

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

Pull request links for the threads of [MindRoom](https://github.com/mindroom-ai/mindroom) agents.

Agents open GitHub pull requests from inside a Matrix thread. This plugin records which pull requests belong to which thread, as Matrix room state, so that a chat client can show them next to the thread. The pull request client plugin of MindRoom Chat ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)) shows these links on thread cards.

## Features

- Stores each link as a Matrix state event under `com.mindroom.thread.pull_request`
- Links a pull request automatically when an agent's tool call creates one
- Exposes thread-scoped tools to link, unlink, and list pull requests by hand
- Allows several pull requests per thread, and one pull request in several threads
- Records `set_by` and `set_at` metadata with every link

## How It Works

1. An agent creates a pull request with `gh pr create` (or its alias `gh pr new`) in its shell tool, or with the `github` toolkit's `create_pull_request`.
2. After the call, the `link-created-pull-request` hook picks the created pull request: for a shell command, the last pull request URL of the output, which is where `gh pr create` prints it, taken from stdout (before any `Stderr:` section) when the command succeeded and from the whole output, where `gh` names the branch's existing pull request, when it failed; for the `github` toolkit, the `html_url` of the created pull request in its JSON result.
3. If the call ran in a thread and the pull request is not linked to it yet, the hook writes one state event for it.
4. Chat clients read the state events of the room and show the links of each thread.

The hook ignores calls that raised or were blocked, calls outside a thread, shell commands that contain neither `gh pr create` nor `gh pr new`, shell output without a pull request URL, and `github` toolkit results without a pull request `html_url`, such as its error payloads. A shell command that fails without raising but prints an existing pull request's URL (as `gh pr create` does when the branch already has one) links that pull request. A failure of the hook never reaches the turn: it logs a warning.

Writing a link needs the right to send that state event in the room; when the agent's account lacks it, MindRoom retries through the router account.

## Agent Tools

The toolkit is named `pull_requests`.

| Tool | Purpose |
|------|---------|
| `link_pull_request(url)` | Link a GitHub pull request, given by its URL, to the current thread |
| `unlink_pull_request(url)` | Remove the link of a pull request from the current thread |
| `list_pull_requests()` | List the pull requests linked to the current thread |

All three tools are thread-scoped only. They work from a thread's first message on, and refuse to operate at room level.

A pull request an agent creates in the thread with `gh pr create` or the `github` toolkit is linked by itself; `link_pull_request` is for any other pull request that belongs to the thread.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `link-created-pull-request` | `tool:after_call` | Link the pull request a shell or `github` tool call created to its thread |

## Storage

- Event type: `com.mindroom.thread.pull_request`
- State key: `["<thread root event ID>","<owner>/<repo>#<number>"]` as compact JSON, with owner and repository in lower case
- Payload fields: `url` (`https://github.com/<owner>/<repo>/pull/<number>`, lower case), `set_by`, `set_at`
- Unlinking writes an empty payload (`{}`) for that state key

## Install

Vendor this plugin with the MindRoom CLI:

```bash
mindroom plugins install pull-request-plugin
```

Then reference it from `config.yaml`:

```yaml
plugins:
  - path: plugins/pull-request-plugin
```

Add `pull_requests` to the tools list of each agent that should link pull requests by hand. The hook works for every agent once the plugin is loaded.

The tools always run in MindRoom's own process and are not offered where there is no room, such as the MCP gateway.

Update to the latest commit later with:

```bash
mindroom plugins update pull-request-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
