# ruff: noqa: INP001
"""Pull request links of a thread, backed by Matrix room state."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

THREAD_PULL_REQUEST_EVENT_TYPE = "com.mindroom.thread.pull_request"
THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
RESOLVED_THREAD_TAG = "resolved"

type RoomStateQuerier = Callable[[str, str, str | None], Awaitable[dict[str, Any] | None]]
type RoomStatePutter = Callable[[str, str, str, dict[str, Any]], Awaitable[bool]]

_PREFIX = (
    r"https://github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})/pull/(?P<number>[0-9]{1,10})"
)
_WHOLE_URL = re.compile(_PREFIX + r"(?:[/?#]\S*)?")
_URL_IN_TEXT = re.compile(_PREFIX + r"(?![A-Za-z0-9_-])")


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One GitHub pull request, with owner and repository in lower case."""

    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        """Return the canonical pull request URL."""
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def name(self) -> str:
        """Return the short name `<owner>/<repo>#<number>`."""
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True, slots=True)
class ThreadLink:
    """One pull request linked to a thread."""

    pull_request: PullRequest
    set_by: str
    set_at: str


def _from_match(match: re.Match[str]) -> PullRequest | None:
    number = int(match["number"])
    if number < 1:
        return None
    return PullRequest(match["owner"].lower(), match["repo"].lower(), number)


def parse_pull_request(url: object) -> PullRequest | None:
    """Parse a string that is exactly one pull request URL."""
    if not isinstance(url, str):
        return None
    match = _WHOLE_URL.fullmatch(url)
    return _from_match(match) if match else None


def last_pull_request(text: str) -> PullRequest | None:
    """Return the last pull request URL found in a text."""
    found = None
    for match in _URL_IN_TEXT.finditer(text):
        found = _from_match(match) or found
    return found


def link_state_key(thread_id: str, pull_request: PullRequest) -> str:
    """Return the room-state key of one thread and pull request."""
    return json.dumps([thread_id, pull_request.name], separators=(",", ":"))


def _parse_state_key_pair(state_key: object) -> tuple[str, str] | None:
    if not isinstance(state_key, str):
        return None
    try:
        parsed = json.loads(state_key)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, list) or len(parsed) != 2:
        return None
    first, second = parsed
    if not isinstance(first, str) or not isinstance(second, str):
        return None
    thread_id = first.strip()
    if not thread_id:
        return None
    return thread_id, second


def _parse_room_link(state_key: str, content: object) -> tuple[str, ThreadLink] | None:
    pair = _parse_state_key_pair(state_key)
    if pair is None or not isinstance(content, Mapping) or not content:
        return None
    thread_id, _ = pair
    pull_request = parse_pull_request(content.get("url"))
    set_by = content.get("set_by")
    set_at = content.get("set_at")
    if pull_request is None or not isinstance(set_by, str) or not isinstance(set_at, str):
        return None
    if state_key != link_state_key(thread_id, pull_request):
        return None
    return thread_id, ThreadLink(pull_request, set_by, set_at)


async def read_room_links(
    query_room_state: RoomStateQuerier,
    room_id: str,
) -> list[tuple[str, ThreadLink]] | None:
    """Read all valid pull request links of one room, ordered deterministically."""
    states = await query_room_state(room_id, THREAD_PULL_REQUEST_EVENT_TYPE, None)
    if states is None:
        return None
    links = [
        parsed for state_key, content in states.items() if (parsed := _parse_room_link(state_key, content)) is not None
    ]
    return sorted(links, key=lambda item: (item[1].set_at, item[0], item[1].pull_request.name))


async def read_thread_links(
    query_room_state: RoomStateQuerier,
    room_id: str,
    thread_id: str,
) -> list[ThreadLink]:
    """Read the valid pull request links of one thread, in set_at order."""
    room_links = await read_room_links(query_room_state, room_id)
    if room_links is None:
        return []
    return [link for link_thread_id, link in room_links if link_thread_id == thread_id]


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


async def read_resolved_thread_ids(
    query_room_state: RoomStateQuerier,
    room_id: str,
) -> set[str] | None:
    """Read thread IDs tagged as resolved in one room, or None if room state cannot be read."""
    states = await query_room_state(room_id, THREAD_TAGS_EVENT_TYPE, None)
    if states is None:
        return None
    resolved: set[str] = set()
    for state_key, content in states.items():
        if not isinstance(content, Mapping):
            continue
        if not (_is_non_empty_string(content.get("set_by")) and _is_non_empty_string(content.get("set_at"))):
            continue
        pair = _parse_state_key_pair(state_key)
        if pair is not None and pair[1] == RESOLVED_THREAD_TAG:
            resolved.add(pair[0])
    return resolved


async def write_thread_link(
    put_room_state: RoomStatePutter,
    room_id: str,
    thread_id: str,
    link: ThreadLink,
) -> bool:
    """Write one pull request link."""
    return await put_room_state(
        room_id,
        THREAD_PULL_REQUEST_EVENT_TYPE,
        link_state_key(thread_id, link.pull_request),
        {"url": link.pull_request.url, "set_by": link.set_by, "set_at": link.set_at},
    )


async def clear_thread_link(
    put_room_state: RoomStatePutter,
    room_id: str,
    thread_id: str,
    pull_request: PullRequest,
) -> bool:
    """Unlink a pull request by writing a tombstone payload."""
    return await put_room_state(
        room_id,
        THREAD_PULL_REQUEST_EVENT_TYPE,
        link_state_key(thread_id, pull_request),
        {},
    )
