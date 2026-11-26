# ruff: noqa: INP001
"""Tests for the pull-requests toolkit."""

from __future__ import annotations

import asyncio
import json
import sys
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments
from mindroom.config.legacy_fields import retired_consolidated_approval_error
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import HookBindings
from mindroom.message_target import MessageTarget
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.registry import ToolRegistry, plugin_module_registrations
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
EVENT_TYPE = "com.mindroom.thread.pull_request"
TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
ROOM = "!room:localhost"
OTHER_ROOM = "!other:localhost"
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
        self.fail_queries: set[tuple[str, str]] = set()
        self.raise_queries: dict[tuple[str, str], Exception] = {}
        self.put_result = put_result
        self.querier = AsyncMock(side_effect=self._query)
        self.putter = AsyncMock(side_effect=self._put)

    def add_link(
        self,
        name: str,
        *,
        room_id: str = ROOM,
        thread_id: str = THREAD,
        set_at: str = "2026-01-01T00:00:00+00:00",
        set_by: str = "code",
    ) -> None:
        """Seed one pull request link state event."""
        owner_repo, number = name.split("#")
        key = json.dumps([thread_id, name], separators=(",", ":"))
        self.state[(room_id, EVENT_TYPE, key)] = {
            "url": f"https://github.com/{owner_repo}/pull/{number}",
            "set_by": set_by,
            "set_at": set_at,
        }

    def set_resolved(
        self,
        thread_id: str,
        *,
        room_id: str = ROOM,
        active: bool = True,
        set_at: str = "2026-01-01T00:00:00+00:00",
    ) -> None:
        """Seed one resolved thread-tag state event or tombstone."""
        key = json.dumps([thread_id, "resolved"], separators=(",", ":"))
        self.state[(room_id, TAGS_EVENT_TYPE, key)] = {"set_by": "@user:localhost", "set_at": set_at} if active else {}

    async def _query(self, room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        if (room_id, event_type) in self.raise_queries:
            raise self.raise_queries[(room_id, event_type)]
        if (room_id, event_type) in self.fail_queries:
            return None
        if state_key is not None:
            return self.state.get((room_id, event_type, state_key))
        return {
            key: content for (room, kind, key), content in self.state.items() if (room, kind) == (room_id, event_type)
        }

    async def _put(self, room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        if self.put_result:
            self.state[(room_id, event_type, state_key)] = dict(content)
        return self.put_result

    def context(self, **kwargs: Any) -> Any:  # noqa: ANN401
        """Enter a runtime context backed by this fake room state."""
        return tool_runtime_context(
            _tool_context(room_state_querier=self.querier, room_state_putter=self.putter, **kwargs),
        )


def test_registration_and_single_tool_exposure() -> None:
    """The toolkit requires room context, declares grant/retirement metadata, and exposes only pull_requests."""
    module = _load_tools_module()
    registration = module.registrations["pull_requests"]
    tools = module.PullRequestTools()

    assert registration.requires_room_context is True
    assert registration.action_scoped_grants == ("pull_requests",)
    assert registration.retired_functions == {
        "link_pull_request": "pull_requests",
        "unlink_pull_request": "pull_requests",
        "list_pull_requests": "pull_requests",
    }
    assert list(tools.functions) == []
    assert list(tools.async_functions) == ["pull_requests"]
    assert [fn.__name__ for fn in tools.tools] == ["pull_requests"]
    assert not hasattr(tools, "link_pull_request")
    assert not hasattr(tools, "unlink_pull_request")
    assert not hasattr(tools, "list_pull_requests")


def test_action_scoped_grants_separate_operations_per_action() -> None:
    """Timed approval grants bind to (pull_requests, action) so approving list leaves unlink and link gated."""
    module = _load_tools_module()
    registry = ToolRegistry(entries=module.registrations)
    config = Config(tool_approval={"scheduled_any_arguments": True})

    list_op = grant_operation(config, "pull_requests", {"action": "list"}, tools=registry)
    unlink_op = grant_operation(config, "pull_requests", {"action": "unlink"}, tools=registry)
    link_op = grant_operation(config, "pull_requests", {"action": "link"}, tools=registry)

    assert list_op is not None
    assert unlink_op is not None
    assert link_op is not None
    assert list_op == grant_operation(config, "pull_requests", {"action": " list "}, tools=registry)
    assert len({list_op.key, unlink_op.key, link_op.key}) == 3
    for bad_args in ({}, {"action": ""}, {"action": "   "}, {"action": 123}):
        assert grant_operation(config, "pull_requests", bad_args, tools=registry) is None
    assert not scheduled_call_offers_any_arguments(
        config,
        "pull_requests",
        {"action": "list"},
        requester_id="@user:localhost",
        approver_id="@user:localhost",
        authored_confirmation=False,
        tools=registry,
    )


def test_retired_functions_fail_closed_in_config_validation(tmp_path: Path) -> None:
    """Legacy approval rules for retired pull-request functions fail closed until pull_requests is explicitly ruled."""
    plugin_root = Path(__file__).resolve().parents[1]
    module = _load_tools_module()
    registration = module.registrations["pull_requests"]
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )

    for legacy_name in ("link_pull_request", "unlink_pull_request", "list_pull_requests"):
        approval_config = Config(
            tool_approval={
                "default": "auto_approve",
                "rules": [{"match": legacy_name, "action": "require_approval"}],
            },
        ).tool_approval
        error = retired_consolidated_approval_error(
            "pull_requests",
            approval_config.rules,
            approval_config.default,
            approval_config.timeout_days,
            renames=registration.retired_functions,
        )
        assert error is not None
        assert legacy_name in error
        assert "pull_requests" in error

        stale_data = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(plugin_root)}],
            "agents": {
                "code": {"display_name": "Code", "tools": ["pull_requests"], "include_default_tools": False},
            },
            "tool_approval": {
                "default": "auto_approve",
                "rules": [{"match": legacy_name, "action": "require_approval"}],
            },
        }
        with pytest.raises(ConfigRuntimeValidationError, match="pull_requests"):
            Config.validate_with_runtime(stale_data, runtime_paths)

    migrated_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "plugins": [{"path": str(plugin_root)}],
        "agents": {
            "code": {"display_name": "Code", "tools": ["pull_requests"], "include_default_tools": False},
        },
        "tool_approval": {
            "default": "auto_approve",
            "rules": [
                {"match": "unlink_pull_request", "action": "require_approval"},
                {"match": "pull_requests", "action": "require_approval"},
            ],
        },
    }
    loaded = Config.validate_with_runtime(migrated_data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == ["unlink_pull_request", "pull_requests"]


def _key(name: str) -> tuple[str, str, str]:
    return (ROOM, EVENT_TYPE, json.dumps([THREAD, name], separators=(",", ":")))


@pytest.mark.asyncio
async def test_link_writes_canonical_event_and_is_idempotent() -> None:
    """Linking writes the canonical URL once; the same link again changes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        first = json.loads(await tools.pull_requests(action="link", url="https://github.com/Owner/Repo/pull/7"))
        again = json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7"))

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


@pytest.mark.parametrize("url", [None, "https://github.com/owner/repo/issues/7", "not a url"])
@pytest.mark.asyncio
async def test_link_rejects_missing_or_non_pull_request_url(url: str | None) -> None:
    """A missing or invalid URL errors and writes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        result = json.loads(await tools.pull_requests(action="link", url=url))

    assert result["status"] == "error"
    assert result["tool"] == "pull_requests"
    assert result["action"] == "link"
    assert room.state == {}
    room.putter.assert_not_awaited()
    room.querier.assert_not_awaited()


@pytest.mark.parametrize("action", ["link", "unlink"])
@pytest.mark.parametrize(
    "extra",
    [
        {"room_id": "room"},
        {"room_id": OTHER_ROOM},
        {"resolved": False},
        {"resolved": True},
        {"room_id": "all", "resolved": False},
    ],
)
@pytest.mark.asyncio
async def test_link_and_unlink_reject_list_only_arguments(action: str, extra: dict[str, Any]) -> None:
    """Passing room_id or resolved is rejected for link and unlink actions."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        result = json.loads(
            await tools.pull_requests(action=action, url="https://github.com/owner/repo/pull/7", **extra),
        )

    assert result["status"] == "error"
    assert result["tool"] == "pull_requests"
    assert result["action"] == action
    assert "room_id and resolved" in result["message"]
    room.putter.assert_not_awaited()
    room.querier.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_scoped_actions_error_outside_a_thread() -> None:
    """Without a thread, link, unlink, and thread-scoped list error and touch no state."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context(thread_id=None, resolved_thread_id=None):
        results = [
            json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7")),
            json.loads(await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/7")),
            json.loads(await tools.pull_requests(action="list")),
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
        link = json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7"))
        listed = json.loads(await tools.pull_requests(action="list"))
        unlink = json.loads(await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/7"))

    assert [link["status"], listed["status"], unlink["status"]] == ["ok"] * 3
    assert [link["thread_id"], listed["thread_id"], unlink["thread_id"]] == [resolved_thread_id] * 3
    assert [link["changed"], unlink["changed"]] == [True, True]
    assert [item["name"] for item in listed["pull_requests"]] == ["owner/repo#7"]
    key = json.dumps([resolved_thread_id, "owner/repo#7"], separators=(",", ":"))
    assert room.state == {(ROOM, EVENT_TYPE, key): {}}


@pytest.mark.asyncio
async def test_tool_errors_without_runtime_context_or_putter() -> None:
    """A missing runtime context or room-state putter returns a structured error."""
    tools = _load_tools_module().PullRequestTools()

    with tool_runtime_context(None):
        list_result = json.loads(await tools.pull_requests(action="list"))
        link_result = json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7"))

    assert list_result["status"] == "error"
    assert list_result["tool"] == "pull_requests"
    assert link_result["status"] == "error"
    assert link_result["action"] == "link"

    room = FakeRoomState()
    read_only_ctx = _tool_context(room_state_querier=room.querier, room_state_putter=None)
    with tool_runtime_context(read_only_ctx):
        no_putter = json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7"))

    assert no_putter["status"] == "error"
    assert no_putter["action"] == "link"
    room.querier.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlink_writes_tombstone_only_for_linked_pull_request() -> None:
    """Unlinking a linked pull request writes {}; one not linked or invalid writes nothing."""
    tools = _load_tools_module().PullRequestTools()
    room = FakeRoomState()

    with room.context():
        await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7")
        removed = json.loads(await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/7"))
        removed_again = json.loads(
            await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/7"),
        )
        never = json.loads(await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/8"))
        bad = json.loads(await tools.pull_requests(action="unlink", url="not a url"))
        missing = json.loads(await tools.pull_requests(action="unlink"))

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
    assert missing["status"] == "error"
    assert missing["action"] == "unlink"
    assert room.putter.await_count == 2


@pytest.mark.asyncio
async def test_list_returns_links_in_set_at_order_and_relinked_once() -> None:
    """Thread listing sorts by set_at and shows a cleared then re-linked pull request once."""
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
        await tools.pull_requests(action="link", url="https://github.com/c/c/pull/3")
        await tools.pull_requests(action="unlink", url="https://github.com/c/c/pull/3")
        await tools.pull_requests(action="link", url="https://github.com/c/c/pull/3")
        result = json.loads(await tools.pull_requests(action="list"))

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
        link = json.loads(await tools.pull_requests(action="link", url="https://github.com/owner/repo/pull/7"))
        unlink = json.loads(await tools.pull_requests(action="unlink", url="https://github.com/owner/repo/pull/9"))

    assert link["status"] == "error"
    assert unlink["status"] == "error"


@pytest.mark.asyncio
async def test_list_room_works_outside_thread_and_filters_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Room listing works without a thread for room_id='room', resolved=True/False, and a named room."""
    module = _load_tools_module()
    resolve_mock = AsyncMock(side_effect=lambda ctx, room_id=None, **_kw: (room_id or ctx.room_id, None))
    monkeypatch.setattr(module, "resolve_accessible_room_id", resolve_mock)
    tools = module.PullRequestTools()
    room = FakeRoomState()
    room.add_link("a/a#1", thread_id="$t1", set_at="2026-01-01T00:00:00+00:00")
    room.add_link("b/b#2", thread_id="$t2", set_at="2026-01-02T00:00:00+00:00")
    room.add_link("c/c#3", room_id=OTHER_ROOM, thread_id="$t9", set_at="2026-01-03T00:00:00+00:00")
    room.set_resolved("$t1", active=True)
    room.set_resolved("$t2", active=False)

    with room.context(thread_id=None, resolved_thread_id=None):
        all_in_room = json.loads(await tools.pull_requests(action="list", room_id=" room "))
        unresolved_only = json.loads(await tools.pull_requests(action="list", resolved=False))
        resolved_only = json.loads(await tools.pull_requests(action="list", resolved=True))
        room_resolved = json.loads(await tools.pull_requests(action="list", room_id="room", resolved=True))
        other_room = json.loads(await tools.pull_requests(action="list", room_id=f" {OTHER_ROOM} "))

    assert all_in_room == {
        "status": "ok",
        "tool": "pull_requests",
        "action": "list",
        "scope": "room",
        "room_id": ROOM,
        "resolved": None,
        "count": 2,
        "pull_requests": [
            {
                "room_id": ROOM,
                "thread_id": "$t1",
                "resolved": True,
                "url": "https://github.com/a/a/pull/1",
                "name": "a/a#1",
                "set_by": "code",
                "set_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "room_id": ROOM,
                "thread_id": "$t2",
                "resolved": False,
                "url": "https://github.com/b/b/pull/2",
                "name": "b/b#2",
                "set_by": "code",
                "set_at": "2026-01-02T00:00:00+00:00",
            },
        ],
    }
    assert unresolved_only["resolved"] is False
    assert [item["name"] for item in unresolved_only["pull_requests"]] == ["b/b#2"]
    assert resolved_only["resolved"] is True
    assert [item["name"] for item in resolved_only["pull_requests"]] == ["a/a#1"]
    assert room_resolved == resolved_only
    assert other_room["room_id"] == OTHER_ROOM
    assert [item["name"] for item in other_room["pull_requests"]] == ["c/c#3"]
    assert all(call.kwargs == {"contact_room_ok": True} for call in resolve_mock.await_args_list)
    assert [call.args[1] for call in resolve_mock.await_args_list] == [None, None, None, None, OTHER_ROOM]


@pytest.mark.asyncio
async def test_list_room_errors_on_auth_denial_and_state_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Room listing surfaces room access denial and any failed/errored state read."""
    module = _load_tools_module()
    denied = AsyncMock(return_value=(None, "Not authorized to access the target room."))
    monkeypatch.setattr(module, "resolve_accessible_room_id", denied)
    tools = module.PullRequestTools()
    room = FakeRoomState()

    with room.context(thread_id=None, resolved_thread_id=None):
        denied_res = json.loads(await tools.pull_requests(action="list", room_id=OTHER_ROOM))

    assert denied_res["status"] == "error"
    assert denied_res["action"] == "list"
    assert "Not authorized" in denied_res["message"]
    room.querier.assert_not_awaited()

    room.add_link("a/a#1")
    monkeypatch.setattr(module, "resolve_accessible_room_id", AsyncMock(return_value=(ROOM, None)))
    with room.context(thread_id=None, resolved_thread_id=None):
        room.fail_queries = {(ROOM, EVENT_TYPE)}
        failed_links = json.loads(await tools.pull_requests(action="list", room_id="room"))
        room.fail_queries = {(ROOM, TAGS_EVENT_TYPE)}
        failed_tags = json.loads(await tools.pull_requests(action="list", room_id="room"))
        room.fail_queries = set()
        room.raise_queries = {(ROOM, EVENT_TYPE): RuntimeError("boom")}
        raised = json.loads(await tools.pull_requests(action="list", room_id="room"))

    assert [failed_links["status"], failed_tags["status"], raised["status"]] == ["error"] * 3


@pytest.mark.asyncio
async def test_list_errors_on_room_access_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport errors while resolving room access return structured JSON errors without querying state."""
    module = _load_tools_module()
    monkeypatch.setattr(module, "resolve_accessible_room_id", AsyncMock(side_effect=TimeoutError("timed out")))
    monkeypatch.setattr(module, "shared_room_ids", AsyncMock(side_effect=TimeoutError("timed out")))
    tools = module.PullRequestTools()
    room = FakeRoomState()

    with room.context(thread_id=None, resolved_thread_id=None):
        room_result = json.loads(await tools.pull_requests(action="list", room_id=OTHER_ROOM))
        all_result = json.loads(await tools.pull_requests(action="list", room_id="all"))

    assert room_result["status"] == "error"
    assert room_result["action"] == "list"
    assert all_result["status"] == "error"
    assert all_result["action"] == "list"
    room.querier.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_skips_thread_tags_query_for_empty_rooms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rooms with zero PR links skip querying com.mindroom.thread.tags."""
    module = _load_tools_module()
    monkeypatch.setattr(module, "resolve_accessible_room_id", AsyncMock(return_value=(ROOM, None)))
    monkeypatch.setattr(module, "shared_room_ids", AsyncMock(return_value=["!r1:localhost", "!empty:localhost"]))
    tools = module.PullRequestTools()
    room = FakeRoomState()
    room.add_link("a/a#1", room_id="!r1:localhost", thread_id="$t1")

    with room.context(thread_id=None, resolved_thread_id=None):
        empty_room = json.loads(await tools.pull_requests(action="list", room_id="room"))
        assert empty_room["status"] == "ok"
        assert empty_room["count"] == 0
        assert empty_room["pull_requests"] == []
        assert [call.args for call in room.querier.await_args_list] == [(ROOM, EVENT_TYPE, None)]

        room.querier.reset_mock()
        all_rooms = json.loads(await tools.pull_requests(action="list", room_id="all"))

    assert all_rooms["status"] == "ok"
    assert [item["name"] for item in all_rooms["pull_requests"]] == ["a/a#1"]
    queried = {(call.args[0], call.args[1]) for call in room.querier.await_args_list}
    assert queried == {
        ("!r1:localhost", EVENT_TYPE),
        ("!r1:localhost", TAGS_EVENT_TYPE),
        ("!empty:localhost", EVENT_TYPE),
    }


@pytest.mark.asyncio
async def test_list_all_aggregates_sorts_filters_and_isolates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-room listing aggregates shared rooms, sorts deterministically, and isolates failed rooms."""
    module = _load_tools_module()
    shared = AsyncMock(return_value=["!r2:localhost", "!r1:localhost", "!r3:localhost"])
    monkeypatch.setattr(module, "shared_room_ids", shared)
    tools = module.PullRequestTools()
    room = FakeRoomState()
    room.add_link("b/b#2", room_id="!r2:localhost", thread_id="$t1", set_at="2026-01-01T00:00:00+00:00")
    room.add_link("a/a#1", room_id="!r1:localhost", thread_id="$t1", set_at="2026-01-01T00:00:00+00:00")
    room.add_link("c/c#3", room_id="!r3:localhost", thread_id="$t2", set_at="2026-01-02T00:00:00+00:00")
    room.set_resolved("$t1", room_id="!r1:localhost", active=True)

    with room.context(thread_id=None, resolved_thread_id=None):
        all_rooms = json.loads(await tools.pull_requests(action="list", room_id=" all "))
        unresolved = json.loads(await tools.pull_requests(action="list", room_id="all", resolved=False))
        resolved = json.loads(await tools.pull_requests(action="list", room_id="all", resolved=True))
        room.fail_queries = {("!r2:localhost", EVENT_TYPE)}
        room.raise_queries = {("!r3:localhost", TAGS_EVENT_TYPE): RuntimeError("timeout")}
        partial = json.loads(await tools.pull_requests(action="list", room_id="all"))

    assert all_rooms["scope"] == "all"
    assert all_rooms["room_id"] == "all"
    assert all_rooms["count"] == 3
    assert "failed_room_ids" not in all_rooms
    assert [(item["room_id"], item["name"]) for item in all_rooms["pull_requests"]] == [
        ("!r1:localhost", "a/a#1"),
        ("!r2:localhost", "b/b#2"),
        ("!r3:localhost", "c/c#3"),
    ]
    assert unresolved["resolved"] is False
    assert [item["name"] for item in unresolved["pull_requests"]] == ["b/b#2", "c/c#3"]
    assert resolved["resolved"] is True
    assert [item["name"] for item in resolved["pull_requests"]] == ["a/a#1"]
    assert partial["status"] == "ok"
    assert partial["count"] == 1
    assert [item["name"] for item in partial["pull_requests"]] == ["a/a#1"]
    assert partial["failed_room_ids"] == ["!r2:localhost", "!r3:localhost"]
    assert all(call.kwargs == {"contact_room_ok": True} for call in shared.await_args_list)


@pytest.mark.asyncio
async def test_list_all_bounds_room_query_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-room listing queries at most 6 rooms concurrently."""
    module = _load_tools_module()
    rooms = [f"!r{i:02d}:localhost" for i in range(10)]
    monkeypatch.setattr(module, "shared_room_ids", AsyncMock(return_value=rooms))
    tools = module.PullRequestTools()

    active_rooms: dict[str, int] = {}
    peak_rooms = 0

    async def slow_query(room_id: str, event_type: str, _state_key: str | None) -> dict[str, Any]:
        nonlocal peak_rooms
        active_rooms[room_id] = active_rooms.get(room_id, 0) + 1
        peak_rooms = max(peak_rooms, len(active_rooms))
        await asyncio.sleep(0.01)
        active_rooms[room_id] -= 1
        if active_rooms[room_id] == 0:
            del active_rooms[room_id]
        if event_type == EVENT_TYPE:
            key = json.dumps(["$t", f"o/r#{int(room_id[2:4]) + 1}"], separators=(",", ":"))
            return {
                key: {
                    "url": f"https://github.com/o/r/pull/{int(room_id[2:4]) + 1}",
                    "set_by": "code",
                    "set_at": "2026-01-01T00:00:00+00:00",
                },
            }
        return {}

    ctx = _tool_context(thread_id=None, resolved_thread_id=None, room_state_querier=AsyncMock(side_effect=slow_query))
    with tool_runtime_context(ctx):
        result = json.loads(await tools.pull_requests(action="list", room_id="all"))

    assert result["status"] == "ok"
    assert result["count"] == 10
    assert peak_rooms <= 6


@pytest.mark.parametrize(
    ("bad_action", "expected_action"),
    [
        ("invalid", "invalid"),
        ("", "unknown"),
        ("   ", "unknown"),
        (None, "unknown"),
        (123, "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_action_returns_error(
    bad_action: Any,  # noqa: ANN401
    expected_action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown or non-string actions return a structured error without querying rooms or state."""
    module = _load_tools_module()
    resolve_mock = AsyncMock()
    shared_mock = AsyncMock()
    monkeypatch.setattr(module, "resolve_accessible_room_id", resolve_mock)
    monkeypatch.setattr(module, "shared_room_ids", shared_mock)
    tools = module.PullRequestTools()
    room = FakeRoomState()

    with room.context():
        result = json.loads(await tools.pull_requests(action=bad_action))

    assert result["status"] == "error"
    assert result["tool"] == "pull_requests"
    assert result["action"] == expected_action
    assert "link" in result["message"]
    resolve_mock.assert_not_awaited()
    shared_mock.assert_not_awaited()
    room.querier.assert_not_awaited()
    room.putter.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "https://github.com/owner/repo/pull/7"},
        {"url": ""},
        {"room_id": ""},
        {"room_id": "   "},
        {"room_id": 123},
        {"resolved": "true"},
        {"resolved": 1},
        {"resolved": 0},
    ],
)
@pytest.mark.asyncio
async def test_list_rejects_invalid_arguments(
    kwargs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing url on list, blank/non-string room_id, or non-bool resolved returns an error without querying state."""
    module = _load_tools_module()
    resolve_mock = AsyncMock()
    shared_mock = AsyncMock()
    monkeypatch.setattr(module, "resolve_accessible_room_id", resolve_mock)
    monkeypatch.setattr(module, "shared_room_ids", shared_mock)
    tools = module.PullRequestTools()
    room = FakeRoomState()

    with room.context():
        result = json.loads(await tools.pull_requests(action="list", **kwargs))

    assert result["status"] == "error"
    assert result["action"] == "list"
    resolve_mock.assert_not_awaited()
    shared_mock.assert_not_awaited()
    room.querier.assert_not_awaited()
