# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom pull-requests plugin."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

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
    resolve_accessible_room_id,
    shared_room_ids,
)

from .state import (
    PullRequest,
    ThreadLink,
    clear_thread_link,
    parse_pull_request,
    read_resolved_thread_ids,
    read_room_links,
    read_thread_links,
    write_thread_link,
)

if TYPE_CHECKING:
    from .state import RoomStateQuerier

type _ListScope = Literal["thread", "room", "all"]
_VALID_SCOPES = frozenset({"thread", "room", "all"})
_ROOM_QUERY_CONCURRENCY = 6


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _payload(status: str, **kwargs: object) -> str:
    payload: dict[str, object] = {"status": status, "tool": "pull_requests"}
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _error(action: str, message: str) -> str:
    return _payload("error", action=action, message=message)


def _resolve_read_access(action: str) -> tuple[ToolRuntimeContext, RoomStateQuerier] | str:
    """Resolve the runtime context and room-state querier."""
    context = get_tool_runtime_context()
    if context is None:
        return _error(action, "Pull request tool context is unavailable in this runtime path.")
    querier = context.hooks.room_state_querier
    if querier is None:
        return _error(action, "Pull request room-state access is unavailable in this runtime path.")
    return context, querier


def _resolve_state_access(
    *,
    action: str,
    require_putter: bool,
) -> tuple[ToolRuntimeContext, str, str, HookBindings] | str:
    """Resolve the active thread scope and room-state bindings."""
    access = _resolve_read_access(action)
    if isinstance(access, str):
        return access
    context, _ = access
    if context.resolved_thread_id is None:
        return _error(action, f"An active thread is required to {action} pull requests.")
    if require_putter and context.hooks.room_state_putter is None:
        return _error(action, "Pull request room-state access is unavailable in this runtime path.")
    return context, context.room_id, context.resolved_thread_id, context.hooks


def _parse_or_error(action: str, url: str) -> PullRequest | str:
    pull_request = parse_pull_request(url)
    if pull_request is None:
        return _error(
            action,
            "Expected a GitHub pull request URL such as https://github.com/<owner>/<repo>/pull/<number>.",
        )
    return pull_request


def _link_row(link: ThreadLink, **extra: object) -> dict[str, object]:
    return {
        **extra,
        "url": link.pull_request.url,
        "name": link.pull_request.name,
        "set_by": link.set_by,
        "set_at": link.set_at,
    }


def _resolve_list_scope(
    scope: _ListScope | None,
    room_id: str | None,
    resolved: bool | None,
) -> tuple[_ListScope, str | None] | str:
    if scope is not None and (not isinstance(scope, str) or scope not in _VALID_SCOPES):
        return _error("list", "scope must be 'thread', 'room', or 'all'.")
    if resolved is not None and not isinstance(resolved, bool):
        return _error("list", "resolved must be a boolean.")
    normalized_room_id: str | None = None
    if room_id is not None:
        if not isinstance(room_id, str) or not room_id.strip():
            return _error("list", "room_id must be a non-empty string.")
        normalized_room_id = room_id.strip()

    if scope is not None:
        effective_scope: _ListScope = scope
    elif normalized_room_id == "all":
        effective_scope = "all"
    elif normalized_room_id is not None or resolved is not None:
        effective_scope = "room"
    else:
        effective_scope = "thread"

    if effective_scope == "thread" and (normalized_room_id is not None or resolved is not None):
        return _error("list", "scope='thread' cannot be combined with room_id or resolved.")
    if effective_scope == "all" and normalized_room_id not in (None, "all"):
        return _error("list", "scope='all' cannot be combined with a specific room_id.")
    return effective_scope, normalized_room_id


async def _read_room_rows(
    querier: RoomStateQuerier,
    room_id: str,
    *,
    resolved: bool | None,
) -> list[dict[str, object]] | None:
    links_result, resolved_result = await asyncio.gather(
        read_room_links(querier, room_id),
        read_resolved_thread_ids(querier, room_id),
        return_exceptions=True,
    )
    for result in (links_result, resolved_result):
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
    if not isinstance(links_result, list) or not isinstance(resolved_result, set):
        return None
    rows: list[dict[str, object]] = []
    for thread_id, link in links_result:
        is_resolved = thread_id in resolved_result
        if resolved is None or is_resolved is resolved:
            rows.append(_link_row(link, room_id=room_id, thread_id=thread_id, resolved=is_resolved))
    return rows


