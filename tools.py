# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom pull-requests plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agno.tools import Toolkit
from mindroom.plugin_api import (
    HookBindings,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolRuntimeContext,
    ToolStatus,
    get_tool_runtime_context,
    register_tool_with_metadata,
)

from .state import (
    PullRequest,
    ThreadLink,
    clear_thread_link,
    parse_pull_request,
    read_thread_links,
    write_thread_link,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _payload(status: str, **kwargs: object) -> str:
    payload: dict[str, object] = {"status": status, "tool": "pull_requests"}
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _error(action: str, message: str) -> str:
    return _payload("error", action=action, message=message)


def _resolve_state_access(
    *,
    action: str,
    require_putter: bool,
) -> tuple[ToolRuntimeContext, str, str, HookBindings] | str:
    """Resolve the active thread scope and room-state bindings."""
    context = get_tool_runtime_context()
    if context is None:
        return _error(action, "Pull request tool context is unavailable in this runtime path.")
    if context.resolved_thread_id is None:
        return _error(action, f"An active thread is required to {action} pull requests.")
    bindings = context.hooks
    if bindings.room_state_querier is None or (require_putter and bindings.room_state_putter is None):
        return _error(action, "Pull request room-state access is unavailable in this runtime path.")
    return context, context.room_id, context.resolved_thread_id, bindings


def _parse_or_error(action: str, url: str) -> PullRequest | str:
    pull_request = parse_pull_request(url)
    if pull_request is None:
        return _error(action, "Expected a GitHub pull request URL such as https://github.com/<owner>/<repo>/pull/<number>.")
    return pull_request


class PullRequestTools(Toolkit):
    """Toolkit for linking GitHub pull requests to a Matrix thread."""

    def __init__(self) -> None:
        super().__init__(
            name="pull_requests",
            instructions=(
                "A pull request you create in this thread with `gh pr create` or the GitHub tool is linked by itself. "
                "Use `link_pull_request` for any other pull request that belongs to this thread, "
                "and `unlink_pull_request` when one no longer belongs here."
            ),
            tools=[
                self.link_pull_request,
                self.unlink_pull_request,
                self.list_pull_requests,
            ],
        )

    async def link_pull_request(self, url: str) -> str:
        """Link a GitHub pull request, given by its URL, to the current thread."""
        resolved = _resolve_state_access(action="link", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved
        pull_request = _parse_or_error("link", url)
        if isinstance(pull_request, str):
            return pull_request

        links = await read_thread_links(bindings.room_state_querier, room_id, thread_id)
        if any(link.pull_request == pull_request for link in links):
            return _payload("ok", action="link", thread_id=thread_id, changed=False)

        link = ThreadLink(pull_request, context.agent_name or context.requester_id, _now_iso())
        if not await write_thread_link(bindings.room_state_putter, room_id, thread_id, link):
            return _error("link", "Failed to write the pull request state event.")
        return _payload("ok", action="link", thread_id=thread_id, changed=True)

    async def unlink_pull_request(self, url: str) -> str:
        """Unlink a GitHub pull request, given by its URL, from the current thread."""
        resolved = _resolve_state_access(action="unlink", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        _, room_id, thread_id, bindings = resolved
        pull_request = _parse_or_error("unlink", url)
        if isinstance(pull_request, str):
            return pull_request

        links = await read_thread_links(bindings.room_state_querier, room_id, thread_id)
        if not any(link.pull_request == pull_request for link in links):
            return _payload("ok", action="unlink", thread_id=thread_id, changed=False)

        if not await clear_thread_link(bindings.room_state_putter, room_id, thread_id, pull_request):
            return _error("unlink", "Failed to clear the pull request state event.")
        return _payload("ok", action="unlink", thread_id=thread_id, changed=True)

    async def list_pull_requests(self) -> str:
        """List the GitHub pull requests linked to the current thread."""
        resolved = _resolve_state_access(action="list", require_putter=False)
        if isinstance(resolved, str):
            return resolved
        _, room_id, thread_id, bindings = resolved

        links = await read_thread_links(bindings.room_state_querier, room_id, thread_id)
        return _payload(
            "ok",
            action="list",
            thread_id=thread_id,
            pull_requests=[
                {
                    "url": link.pull_request.url,
                    "name": link.pull_request.name,
                    "set_by": link.set_by,
                    "set_at": link.set_at,
                }
                for link in links
            ],
        )


@register_tool_with_metadata(
    name="pull_requests",
    file_access=ToolFileAccess.NONE,
    display_name="Pull Requests",
    description="Link, unlink, and list the GitHub pull requests of the current Matrix thread.",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="LuGitPullRequest",
    icon_color="text-green-600",
    requires_room_context=True,
)
def pull_requests_factory() -> type[PullRequestTools]:
    """Factory function for the pull-requests toolkit."""
    return PullRequestTools
