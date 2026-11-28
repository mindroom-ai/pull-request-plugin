# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom pull-requests plugin."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.plugin_api import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    ToolRuntimeContext,
    ToolStatus,
    get_logger,
    get_tool_runtime_context,
    register_tool_with_metadata,
    resolve_accessible_room_id,
    shared_room_ids,
    tool,
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
    from .state import RoomStatePutter, RoomStateQuerier

_VALID_ACTIONS = frozenset({"link", "unlink", "list"})
_ROOM_QUERY_CONCURRENCY = 6
logger = get_logger(__name__)


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


def _resolve_write_access(
    action: str,
) -> tuple[ToolRuntimeContext, str, str, RoomStateQuerier, RoomStatePutter] | str:
    """Resolve the active thread scope and room-state read/write access."""
    access = _resolve_read_access(action)
    if isinstance(access, str):
        return access
    context, querier = access
    if context.resolved_thread_id is None:
        return _error(action, f"An active thread is required to {action} pull requests.")
    putter = context.hooks.room_state_putter
    if putter is None:
        return _error(action, "Pull request room-state access is unavailable in this runtime path.")
    return context, context.room_id, context.resolved_thread_id, querier, putter


def _parse_or_error(action: str, url: str | None) -> PullRequest | str:
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


def _validate_list_args(room_id: str | None, resolved: bool | None) -> tuple[str | None, bool | None] | str:
    if resolved is not None and not isinstance(resolved, bool):
        return _error("list", "resolved must be a boolean.")
    normalized_room_id: str | None = None
    if room_id is not None:
        if not isinstance(room_id, str) or not room_id.strip():
            return _error("list", "room_id must be a non-empty string.")
        normalized_room_id = room_id.strip()
    return normalized_room_id, resolved


async def _read_room_rows(
    querier: RoomStateQuerier,
    room_id: str,
    *,
    resolved: bool | None,
) -> list[dict[str, object]] | None:
    try:
        links = await read_room_links(querier, room_id)
        if links is None:
            return None
        if not links:
            return []
        resolved_ids = await read_resolved_thread_ids(querier, room_id)
    except Exception:
        logger.warning("pull_requests_room_state_read_failed", room_id=room_id, exc_info=True)
        return None
    if resolved_ids is None:
        return None
    rows: list[dict[str, object]] = []
    for thread_id, link in links:
        is_resolved = thread_id in resolved_ids
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
    try:
        resolved_room_id, room_error = await resolve_accessible_room_id(context, room_id, contact_room_ok=True)
    except Exception:
        logger.warning("pull_requests_room_access_failed", room_id=room_id or context.room_id, exc_info=True)
        return _error("list", "Failed to verify room access.")
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
    try:
        room_ids = await shared_room_ids(context, contact_room_ok=True)
    except Exception:
        logger.warning("pull_requests_shared_rooms_failed", exc_info=True)
        return _error("list", "Failed to resolve shared rooms.")
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


async def _link_pull_request(url: str | None) -> str:
    pull_request = _parse_or_error("link", url)
    if isinstance(pull_request, str):
        return pull_request
    access = _resolve_write_access("link")
    if isinstance(access, str):
        return access
    context, room_id, thread_id, querier, putter = access

    links = await read_thread_links(querier, room_id, thread_id)
    if any(link.pull_request == pull_request for link in links):
        return _payload("ok", action="link", thread_id=thread_id, changed=False)

    link = ThreadLink(pull_request, context.agent_name or context.requester_id, _now_iso())
    if not await write_thread_link(putter, room_id, thread_id, link):
        return _error("link", "Failed to write the pull request state event.")
    return _payload("ok", action="link", thread_id=thread_id, changed=True)


