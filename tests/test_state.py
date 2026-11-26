# ruff: noqa: INP001
"""Tests for pull-request state helpers."""

from __future__ import annotations

import asyncio
import sys
from importlib import util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
EVENT_TYPE = "com.mindroom.thread.pull_request"
TAGS_EVENT_TYPE = "com.mindroom.thread.tags"


def _load_state_module() -> ModuleType:
    """Load the plugin state module under its synthetic package name."""
    state_path = Path(__file__).resolve().parents[1] / "state.py"
    module_name = f"{PACKAGE_NAME}.state"
    sys.modules.pop(module_name, None)
    spec = util.spec_from_file_location(module_name, state_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Mindroom-AI/Mindroom-Chat/pull/412",
        "https://github.com/Mindroom-AI/Mindroom-Chat/pull/412/files",
        "https://github.com/Mindroom-AI/Mindroom-Chat/pull/412/",
        "https://github.com/Mindroom-AI/Mindroom-Chat/pull/412?w=1",
        "https://github.com/Mindroom-AI/Mindroom-Chat/pull/412#discussion_r1",
    ],
)
def test_parse_pull_request_accepts_pull_request_urls(url: str) -> None:
    """Pull request URLs parse to a lower-cased pull request."""
    module = _load_state_module()
    pull_request = module.parse_pull_request(url)
    assert pull_request is not None
    assert pull_request.name == "mindroom-ai/mindroom-chat#412"
    assert pull_request.url == "https://github.com/mindroom-ai/mindroom-chat/pull/412"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/new/branch",
        "https://github.com/o/r/issues/4",
        "https://gitlab.com/o/r/pull/4",
        "http://github.com/o/r/pull/4",
        "github.com/o/r/pull/4",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pull/4x",
        "https://github.com/o/r/pull/4 trailing words",
        "https://github.com/o/r/pull/4\n",
        " https://github.com/o/r/pull/4",
        "https://github.com/o/r/pull/\u0664",
        "https://github.com/o/r/pull/4/ trailing words",
        "https://github.com/o/r/pull/4?x\nmore",
        f"https://github.com/{'o' * 40}/r/pull/4",
        'https://github.com/o"x/r/pull/4',
        "https://github.com/o%20x/r/pull/4",
        "https://github.com/o/r/pull/12345678901",
        None,
        4,
    ],
)
def test_parse_pull_request_refuses_other_input(url: object) -> None:
    """Anything but one pull request URL gives None."""
    module = _load_state_module()
    assert module.parse_pull_request(url) is None


def test_parse_pull_request_accepts_longest_names() -> None:
    """An owner of 39 characters, a repository of 100 and a number of 10 digits parse."""
    module = _load_state_module()
    pull_request = module.parse_pull_request(f"https://github.com/{'o' * 39}/{'r' * 100}/pull/1234567890")
    assert pull_request is not None
    assert pull_request.number == 1234567890


def test_last_pull_request_returns_last_pull_request_url() -> None:
    """The last real pull request URL in a text wins."""
    module = _load_state_module()
    text = "remote: https://github.com/o/r/pull/new/x\nhttps://github.com/o/r/pull/7\nhttps://github.com/o/r/pull/8"
    found = module.last_pull_request(text)
    assert found is not None
    assert found.number == 8
    assert module.last_pull_request("nothing here") is None
    assert module.last_pull_request('{"a":"https://github.com/foo","b":"x/y/pull/7"}') is None


@pytest.mark.parametrize(
    "text",
    [
        '{"url": "https://github.com/o/r/pull/7", "number": 7}',
        "(https://github.com/o/r/pull/7)",
        "see https://github.com/o/r/pull/7.",
    ],
)
def test_last_pull_request_finds_url_in_json_and_prose(text: str) -> None:
    """A found URL may sit in JSON, parentheses or before punctuation."""
    module = _load_state_module()
    found = module.last_pull_request(text)
    assert found is not None
    assert found.name == "o/r#7"


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/o/r/pull/4x https://github.com/o/r/pull/9",
        "https://github.com/o/r/pull/9 https://github.com/o/r/pull/4x",
    ],
)
def test_last_pull_request_skips_url_with_number_suffix(text: str) -> None:
    """A number followed by a letter is no pull request."""
    module = _load_state_module()
    found = module.last_pull_request(text)
    assert found is not None
    assert found.name == "o/r#9"


