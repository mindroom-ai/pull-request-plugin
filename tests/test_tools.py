# ruff: noqa: INP001
"""Tests for the pull-requests toolkit."""

from __future__ import annotations

import json
import sys
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mindroom.hooks import HookBindings
from mindroom.message_target import MessageTarget
from mindroom.tool_system.registry import plugin_module_registrations
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
EVENT_TYPE = "com.mindroom.thread.pull_request"
ROOM = "!room:localhost"
THREAD = "$thread-root"


def _load_tools_module() -> ModuleType:
    """Load the plugin tools module under its synthetic package name."""
    tools_path = Path(__file__).resolve().parents[1] / "tools.py"
    module_name = f"{PACKAGE_NAME}.tools_test_{uuid4().hex}"
    sys.modules.pop(module_name, None)
    spec = util.spec_from_file_location(module_name, tools_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    # A plugin module registers its tools while it loads, as the plugin loader runs it.
    with plugin_module_registrations(module_name) as registrations:
        spec.loader.exec_module(module)
    module.registrations = registrations
    return module


def _tool_context(
    *,
    thread_id: str | None = THREAD,
    resolved_thread_id: str | None = THREAD,
    room_state_querier: AsyncMock | None = None,
    room_state_putter: AsyncMock | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget(
            room_id=ROOM,
            source_thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=None,
            session_id="session-1",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=SimpleNamespace(),
        runtime_paths=SimpleNamespace(),
        plugins=SimpleNamespace(),
        conversation_reader=AsyncMock(),
        relations=AsyncMock(),
        hooks=HookBindings(room_state_querier=room_state_querier, room_state_putter=room_state_putter),
    )


class FakeRoomState:
    """In-memory room state with the querier and putter the runtime provides."""

    def __init__(self, *, put_result: bool = True) -> None:
        self.state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.put_result = put_result
        self.querier = AsyncMock(side_effect=self._query)
        self.putter = AsyncMock(side_effect=self._put)

    async def _query(self, room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        if state_key is not None:
            return self.state.get((room_id, event_type, state_key))
        return {key: content for (room, kind, key), content in self.state.items() if (room, kind) == (room_id, event_type)}

    async def _put(self, room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        if self.put_result:
            self.state[(room_id, event_type, state_key)] = dict(content)
        return self.put_result

    def context(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return tool_runtime_context(_tool_context(room_state_querier=self.querier, room_state_putter=self.putter, **kwargs))


def test_registration_requires_room_context() -> None:
    """The tools read room state, so they must always run in the primary runtime."""
    assert _load_tools_module().registrations["pull_requests"].requires_room_context is True


def _key(name: str) -> tuple[str, str, str]:
    return (ROOM, EVENT_TYPE, json.dumps([THREAD, name], separators=(",", ":")))


@pytest.mark.asyncio
async def test_link_writes_canonical_event_and_is_idempotent() -> None:
    """Linking writes the canonical URL once; the same link again changes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        first = json.loads(await tools.link_pull_request("https://github.com/Owner/Repo/pull/7"))
        again = json.loads(await tools.link_pull_request("https://github.com/owner/repo/pull/7"))

    assert first == {
        "status": "ok",
        "tool": "pull_requests",
        "action": "link",
        "thread_id": THREAD,
        "changed": True,
    }
    assert again == {**first, "changed": False}
    content = room.state[_key("owner/repo#7")]
    assert content["url"] == "https://github.com/owner/repo/pull/7"
    assert content["set_by"] == "code"
    assert room.putter.await_count == 1


@pytest.mark.asyncio
async def test_link_rejects_non_pull_request_url() -> None:
    """A URL that is no pull request errors and writes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        result = json.loads(await tools.link_pull_request("https://github.com/owner/repo/issues/7"))

    assert result["status"] == "error"
    assert result["tool"] == "pull_requests"
    assert result["action"] == "link"
    assert room.state == {}
    room.putter.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_tool_errors_outside_a_thread() -> None:
    """Without a thread every tool answers an error and touches no state."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context(thread_id=None, resolved_thread_id=None):
        results = [
            json.loads(await tools.link_pull_request("https://github.com/owner/repo/pull/7")),
            json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/7")),
            json.loads(await tools.list_pull_requests()),
        ]

    assert [result["status"] for result in results] == ["error"] * 3
    assert [result["action"] for result in results] == ["link", "unlink", "list"]
    room.putter.assert_not_awaited()
    room.querier.assert_not_awaited()


@pytest.mark.parametrize(
    ("thread_id", "resolved_thread_id"),
    [
        (None, "$new-root"),
        ("$a", "$b"),
    ],
)
@pytest.mark.asyncio
async def test_tools_use_the_resolved_thread(thread_id: str | None, resolved_thread_id: str) -> None:
    """The resolved thread root is the thread, also for the message that starts a thread."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context(thread_id=thread_id, resolved_thread_id=resolved_thread_id):
        link = json.loads(await tools.link_pull_request("https://github.com/owner/repo/pull/7"))
        listed = json.loads(await tools.list_pull_requests())
        unlink = json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/7"))

    assert [link["status"], listed["status"], unlink["status"]] == ["ok"] * 3
    assert [link["thread_id"], listed["thread_id"], unlink["thread_id"]] == [resolved_thread_id] * 3
    assert [link["changed"], unlink["changed"]] == [True, True]
    assert [item["name"] for item in listed["pull_requests"]] == ["owner/repo#7"]
    key = json.dumps([resolved_thread_id, "owner/repo#7"], separators=(",", ":"))
    assert room.state == {(ROOM, EVENT_TYPE, key): {}}


@pytest.mark.asyncio
async def test_tool_errors_without_runtime_context() -> None:
    """A missing runtime context is an error, not a crash."""
    tools = _load_tools_module().PullRequestTools()

    with tool_runtime_context(None):
        result = json.loads(await tools.list_pull_requests())

    assert result["status"] == "error"
    assert result["tool"] == "pull_requests"


@pytest.mark.asyncio
async def test_unlink_writes_tombstone_only_for_linked_pull_request() -> None:
    """Unlinking a linked pull request writes {}; one not linked writes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        await tools.link_pull_request("https://github.com/owner/repo/pull/7")
        removed = json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/7"))
        removed_again = json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/7"))
        never = json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/8"))
        bad = json.loads(await tools.unlink_pull_request("not a url"))

    assert removed == {
        "status": "ok",
        "tool": "pull_requests",
        "action": "unlink",
        "thread_id": THREAD,
        "changed": True,
    }
    assert room.state[_key("owner/repo#7")] == {}
    assert removed_again["changed"] is False
    assert never["changed"] is False
    assert _key("owner/repo#8") not in room.state
    assert bad["status"] == "error"
    assert room.putter.await_count == 2


@pytest.mark.asyncio
async def test_list_returns_links_in_set_at_order_and_relinked_once() -> None:
    """Listing sorts by set_at and shows a cleared then re-linked pull request once."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()
    for name, set_at in (("b/b#2", "2026-01-02T00:00:00+00:00"), ("a/a#1", "2026-01-01T00:00:00+00:00")):
        owner_repo, number = name.split("#")
        room.state[_key(name)] = {
            "url": f"https://github.com/{owner_repo}/pull/{number}",
            "set_by": "code",
            "set_at": set_at,
        }

    with room.context():
        await tools.link_pull_request("https://github.com/c/c/pull/3")
        await tools.unlink_pull_request("https://github.com/c/c/pull/3")
        await tools.link_pull_request("https://github.com/c/c/pull/3")
        result = json.loads(await tools.list_pull_requests())

    assert result["status"] == "ok"
    assert result["action"] == "list"
    assert result["thread_id"] == THREAD
    assert [item["name"] for item in result["pull_requests"]] == ["a/a#1", "b/b#2", "c/c#3"]
    assert result["pull_requests"][0] == {
        "url": "https://github.com/a/a/pull/1",
        "name": "a/a#1",
        "set_by": "code",
        "set_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_failed_write_gives_error() -> None:
    """A putter that returns False surfaces as an error for link and unlink."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState(put_result=False)
    room.state[_key("owner/repo#9")] = {
        "url": "https://github.com/owner/repo/pull/9",
        "set_by": "code",
        "set_at": "2026-01-01T00:00:00+00:00",
    }

    with room.context():
        link = json.loads(await tools.link_pull_request("https://github.com/owner/repo/pull/7"))
        unlink = json.loads(await tools.unlink_pull_request("https://github.com/owner/repo/pull/9"))

    assert link["status"] == "error"
    assert unlink["status"] == "error"