async def _list_room_pull_requests(
    context: ToolRuntimeContext,
    querier: RoomStateQuerier,
    *,
    room_id: str | None,
    resolved: bool | None,
) -> str:
    resolved_room_id, room_error = await resolve_accessible_room_id(context, room_id, contact_room_ok=True)
    if room_error is not None or resolved_room_id is None:
        return _error("list", room_error or "Unknown room.")
    rows = await _read_room_rows(querier, resolved_room_id, resolved=resolved)
    if rows is None:
        return _error("list", "Failed to read pull request state for the room.")
    return _payload(
        "ok",
        action="list",
        scope="room",
        room_id=resolved_room_id,
        resolved=resolved,
        count=len(rows),
        pull_requests=rows,
    )


async def _list_all_pull_requests(
    context: ToolRuntimeContext,
    querier: RoomStateQuerier,
    *,
    resolved: bool | None,
) -> str:
    room_ids = await shared_room_ids(context, contact_room_ok=True)
    semaphore = asyncio.Semaphore(_ROOM_QUERY_CONCURRENCY)

    async def read_one(room_id: str) -> list[dict[str, object]] | None:
        async with semaphore:
            return await _read_room_rows(querier, room_id, resolved=resolved)

    results = await asyncio.gather(*(read_one(room_id) for room_id in room_ids))
    rows: list[dict[str, object]] = []
    failed: list[str] = []
    for room_id, room_rows in zip(room_ids, results, strict=True):
        if room_rows is None:
            failed.append(room_id)
        else:
            rows.extend(room_rows)
    rows.sort(key=lambda row: (str(row["set_at"]), str(row["room_id"]), str(row["thread_id"]), str(row["name"])))
    extra: dict[str, object] = {"failed_room_ids": sorted(failed)} if failed else {}
    return _payload(
        "ok",
        action="list",
        scope="all",
        room_id="all",
        resolved=resolved,
        count=len(rows),
        pull_requests=rows,
        **extra,
    )


class PullRequestTools(Toolkit):
    """Toolkit for linking GitHub pull requests to Matrix threads and listing linked PRs by thread, room, or all shared rooms."""

    def __init__(self) -> None:
        super().__init__(
            name="pull_requests",
            instructions=(
                "A pull request you create in this thread with `gh pr create` or the GitHub tool is linked by itself. "
                "Use `link_pull_request` for any other pull request that belongs to this thread, "
                "`unlink_pull_request` when one no longer belongs here, "
                "and `list_pull_requests` to list linked pull requests for the current thread, a room, or all shared rooms."
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

    async def list_pull_requests(
        self,
        scope: Literal["thread", "room", "all"] | None = None,
        room_id: str | None = None,
        resolved: bool | None = None,
    ) -> str:
        """List linked GitHub pull requests for the current thread, a room, or all shared rooms.

        Defaults to the current thread when all arguments are omitted. Passing `room_id="all"` queries
        all shared rooms; passing another `room_id` or `resolved` queries a single room.

        Args:
            scope: Listing scope: `"thread"` (current thread), `"room"` (one room), or `"all"` (all shared rooms).
            room_id: Target room ID, alias, configured room name, or `"all"` (defaults to current room for `scope="room"`).
            resolved: Optional filter for `"room"`/`"all"` scopes (`True` = resolved threads only, `False` = unresolved only).

        """
        access = _resolve_read_access("list")
        if isinstance(access, str):
            return access
        context, querier = access

        resolved_scope = _resolve_list_scope(scope, room_id, resolved)
        if isinstance(resolved_scope, str):
            return resolved_scope
        effective_scope, normalized_room_id = resolved_scope

        if effective_scope == "room":
            return await _list_room_pull_requests(context, querier, room_id=normalized_room_id, resolved=resolved)
        if effective_scope == "all":
            return await _list_all_pull_requests(context, querier, resolved=resolved)

        if context.resolved_thread_id is None:
            return _error("list", "An active thread is required to list pull requests.")
        links = await read_thread_links(querier, context.room_id, context.resolved_thread_id)
        return _payload(
            "ok",
            action="list",
            thread_id=context.resolved_thread_id,
            pull_requests=[_link_row(link) for link in links],
        )


@register_tool_with_metadata(
    name="pull_requests",
    file_access=ToolFileAccess.NONE,
    display_name="Pull Requests",
    description="Link and unlink GitHub pull requests on the current thread, and list linked PRs for a thread, room, or all shared rooms.",
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