async def _unlink_pull_request(url: str | None) -> str:
    pull_request = _parse_or_error("unlink", url)
    if isinstance(pull_request, str):
        return pull_request
    access = _resolve_write_access("unlink")
    if isinstance(access, str):
        return access
    _, room_id, thread_id, querier, putter = access

    links = await read_thread_links(querier, room_id, thread_id)
    if not any(link.pull_request == pull_request for link in links):
        return _payload("ok", action="unlink", thread_id=thread_id, changed=False)

    if not await clear_thread_link(putter, room_id, thread_id, pull_request):
        return _error("unlink", "Failed to clear the pull request state event.")
    return _payload("ok", action="unlink", thread_id=thread_id, changed=True)


async def _list_pull_requests(*, room_id: str | None, resolved: bool | None) -> str:
    validated = _validate_list_args(room_id, resolved)
    if isinstance(validated, str):
        return validated
    normalized_room_id, validated_resolved = validated

    access = _resolve_read_access("list")
    if isinstance(access, str):
        return access
    context, querier = access

    if normalized_room_id is None and validated_resolved is None:
        if context.resolved_thread_id is None:
            return _error("list", "An active thread is required to list pull requests.")
        links = await read_thread_links(querier, context.room_id, context.resolved_thread_id)
        return _payload(
            "ok",
            action="list",
            thread_id=context.resolved_thread_id,
            pull_requests=[_link_row(link) for link in links],
        )

    if normalized_room_id == "all":
        return await _list_all_pull_requests(context, querier, resolved=validated_resolved)

    target_room_id = None if normalized_room_id in (None, "room") else normalized_room_id
    return await _list_room_pull_requests(context, querier, room_id=target_room_id, resolved=validated_resolved)


class PullRequestTools(Toolkit):
    """Toolkit for linking, unlinking, and listing GitHub pull requests across threads and rooms."""

    offer_background_after = False
    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(
            name="pull_requests",
            instructions=(
                "PRs created via `gh pr create` or the GitHub tool auto-link to this thread. "
                "Use `pull_requests` to link/unlink PRs on this thread or list linked PRs for this thread, "
                "a room, or all shared rooms."
            ),
        )

    @tool
    async def pull_requests(
        self,
        action: Literal["link", "unlink", "list"],
        url: str | None = None,
        room_id: str | None = None,
        resolved: bool | None = None,
    ) -> str:
        """Link, unlink, or list GitHub pull requests.

        Actions:
        - link / unlink: require `url` (applies to the current thread).
        - list: omit `room_id`/`resolved` for the current thread; pass `room_id="room"` (current room), a room ID/alias/name, `room_id="all"` (all shared rooms), and/or `resolved=True|False`.

        Args:
            action: Operation to perform: `"link"`, `"unlink"`, or `"list"`.
            url: GitHub PR URL (required for `"link"` and `"unlink"`).
            room_id: For `"list"`: `"room"` (current room), a room ID/alias/name, or `"all"` (all shared rooms).
            resolved: For `"list"` (room/all): `True` = resolved threads only, `False` = unresolved only.

        """
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            error_action = action if isinstance(action, str) and action.strip() else "unknown"
            return _error(error_action, "action must be 'link', 'unlink', or 'list'.")
        if action == "list":
            if url is not None:
                return _error("list", "url applies only to action='link' and action='unlink'.")
            return await _list_pull_requests(room_id=room_id, resolved=resolved)
        if room_id is not None or resolved is not None:
            return _error(action, "room_id and resolved apply only to action='list'.")
        if action == "link":
            return await _link_pull_request(url)
        return await _unlink_pull_request(url)


@register_tool_with_metadata(
    name="pull_requests",
    file_access=ToolFileAccess.NONE,
    display_name="Pull Requests",
    description="Link, unlink, or list GitHub pull requests for a thread, room, or all shared rooms.",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="LuGitPullRequest",
    icon_color="text-green-600",
    requires_room_context=True,
    action_scoped_grants=("pull_requests",),
    retired_functions={
        "link_pull_request": "pull_requests",
        "unlink_pull_request": "pull_requests",
        "list_pull_requests": "pull_requests",
    },
)
def pull_requests_factory() -> type[PullRequestTools]:
    """Factory function for the pull-requests toolkit."""
    return PullRequestTools
