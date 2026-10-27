"""Core action-log tests — pure helpers + the per-serial session registry (linc-nerve-7ea).

No device, no adb: the registry writes to ``tmp_path`` and the clock is the
module-level ``action_log._clock`` seam.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.action_log import (
    REDACTED,
    ActionLogError,
    ActionLogRegistry,
    build_action_entry,
    element_at,
    outcome_of,
    parse_bounds,
    redact_args,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(action_log, "_clock", fake)
    monkeypatch.setattr(
        action_log, "_wallclock", lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
    )
    return fake


@pytest.fixture
def reg() -> Iterator[ActionLogRegistry]:
    registry = ActionLogRegistry()
    yield registry
    registry.reset()  # close any file a test left open


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# parse_bounds / element_at
# ---------------------------------------------------------------------------


class TestParseBounds:
    def test_parses_uiautomator_bounds(self) -> None:
        assert parse_bounds("[10,20][110,220]") == (10, 20, 110, 220)

    def test_garbage_is_none(self) -> None:
        assert parse_bounds("garbage") is None

    def test_empty_and_non_string_are_none(self) -> None:
        assert parse_bounds("") is None
        assert parse_bounds(None) is None  # type: ignore[arg-type]


class TestElementAt:
    _CONTAINER: ClassVar[dict[str, str]] = {
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][1080,2400]",
    }
    _BUTTON: ClassVar[dict[str, str]] = {
        "id": "com.x:id/next",
        "text": "Next",
        "desc": "Go next",
        "class": "android.widget.Button",
        "bounds": "[100,100][300,200]",
    }

    def test_smallest_containing_element_wins(self) -> None:
        target = element_at([self._CONTAINER, self._BUTTON], 150, 150)
        assert target == {
            "resource_id": "com.x:id/next",
            "text": "Next",
            "content_desc": "Go next",
            "class_name": "android.widget.Button",
            "bounds": "[100,100][300,200]",
        }

    def test_order_does_not_matter(self) -> None:
        target = element_at([self._BUTTON, self._CONTAINER], 150, 150)
        assert target is not None and target["text"] == "Next"

    def test_none_when_no_element_contains_point(self) -> None:
        assert element_at([self._BUTTON], 900, 900) is None
        assert element_at([], 1, 1) is None

    def test_skips_unparseable_bounds_and_non_dicts(self) -> None:
        bad = {"text": "bad", "bounds": "nope"}
        target = element_at([bad, "junk", self._CONTAINER], 5, 5)  # type: ignore[list-item]
        assert target is not None and target["class_name"] == "android.widget.FrameLayout"

    def test_walks_nested_children(self) -> None:
        tree = [dict(self._CONTAINER, children=[self._BUTTON])]
        target = element_at(tree, 150, 150)
        assert target is not None and target["text"] == "Next"

    def test_accepts_long_field_names_and_element_nodes(self) -> None:
        from linc_nerve.core.device import ElementNode

        node = ElementNode(
            resource_id="r", text="T", content_desc="d", class_name="c", bounds="[0,0][10,10]"
        )
        assert element_at([node], 5, 5) == {
            "resource_id": "r",
            "text": "T",
            "content_desc": "d",
            "class_name": "c",
            "bounds": "[0,0][10,10]",
        }


# ---------------------------------------------------------------------------
# Pure entry helpers
# ---------------------------------------------------------------------------


class TestRedactArgs:
    def test_drops_device_key(self) -> None:
        assert redact_args({"device": "S", "x": 1, "y": 2}) == {"x": 1, "y": 2}

    def test_secret_text_is_redacted_to_length(self) -> None:
        assert redact_args({"device": "S", "text": "hunter2", "secret": True}) == {
            "text": REDACTED,
            "secret": True,
            "length": 7,
        }

    def test_non_secret_text_kept(self) -> None:
        assert redact_args({"text": "hello", "secret": False}) == {
            "text": "hello",
            "secret": False,
        }


class TestOutcomeOf:
    def test_success_dict(self) -> None:
        assert outcome_of({"success": True}) == (True, None)

    def test_error_dict(self) -> None:
        assert outcome_of({"error": "Tap failed: boom"}) == (False, "Tap failed: boom")

    def test_non_dict_is_ok(self) -> None:
        assert outcome_of([1, 2]) == (True, None)


class TestBuildActionEntry:
    def test_shape(self) -> None:
        entry = build_action_entry(
            "nerve_tap", {"device": "S", "x": 1, "y": 2}, ok=True, error=None, duration_ms=1.23456
        )
        assert entry == {
            "tool": "nerve_tap",
            "args": {"x": 1, "y": 2},
            "duration_ms": 1.235,
            "ok": True,
            "error": None,
        }

    def test_readonly_and_target_flags(self) -> None:
        entry = build_action_entry(
            "nerve_tap",
            {"x": 1, "y": 2},
            ok=True,
            error=None,
            duration_ms=0.0,
            readonly=True,
            target=None,
            with_target=True,
        )
        assert entry["readonly"] is True
        assert "target" in entry and entry["target"] is None

    def test_secret_scrubbed_from_error(self) -> None:
        entry = build_action_entry(
            "nerve_type",
            {"text": "hunter2", "secret": True},
            ok=False,
            error="Type failed: could not send hunter2",
            duration_ms=0.0,
        )
        assert "hunter2" not in json.dumps(entry)
        assert entry["error"] == f"Type failed: could not send {REDACTED}"


# ---------------------------------------------------------------------------
# Registry / sessions
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_header_then_action_with_rec_offset(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "nested" / "a.jsonl"
        info = reg.start("S", path, {"target_package": "com.x"})
        assert info["path"] == str(path)

        clock.now = 102.5
        reg.append("S", {"tool": "nerve_tap", "args": {"x": 1, "y": 2}})
        reg.mark_recording_started("S", 103.0)
        clock.now = 105.0
        reg.append("S", {"tool": "nerve_tap", "args": {"x": 3, "y": 4}})

        lines = _lines(path)
        assert lines[0] == {
            "type": "header",
            "version": 1,
            "serial": "S",
            "started_at": "2026-10-03T12:00:00+00:00",
            "meta": {"target_package": "com.x"},
        }
        first, second = lines[1], lines[2]
        assert first["type"] == "action"
        assert first["seq"] == 1
        assert first["t_rec_s"] is None
        assert first["t_session_s"] == 2.5
        assert second["seq"] == 2
        assert second["t_rec_s"] == 105.0 - 103.0

    def test_explicit_at_timestamp(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {})
        reg.mark_recording_started("S", 100.0)
        clock.now = 110.0
        reg.append("S", {"tool": "nerve_tap"}, at=104.0)
        line = _lines(path)[1]
        assert line["t_session_s"] == 4.0
        assert line["t_rec_s"] == 4.0

    def test_explicit_rec_started_snapshot(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {})
        assert reg.recording_started("S") is None
        reg.mark_recording_started("S", 100.0)
        assert reg.recording_started("S") == 100.0
        clock.now = 104.0
        reg.append("S", {"tool": "a"}, rec_started=None)
        reg.append("S", {"tool": "b"}, rec_started=101.0)
        a, b = _lines(path)[1:]
        assert a["t_rec_s"] is None
        assert b["t_rec_s"] == 3.0

    def test_recording_stopped_clears_offset(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {})
        reg.mark_recording_started("S", 100.0)
        reg.mark_recording_stopped("S")
        reg.append("S", {"tool": "nerve_tap"})
        assert _lines(path)[1]["t_rec_s"] is None

    def test_start_on_active_serial_raises(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        reg.start("S", tmp_path / "a.jsonl", {})
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", tmp_path / "b.jsonl", {})

    def test_stop_without_session_raises(self) -> None:
        with pytest.raises(ActionLogError, match="no action log"):
            ActionLogRegistry().stop("S")

    def test_stop_returns_path_and_entry_count(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {})
        reg.append("S", {"tool": "nerve_tap"})
        reg.append("S", {"tool": "nerve_key"})
        assert reg.stop("S") == {"serial": "S", "path": str(path), "entries": 2}
        assert reg.active("S") is None
        # append after stop is a no-op, not an error
        assert reg.append("S", {"tool": "nerve_tap"}) is None
        assert len(_lines(path)) == 3

    def test_two_serials_write_independently(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        reg.start("A", a, {})
        reg.start("B", b, {})
        reg.append("A", {"tool": "nerve_tap"})
        reg.append("B", {"tool": "nerve_swipe"})
        reg.append("B", {"tool": "nerve_key"})
        assert [line.get("tool") for line in _lines(a)] == [None, "nerve_tap"]
        assert [line.get("tool") for line in _lines(b)] == [None, "nerve_swipe", "nerve_key"]
        assert _lines(b)[2]["seq"] == 2

    def test_append_mode_and_owner_only_permissions(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text('{"type": "previous"}\n', encoding="utf-8")
        reg.start("S", path, {})
        reg.stop("S")
        lines = _lines(path)
        assert lines[0] == {"type": "previous"}
        assert lines[1]["type"] == "header"

        fresh = tmp_path / "fresh.jsonl"
        reg.start("S", fresh, {})
        reg.stop("S")
        assert fresh.stat().st_mode & 0o777 == 0o600

    def test_unserializable_values_are_stringified(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {"p": Path("/x")})
        reg.append("S", {"tool": "t", "args": {"blob": b"\x00"}})
        lines = _lines(path)
        assert lines[0]["meta"] == {"p": "/x"}
        assert lines[1]["args"] == {"blob": str(b"\x00")}

    def test_write_after_close_is_swallowed(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        reg.start("S", tmp_path / "a.jsonl", {})
        session = reg.active("S")
        assert session is not None
        session.close()
        # a racing append on a closed handle must never raise into the tool call
        assert reg.append("S", {"tool": "nerve_tap"}) is None

    def test_concurrent_appends_get_unique_seq(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "a.jsonl"
        reg.start("S", path, {})

        def worker() -> None:
            for _ in range(50):
                reg.append("S", {"tool": "nerve_tap"})

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = [line["seq"] for line in _lines(path)[1:]]
        assert sorted(seqs) == list(range(1, 201))

    def test_reset_closes_everything(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        reg.start("S", tmp_path / "a.jsonl", {})
        reg.mark_recording_started("S", 1.0)
        reg.reset()
        assert reg.active("S") is None
        assert reg.recording_offset("S", 5.0) is None


def test_readonly_tools_are_registered_device_tools() -> None:
    from linc_nerve.mcp import _core
    from linc_nerve.mcp import server as _server  # noqa: F401  (registers all tools)

    for name in action_log.READONLY_TOOLS | action_log.TARGET_TOOLS | action_log.UNLOGGED_TOOLS:
        assert name in _core.TOOL_REGISTRY, name
        assert _core.TOOL_REGISTRY[name]["kind"] == "device", name


# ---------------------------------------------------------------------------
# actionlog_roots and resolve_log_path (linc-nerve-yhjn)
# ---------------------------------------------------------------------------


def test_actionlog_roots_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    from linc_nerve.core import observability

    roots = action_log.actionlog_roots()
    assert roots == [observability.linc_home() / "nerve" / "actionlogs"]
    assert action_log.actionlog_roots({}) == [observability.linc_home() / "nerve" / "actionlogs"]
    assert action_log.actionlog_roots({"NERVE_ACTIONLOG_ROOTS": "  "}) == [
        observability.linc_home() / "nerve" / "actionlogs"
    ]


def test_actionlog_roots_configured(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    environ = {"NERVE_ACTIONLOG_ROOTS": f"{a}::rel/path:{b}:"}
    roots = action_log.actionlog_roots(environ)
    assert roots == [a, b]


def test_resolve_log_path_rejects_nul_and_non_jsonl(tmp_path: Path) -> None:
    roots = [tmp_path / "root"]
    assert "NUL" in action_log.resolve_log_path("a\x00b.jsonl", roots)
    assert ".jsonl" in action_log.resolve_log_path("a.json", roots)
    assert ".jsonl" in action_log.resolve_log_path("a.txt", roots)
    assert ".jsonl" in action_log.resolve_log_path("..", roots)


def test_resolve_log_path_relative_and_containment(tmp_path: Path) -> None:
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    roots = [root_a, root_b]

    # relative lands in first root
    rel = action_log.resolve_log_path("sub/run.jsonl", roots)
    assert isinstance(rel, Path)
    assert rel == root_a / "sub" / "run.jsonl"

    # path under root_b accepted
    under_b = action_log.resolve_log_path(str(root_b / "x.jsonl"), roots)
    assert isinstance(under_b, Path)
    assert under_b == root_b / "x.jsonl"

    # path outside every root refused, names roots
    outside = action_log.resolve_log_path(str(tmp_path / "outside.jsonl"), roots)
    assert isinstance(outside, str)
    assert str(root_a) in outside
    assert str(root_b) in outside

    # root/../escape.jsonl refused
    escape = action_log.resolve_log_path(str(root_a / ".." / "escape.jsonl"), roots)
    assert isinstance(escape, str)


def test_resolve_log_path_symlinked_parent_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link_dir = root / "link_dir"
    link_dir.symlink_to(outside)

    res = action_log.resolve_log_path(str(link_dir / "target.jsonl"), [root])
    assert isinstance(res, str)


def test_actionlog_roots_all_relative_entries_fall_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    roots = action_log.actionlog_roots({"NERVE_ACTIONLOG_ROOTS": "rel:other/dir"})
    assert roots == [tmp_path / "nerve" / "actionlogs"]


def test_actionlog_roots_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roots = action_log.actionlog_roots({"NERVE_ACTIONLOG_ROOTS": "~/logs"})
    assert roots == [tmp_path / "logs"]


def test_resolve_log_path_refusal_messages_are_exact(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert action_log.resolve_log_path("a\x00b.jsonl", [root]) == "path cannot contain NUL byte"
    assert action_log.resolve_log_path("a.json", [root]) == (
        "path must be a .jsonl file, got 'a.json'"
    )
    assert action_log.resolve_log_path("run.jsonl", []) == "no action-log roots configured"
    outside = str(tmp_path / "out.jsonl")
    assert action_log.resolve_log_path(outside, [root]) == (
        f"path must be under an allowed action-log root ({root}), got {outside!r}"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "run.jsonl/",  # suffix of the final component is .jsonl, but the string is a dir path
        ".jsonl",  # a dotfile: the string ends in .jsonl but Path.suffix is ""
        "sub/.jsonl",
        "run.jsonl/..",  # final component is ..
        "run.JSONL",
    ],
)
def test_resolve_log_path_rejects_each_suffix_or_name_shape(tmp_path: Path, bad: str) -> None:
    result = action_log.resolve_log_path(bad, [tmp_path])
    assert result == f"path must be a .jsonl file, got {bad!r}"


def test_resolve_log_path_accepts_the_root_itself_as_parent(tmp_path: Path) -> None:
    assert action_log.resolve_log_path(str(tmp_path / "x.jsonl"), [tmp_path]) == (
        Path(os.path.realpath(tmp_path)) / "x.jsonl"
    )


# ---------------------------------------------------------------------------
# denied_label refusal hint + registry guard re-arm
# ---------------------------------------------------------------------------


class TestRefusalHints:
    def test_denied_label_default_hint(self) -> None:
        res = action_log.refusal(
            "denied_label",
            "checkout",
            "guarded session: input on a 'checkout' label refused",
        )
        assert res["error"] == "guarded session: input on a 'checkout' label refused"
        assert res["guard"] == "denied_label"
        assert res["detail"] == "checkout"
        assert res["validation_error"] is True
        assert res["hint"] == (
            "'checkout' is on the default deny list. If tapping it is intended in "
            "this session, call nerve_actionlog_start again with the same path and "
            "deny_words set to the full list you want (deny_words replaces the "
            "defaults; pass 'delete account' to keep only that)."
        )

    def test_denied_label_custom_list_hint(self) -> None:
        guard = action_log.build_guard("com.example.app", deny_words="launch missiles, nuke")
        assert guard is not None
        res = action_log.label_refusal(guard, [{"text": "Launch missiles", "content_desc": ""}])
        assert res is not None
        assert res["guard"] == "denied_label"
        assert res["detail"] == "launch missiles"
        assert "default deny list" not in res["hint"]
        assert "launch missiles, nuke" in res["hint"]
        assert "nerve_actionlog_start" in res["hint"]

    @pytest.mark.parametrize(
        "reason", ["foreground_not_allowed", "other_package", "target_unreadable"]
    )
    def test_non_denied_label_refusal_has_no_hint(self, reason: str) -> None:
        res = action_log.refusal(reason, "detail", "msg")
        assert "hint" not in res


class TestRegistryRearm:
    def test_rearm_same_path_and_target_swaps_guard_and_writes_event(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "run.jsonl"
        g1 = action_log.build_guard("com.example.app")
        assert g1 is not None
        reg.start("S", path, {"target_package": "com.example.app"}, g1)

        clock.now = 103.25
        g2 = action_log.build_guard(
            "com.example.app", allow_packages="com.login", deny_words="delete account"
        )
        assert g2 is not None
        info = reg.start("S", path, {"target_package": "com.example.app"}, g2)

        assert info["rearmed"] is True
        assert info["path"] == str(path)
        session = reg.active("S")
        assert session is not None
        assert session.guard is g2
        assert session.guard is not g1
        lines = _lines(path)
        assert len(lines) == 2
        assert lines[0]["type"] == "header"
        assert lines[1]["event"] == "guard_updated"
        assert lines[1]["deny_words"] == ["delete account"]
        assert "com.login" in lines[1]["allowed_packages"]
        assert lines[1]["t_session_s"] == 3.25

    def test_rearm_rejects_different_path_or_target_or_mode_flip(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "run.jsonl"
        g1 = action_log.build_guard("com.example.app")
        g_other = action_log.build_guard("com.other.app")
        reg.start("S", path, {"target_package": "com.example.app"}, g1)

        # different path
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", tmp_path / "other.jsonl", {"target_package": "com.example.app"}, g1)
        # different target_package
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", path, {"target_package": "com.other.app"}, g_other)
        # guarded -> unguarded
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", path, {}, None)

        reg.stop("S")
        # unguarded -> guarded
        reg.start("S", path, {}, None)
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", path, {"target_package": "com.example.app"}, g1)
        # unguarded -> unguarded (same path)
        with pytest.raises(ActionLogError, match="already active"):
            reg.start("S", path, {}, None)


# ---------------------------------------------------------------------------
# custom_deny flag, hint source selection, guard_updated line, re-arm result
# ---------------------------------------------------------------------------


def _session_list_hint(word: str, words: tuple[str, ...]) -> str:
    joined = ", ".join(words)
    return (
        f"'{word}' is on the session's deny_words list ({joined!r}). If tapping it is "
        "intended in this session, call nerve_actionlog_start again with the same path "
        "and deny_words set to the full list you want (deny_words replaces the "
        "defaults; pass 'delete account' to keep only that)."
    )


_DEFAULT_LIST_HINT = (
    "'checkout' is on the default deny list. If tapping it is intended in this "
    "session, call nerve_actionlog_start again with the same path and deny_words "
    "set to the full list you want (deny_words replaces the defaults; pass "
    "'delete account' to keep only that)."
)

_CHECKOUT = [{"text": "Checkout", "content_desc": ""}]


class TestCustomDenyFlag:
    def test_build_guard_without_deny_words_is_not_custom(self) -> None:
        guard = action_log.build_guard("com.example.app")
        assert guard is not None
        assert guard.custom_deny is False
        assert guard.deny_words == action_log.DEFAULT_DENY_WORDS

    def test_build_guard_blank_deny_words_is_not_custom(self) -> None:
        guard = action_log.build_guard("com.example.app", deny_words=" , ")
        assert guard is not None
        assert guard.custom_deny is False

    def test_build_guard_with_deny_words_is_custom(self) -> None:
        guard = action_log.build_guard("com.example.app", deny_words="nuke")
        assert guard is not None
        assert guard.custom_deny is True
        assert guard.deny_words == ("nuke",)

    def test_explicit_default_list_is_still_custom(self) -> None:
        words = ",".join(action_log.DEFAULT_DENY_WORDS)
        guard = action_log.build_guard("com.example.app", deny_words=words)
        assert guard is not None
        assert guard.custom_deny is True
        assert guard.deny_words == action_log.DEFAULT_DENY_WORDS


class TestHintSource:
    def test_default_guard_hint_names_default_list(self) -> None:
        guard = action_log.build_guard("com.example.app")
        assert guard is not None
        assert action_log._custom_deny_words(guard) is None
        res = action_log.label_refusal(guard, _CHECKOUT)
        assert res is not None
        assert res["hint"] == _DEFAULT_LIST_HINT

    def test_explicit_default_list_hint_names_session_list(self) -> None:
        # custom_deny=True while deny_words == DEFAULT_DENY_WORDS: the caller set
        # the list, so the hint must name it rather than "the default deny list".
        words = ",".join(action_log.DEFAULT_DENY_WORDS)
        guard = action_log.build_guard("com.example.app", deny_words=words)
        assert guard is not None
        assert action_log._custom_deny_words(guard) == action_log.DEFAULT_DENY_WORDS
        res = action_log.label_refusal(guard, _CHECKOUT)
        assert res is not None
        assert res["hint"] == _session_list_hint("checkout", action_log.DEFAULT_DENY_WORDS)

    def test_non_default_words_without_flag_hint_names_session_list(self) -> None:
        # A GuardConfig built directly (custom_deny left False) with its own words
        # still gets a hint naming those words, not the default list.
        words = ("checkout", "nuke")
        guard = action_log.GuardConfig(
            "com.example.app",
            frozenset({"com.example.app"}),
            words,
            action_log.deny_pattern(words),
        )
        assert guard.custom_deny is False
        assert action_log._custom_deny_words(guard) == words
        res = action_log.label_refusal(guard, _CHECKOUT)
        assert res is not None
        assert res["hint"] == _session_list_hint("checkout", words)

    def test_custom_list_hint_is_exact(self) -> None:
        guard = action_log.build_guard("com.example.app", deny_words="checkout, nuke")
        assert guard is not None
        res = action_log.label_refusal(guard, _CHECKOUT)
        assert res == {
            "error": "guarded session: input on a 'checkout' label refused",
            "guard": "denied_label",
            "detail": "checkout",
            "validation_error": True,
            "hint": _session_list_hint("checkout", ("checkout", "nuke")),
        }


class TestGuardUpdatedLine:
    def test_update_guard_writes_exact_line_with_rounded_t_session(
        self, reg: ActionLogRegistry, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "run.jsonl"
        g1 = action_log.build_guard("com.example.app")
        assert g1 is not None
        reg.start("S", path, {"target_package": "com.example.app"}, g1)
        session = reg.active("S")
        assert session is not None

        g2 = action_log.build_guard(
            "com.example.app", allow_packages="com.login", deny_words="delete account"
        )
        assert g2 is not None
        record = session.update_guard(g2, 101.23456)

        expected = {
            "type": "guard_updated",
            "event": "guard_updated",
            "deny_words": ["delete account"],
            "allowed_packages": sorted(
                {"com.example.app", "com.login"} | action_log.BUILTIN_ALLOWED_PACKAGES
            ),
            "t_session_s": 1.235,
        }
        assert record == expected
        assert session.guard is g2
        lines = _lines(path)
        assert len(lines) == 2
        assert lines[1] == expected


class TestRearmResult:
    def test_rearm_returns_exact_info_and_logs_event(
        self,
        reg: ActionLogRegistry,
        tmp_path: Path,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from structlog.testing import capture_logs

        path = tmp_path / "run.jsonl"
        g1 = action_log.build_guard("com.example.app")
        assert g1 is not None
        first = reg.start("S", path, {"target_package": "com.example.app"}, g1)
        assert first["started_at"] == "2026-10-03T12:00:00+00:00"

        # A later wall clock proves started_at is the session's, not re-stamped.
        monkeypatch.setattr(
            action_log, "_wallclock", lambda: datetime(2026, 10, 4, 9, 30, 0, tzinfo=UTC)
        )
        clock.now = 102.0
        g2 = action_log.build_guard("com.example.app", deny_words="delete account")
        assert g2 is not None
        with capture_logs() as logs:
            info = reg.start("S", path, {"target_package": "com.example.app"}, g2)

        assert info == {
            "serial": "S",
            "path": str(path),
            "started_at": "2026-10-03T12:00:00+00:00",
            "rearmed": True,
            "target_package": "com.example.app",
            "allowed_packages": sorted({"com.example.app"} | action_log.BUILTIN_ALLOWED_PACKAGES),
            "deny_words": ["delete account"],
        }
        rearmed = [e for e in logs if e["event"] == "action_log_rearmed"]
        assert len(rearmed) == 1
        assert rearmed[0]["serial"] == "S"
        assert rearmed[0]["path"] == str(path)
        assert rearmed[0]["log_level"] == "info"
        assert _lines(path)[1]["t_session_s"] == 2.0