def test_link_state_key_is_compact_json() -> None:
    """The state key is a compact JSON pair."""
    module = _load_state_module()
    key = module.link_state_key("$root", module.PullRequest("o", "r", 7))
    assert key == '["$root","o/r#7"]'


def _key(thread_id: str, name: str) -> str:
    return f'["{thread_id}","{name}"]'


def test_read_thread_links_filters_and_orders() -> None:
    """Only valid links of the thread come back, ordered by set_at."""
    module = _load_state_module()
    calls: list[tuple[str, str, str | None]] = []
    states: dict[str, Any] = {
        _key("$t", "o/r#2"): {"url": "https://github.com/o/r/pull/2", "set_by": "@a:x", "set_at": "2026-02"},
        _key("$t", "o/r#1"): {"url": "https://github.com/o/r/pull/1", "set_by": "@b:x", "set_at": "2026-01"},
        _key("$t", "o/r#3"): {},
        "not json": {"url": "https://github.com/o/r/pull/9", "set_by": "@a:x", "set_at": "2026-03"},
        _key("$t", "o/r#4"): {"url": "https://example.com/o/r/pull/4", "set_by": "@a:x", "set_at": "2026-04"},
        _key("$t", "o/r#5"): {"url": "https://github.com/o/r/pull/6", "set_by": "@a:x", "set_at": "2026-05"},
        _key("$other", "o/r#7"): {"url": "https://github.com/o/r/pull/7", "set_by": "@a:x", "set_at": "2026-06"},
    }

    async def query(room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        calls.append((room_id, event_type, state_key))
        return states

    links = asyncio.run(module.read_thread_links(query, "!room", "$t"))
    assert [link.pull_request.name for link in links] == ["o/r#1", "o/r#2"]
    assert links[0].set_by == "@b:x"
    assert links[0].set_at == "2026-01"
    assert calls == [("!room", EVENT_TYPE, None)]


def test_read_thread_links_returns_empty_on_query_failure() -> None:
    """A failed query gives no links."""
    module = _load_state_module()

    async def query(_room_id: str, _event_type: str, _state_key: str | None) -> dict[str, Any] | None:
        return None

    assert asyncio.run(module.read_thread_links(query, "!room", "$t")) == []


def test_write_and_clear_thread_link() -> None:
    """Write puts the link content, clear puts an empty tombstone."""
    module = _load_state_module()
    puts: list[tuple[str, str, str, dict[str, Any]]] = []

    async def put(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        puts.append((room_id, event_type, state_key, content))
        return True

    pull_request = module.PullRequest("o", "r", 7)
    link = module.ThreadLink(pull_request, "@a:x", "2026-01")
    assert asyncio.run(module.write_thread_link(put, "!room", "$t", link)) is True
    assert asyncio.run(module.clear_thread_link(put, "!room", "$t", pull_request)) is True
    key = '["$t","o/r#7"]'
    assert puts == [
        ("!room", EVENT_TYPE, key, {"url": "https://github.com/o/r/pull/7", "set_by": "@a:x", "set_at": "2026-01"}),
        ("!room", EVENT_TYPE, key, {}),
    ]


def test_read_room_links_filters_and_orders_deterministically() -> None:
    """Room links include every valid thread link sorted by (set_at, thread_id, name)."""
    module = _load_state_module()
    calls: list[tuple[str, str, str | None]] = []
    states: dict[str, Any] = {
        _key("$t2", "o/r#1"): {"url": "https://github.com/o/r/pull/1", "set_by": "@a:x", "set_at": "2026-01"},
        _key("$t1", "o/r#2"): {"url": "https://github.com/o/r/pull/2", "set_by": "@a:x", "set_at": "2026-01"},
        _key("$t1", "o/r#1"): {"url": "https://github.com/o/r/pull/1", "set_by": "@a:x", "set_at": "2026-01"},
        _key("$t0", "o/r#9"): {"url": "https://github.com/o/r/pull/9", "set_by": "@a:x", "set_at": "2026-02"},
        _key("$t1", "o/r#3"): {},
        '["$t1", "o/r#4"]': {"url": "https://github.com/o/r/pull/4", "set_by": "@a:x", "set_at": "2026-01"},
        _key("", "o/r#5"): {"url": "https://github.com/o/r/pull/5", "set_by": "@a:x", "set_at": "2026-01"},
        _key("   ", "o/r#6"): {"url": "https://github.com/o/r/pull/6", "set_by": "@a:x", "set_at": "2026-01"},
        _key("$t1", "o/r#7"): {"url": "https://github.com/o/r/pull/8", "set_by": "@a:x", "set_at": "2026-01"},
        "not json": {"url": "https://github.com/o/r/pull/9", "set_by": "@a:x", "set_at": "2026-01"},
    }

    async def query(room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        calls.append((room_id, event_type, state_key))
        return states

    links = asyncio.run(module.read_room_links(query, "!room"))
    assert links is not None
    assert [(thread_id, link.pull_request.name) for thread_id, link in links] == [
        ("$t1", "o/r#1"),
        ("$t1", "o/r#2"),
        ("$t2", "o/r#1"),
        ("$t0", "o/r#9"),
    ]
    assert calls == [("!room", EVENT_TYPE, None)]


def test_read_room_links_distinguishes_failure_from_empty() -> None:
    """A failed room state read returns None, while an empty room returns []."""
    module = _load_state_module()

    async def fail_query(_room_id: str, _event_type: str, _state_key: str | None) -> dict[str, Any] | None:
        return None

    async def empty_query(_room_id: str, _event_type: str, _state_key: str | None) -> dict[str, Any] | None:
        return {}

    assert asyncio.run(module.read_room_links(fail_query, "!room")) is None
    assert asyncio.run(module.read_room_links(empty_query, "!room")) == []


def test_read_resolved_thread_ids_parses_tags_and_ignores_tombstones() -> None:
    """Only non-empty resolved tag mappings are returned; tombstones and malformed keys are ignored."""
    module = _load_state_module()
    calls: list[tuple[str, str, str | None]] = []
    states: dict[str, Any] = {
        '["$t1","resolved"]': {"set_by": "@a:x", "set_at": "2026-01-01T00:00:00Z"},
        '["$t2", "resolved"]': {"note": "done"},
        '["$t3","resolved"]': {},
        '["$t4","blocked"]': {"set_by": "@a:x"},
        '["","resolved"]': {"set_by": "@a:x"},
        '["   ","resolved"]': {"set_by": "@a:x"},
        '["$t5","resolved"]': None,
        '["$t6","resolved","extra"]': {"set_by": "@a:x"},
        '[123,"resolved"]': {"set_by": "@a:x"},
        "$legacy": {"tags": {"resolved": {"set_by": "@a:x"}}},
    }

    async def query(room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        calls.append((room_id, event_type, state_key))
        return states

    assert asyncio.run(module.read_resolved_thread_ids(query, "!room")) == {"$t1", "$t2"}
    assert calls == [("!room", TAGS_EVENT_TYPE, None)]


def test_read_resolved_thread_ids_distinguishes_failure_from_empty() -> None:
    """A failed tag query returns None, while an empty room returns an empty set."""
    module = _load_state_module()

    async def fail_query(_room_id: str, _event_type: str, _state_key: str | None) -> dict[str, Any] | None:
        return None

    async def empty_query(_room_id: str, _event_type: str, _state_key: str | None) -> dict[str, Any] | None:
        return {}

    assert asyncio.run(module.read_resolved_thread_ids(fail_query, "!room")) is None
    assert asyncio.run(module.read_resolved_thread_ids(empty_query, "!room")) == set()
