# ruff: noqa: INP001
"""Hook entrypoint for the MindRoom pull-requests plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.plugin_api import hook

from .state import ThreadLink, last_pull_request, parse_pull_request, read_thread_links, write_thread_link

if TYPE_CHECKING:
    from mindroom.plugin_api import ToolAfterCallContext

    from .state import PullRequest

_SHELL_TOOL = "run_shell_command"
_GITHUB_TOOL = "create_pull_request"
_CREATE_COMMANDS = ("gh pr create", "gh pr new")


def _created_pull_request(ctx: ToolAfterCallContext) -> PullRequest | None:
    """Return the pull request a call created: the github tool's `html_url`, or a shell result's last URL.

    A successful command's stdout, before any `Stderr:` section, is where `gh pr create` prints the URL, so it wins over
    a URL a warning mentions; a failure keeps the whole result, where `gh` names the branch's existing pull request.
    """
    result = str(ctx.result)
    if ctx.tool_name == _GITHUB_TOOL:
        try:
            payload = json.loads(result)
        except ValueError:
            return None
        return parse_pull_request(payload.get("html_url")) if isinstance(payload, dict) else None
    if ctx.tool_name != _SHELL_TOOL:
        return None
    command = ctx.arguments.get("args")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str) or not any(create in command for create in _CREATE_COMMANDS):
        return None
    stdout = "" if result.startswith("Exit code ") else f"\n{result}".split("\nStderr:\n", 1)[0]
    return last_pull_request(stdout) or last_pull_request(result)


@hook(event="tool:after_call", name="link-created-pull-request", timeout_ms=5000)
async def link_created_pull_request(ctx: ToolAfterCallContext) -> None:
    """Link the pull request a tool call created to the thread it ran in."""
    if ctx.error is not None or ctx.blocked or ctx.result is None or ctx.room_id is None or ctx.thread_id is None:
        return
    pull_request = _created_pull_request(ctx)
    if pull_request is None:
        return
    try:
        links = await read_thread_links(ctx.query_room_state, ctx.room_id, ctx.thread_id)
        if any(link.pull_request == pull_request for link in links):
            return
        link = ThreadLink(pull_request, ctx.agent_name, datetime.now(UTC).isoformat())
        if not await write_thread_link(ctx.put_room_state, ctx.room_id, ctx.thread_id, link):
            ctx.logger.warning("Failed to link pull request", pull_request=pull_request.name)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning("Failed to link pull request", error=str(exc))


__all__ = ["link_created_pull_request"]
