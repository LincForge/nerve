"""Wireless bench hardware identity: the pure helper (linc-nerve-d7fc).

One physical device reachable over multiple adb transports (an mDNS
``_adb-tls-connect`` name + ``ip:5555``, or USB + ``ip:5555``) is grouped by a
validated ``ro.serialno``. These tests drive ``core/android_identity`` on plain
values only; the ``discover()`` / daemon / ``_core`` wiring is follow-up bead
linc-nerve-u2xa.
"""

from __future__ import annotations

import subprocess  # nosec B404 — fixed argv, no shell
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from structlog.testing import capture_logs

# conftest's autouse Xcode fixture imports ios_discovery, which only resolves its
# ios <-> ios_discovery cycle when ios is imported first (this file alone imports
# nothing else from nerve).
import linc_nerve.core.ios  # noqa: F401
from linc_nerve.core import android_identity
from linc_nerve.core.android_identity import (
    CollapsedTransport,
    Transport,
    alias_view,
    collapse_transports,
    find_transport,
    normalize_hardware_serial,
    read_hardware_serial,
    transport_rank,
    transport_serials,
)

MDNS_WATCH = "adb-FAKESERIAL01-AbCdEf._adb-tls-connect._tcp"
IP_WATCH = "192.0.2.41:5555"
HW_WATCH = "FAKESERIAL01"
USB_PHONE = "R3CFAKE00001"
OTHER = "watch.local:5555"


@dataclass
class _Entry:
    """A stand-in for a discovered device: anything with serial + aliases."""

    serial: str
    aliases: list[str] = field(default_factory=list)
    model: str = "Galaxy Watch7"


def _t(
    serial: str, hw: str = HW_WATCH, model: str = "Galaxy Watch7", emu: bool = False
) -> Transport:
    return Transport(serial=serial, hardware_serial=hw, model=model, is_emulator=emu)


def _pairs(collapsed: list[CollapsedTransport]) -> list[tuple[str, tuple[str, ...]]]:
    return [(c.serial, c.aliases) for c in collapsed]


# --- normalize_hardware_serial ------------------------------------------------


def test_normalize_strips_and_keeps_a_valid_serial() -> None:
    assert normalize_hardware_serial(f"  {HW_WATCH}\n") == HW_WATCH


def test_normalize_length_boundary_is_64() -> None:
    assert android_identity.MAX_HARDWARE_SERIAL_LEN == 64
    assert normalize_hardware_serial("A" * 64) == "A" * 64
    assert normalize_hardware_serial("A" * 65) == ""


@pytest.mark.parametrize(
    "hostile",
    [
        "",
        "  ",
        "unknown",
        "UNKNOWN",
        "0123456789ABCDEF",
        "0123456789abcdef",
        "1234567890ABCDEF",
        "1234567890abcdef",
        "0000000000000000",
        "000000",
        "A\nB",
        "A\x00B",
        "A\x1fB",
        "A\x7fB",
        "X" * 65,
        None,
        12345,
        b"FAKESERIAL01",
    ],
    ids=[
        "empty",
        "whitespace",
        "unknown_lower",
        "unknown_upper",
        "placeholder_upper",
        "placeholder_lower",
        "placeholder_1234_upper",
        "placeholder_1234_lower",
        "all_zeros_16",
        "all_zeros_6",
        "newline_in_middle",
        "nul_byte",
        "ctrl_0x1f",
        "del_0x7f",
        "len_65",
        "none",
        "int",
        "bytes",
    ],
)
def test_contract_hostile_serialno_normalizes_to_empty_and_never_merges(hostile: Any) -> None:
    """[contract] Hostile/unknown ro.serialno readings normalize to '' and two
    transports carrying them stay two separate entries."""
    assert normalize_hardware_serial(hostile) == ""

    collapsed = collapse_transports([_t(MDNS_WATCH, hw=hostile), _t(IP_WATCH, hw=hostile)])

    assert _pairs(collapsed) == [(MDNS_WATCH, ()), (IP_WATCH, ())]
    assert [(c.index, c.hardware_serial) for c in collapsed] == [(0, ""), (1, "")]


def test_normalize_keeps_a_valid_serial_containing_zeros() -> None:
    """Only an ALL-zero reading is a placeholder; zeros inside a real serial are fine."""
    assert normalize_hardware_serial("R3C0000000A") == "R3C0000000A"
    assert normalize_hardware_serial("0000000000000001") == "0000000000000001"


def test_normalize_keeps_a_valid_serial_made_of_one_repeated_letter() -> None:
    """Only the digit '0' is a placeholder filler: an all-letter serial is kept."""
    assert normalize_hardware_serial("XXXXXXXX") == "XXXXXXXX"
    assert normalize_hardware_serial("X0X0") == "X0X0"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "not_str"),
        ("  ", "empty"),
        ("A" * 65, "too_long"),
        ("A\nB", "control_char"),
        ("unknown", "placeholder"),
        ("0000", "placeholder"),
    ],
    ids=["not_str", "empty", "too_long", "control_char", "placeholder", "all_zeros"],
)
def test_contract_rejected_serialno_is_counted_by_reason(raw: Any, reason: str) -> None:
    """[contract] Input boundary: every dropped ro.serialno reading is counted by
    reason; a valid reading is not counted."""
    before = android_identity.rejected_serial_counts()

    assert normalize_hardware_serial(raw) == ""
    assert normalize_hardware_serial(HW_WATCH) == HW_WATCH

    after = android_identity.rejected_serial_counts()
    assert after.get(reason, 0) - before.get(reason, 0) == 1
    assert sum(after.values()) - sum(before.values()) == 1


def test_rejected_serial_counts_is_a_snapshot_copy() -> None:
    snapshot = android_identity.rejected_serial_counts()
    snapshot["empty"] = -1

    assert android_identity.rejected_serial_counts().get("empty", 0) != -1


# --- module import -------------------------------------------------------------


def test_contract_module_imports_cleanly_as_the_first_nerve_import() -> None:
    """[contract] A fresh interpreter can import the pure module first: it has no
    top-level dependency on ``android`` / ``android_reliability`` / ``device``."""
    code = (
        "import sys, linc_nerve.core.android_identity; "
        "print(sorted(m for m in sys.modules if m in ("
        "'linc_nerve.core.android', 'linc_nerve.core.android_reliability', "
        "'linc_nerve.core.device')))"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


# --- read_hardware_serial -----------------------------------------------------


def _recording_timeout(calls: list[tuple[Any, ...]]) -> Any:
    def _ht(
        fn: Any,
        *,
        timeout: int,
        label: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((fn, args, kwargs, timeout, label))
        return fn(*args, **(kwargs or {}))

    return _ht


def test_read_hardware_serial_uses_injected_timeout_with_budget_and_serial_label() -> None:
    """The getprop runs through the injected timeout callable with the 10 s / 12 s
    budget, labelled with the transport serial prefix like its sibling reads."""
    calls: list[tuple[Any, ...]] = []

    def shell(cmd: str, timeout: int = 0) -> str:
        return f"  {HW_WATCH}\n" if cmd == "getprop ro.serialno" else "?"

    result = read_hardware_serial(shell, _recording_timeout(calls), IP_WATCH)

    assert result == HW_WATCH
    assert calls == [
        (shell, ("getprop ro.serialno",), {"timeout": 10}, 12, "discover-serialno-192.0.2.")
    ]


@pytest.mark.parametrize(
    "error",
    [TimeoutError("getprop timed out"), OSError("adb gone"), RuntimeError("closed")],
    ids=["timeout", "oserror", "runtime"],
)
def test_contract_read_hardware_serial_failure_returns_empty_and_logs(error: Exception) -> None:
    """[contract] A failed serialno read never propagates: it reads as unknown ('')
    so the caller keeps every other property it already read."""

    def _boom(fn: Any, **_kw: Any) -> Any:
        raise error

    before = android_identity.rejected_serial_counts().get("read_failed", 0)
    with capture_logs() as logs:
        assert read_hardware_serial(lambda *_a, **_k: HW_WATCH, _boom, MDNS_WATCH) == ""

    failed = [e for e in logs if e["event"] == "android_serialno_read_failed"]
    assert len(failed) == 1
    assert failed[0]["serial"] == MDNS_WATCH
    assert failed[0]["error"] == str(error)
    assert failed[0]["log_level"] == "warning"
    assert android_identity.rejected_serial_counts()["read_failed"] == before + 1


def test_read_hardware_serial_hostile_reading_returns_empty() -> None:
    assert (
        read_hardware_serial(lambda *_a, **_k: "unknown", lambda fn, **kw: "unknown", USB_PHONE)
        == ""
    )


# --- transport_rank -----------------------------------------------------------


def test_transport_rank_orders_usb_ip_mdns_other() -> None:
    assert [transport_rank(s) for s in (HW_WATCH, USB_PHONE, IP_WATCH, MDNS_WATCH, OTHER)] == [
        0,
        0,
        1,
        2,
        3,
    ]
    assert transport_rank("R3C-FAKE_01") == 0
    assert transport_rank("emulator-5554") == 0  # an emulator is never grouped anyway


# --- collapse_transports ------------------------------------------------------


def test_collapse_mdns_and_ip_prefers_ip_and_reports_primary_index() -> None:
    collapsed = collapse_transports([_t(MDNS_WATCH, hw=f"  {HW_WATCH}\n"), _t(IP_WATCH)])

    assert collapsed == [
        CollapsedTransport(
            index=1, serial=IP_WATCH, hardware_serial=HW_WATCH, aliases=(MDNS_WATCH,)
        )
    ]


def test_collapse_usb_beats_every_other_transport_and_aliases_are_sorted() -> None:
    collapsed = collapse_transports([_t(OTHER), _t(MDNS_WATCH), _t(IP_WATCH), _t(USB_PHONE)])

    assert collapsed == [
        CollapsedTransport(
            index=3,
            serial=USB_PHONE,
            hardware_serial=HW_WATCH,
            aliases=tuple(sorted([IP_WATCH, MDNS_WATCH, OTHER])),
        )
    ]


def test_collapse_mdns_beats_other() -> None:
    assert _pairs(collapse_transports([_t(OTHER), _t(MDNS_WATCH)])) == [(MDNS_WATCH, (OTHER,))]


def test_collapse_same_rank_tie_breaks_on_lexical_serial() -> None:
    collapsed = collapse_transports([_t("192.0.2.42:5555"), _t(IP_WATCH)])

    assert _pairs(collapsed) == [(IP_WATCH, ("192.0.2.42:5555",))]
    assert collapsed[0].index == 1


def test_collapse_two_different_valid_serials_stay_separate() -> None:
    collapsed = collapse_transports([_t(MDNS_WATCH, hw=HW_WATCH), _t(IP_WATCH, hw=USB_PHONE)])

    assert [(c.index, c.serial, c.hardware_serial, c.aliases) for c in collapsed] == [
        (0, MDNS_WATCH, HW_WATCH, ()),
        (1, IP_WATCH, USB_PHONE, ()),
    ]


def test_collapse_keeps_input_order_of_first_member() -> None:
    collapsed = collapse_transports(
        [
            _t("R3CSOLO0001", hw="SOLO1"),
            _t(MDNS_WATCH),
            _t("192.0.2.9:5555", hw="SOLO2"),
            _t(IP_WATCH),
        ]
    )

    assert _pairs(collapsed) == [
        ("R3CSOLO0001", ()),
        (IP_WATCH, (MDNS_WATCH,)),
        ("192.0.2.9:5555", ()),
    ]
    assert [c.index for c in collapsed] == [0, 3, 2]


def test_emulators_never_merge_by_flag_or_serial_prefix() -> None:
    collapsed = collapse_transports(
        [
            _t("emulator-5554", hw="EMULATOR35X", model="sdk"),
            _t("emulator-5556", hw="EMULATOR35X", model="sdk"),
            _t("avd-custom-1", hw="EMULATOR35X", model="sdk", emu=True),
            _t("avd-custom-2", hw="EMULATOR35X", model="sdk", emu=True),
        ]
    )

    assert [c.serial for c in collapsed] == [
        "emulator-5554",
        "emulator-5556",
        "avd-custom-1",
        "avd-custom-2",
    ]
    assert {c.hardware_serial for c in collapsed} == {""}
    assert {c.aliases for c in collapsed} == {()}


def test_collapse_empty_input() -> None:
    assert collapse_transports([]) == []


@pytest.mark.parametrize(
    ("transports", "reason"),
    [
        (
            [
                _t("192.0.2.41:5555", hw="ABC123", model="ModelA"),
                _t("192.0.2.42:5555", hw="ABC123", model="ModelB"),
            ],
            "model_mismatch",
        ),
        (
            [
                _t("R3CFAKE00001", hw="CLONE0001", model="Clone"),
                _t("R3CFAKE00002", hw="CLONE0001", model="Clone"),
            ],
            "second_usb_link",
        ),
    ],
    ids=["same_serialno_different_model", "two_usb_same_serialno_same_model"],
)
def test_contract_collapse_refuses_distinct_hardware_sharing_a_serialno(
    transports: list[Transport], reason: str
) -> None:
    """[contract] A shared valid-looking ro.serialno is not proof of one device:
    different models, or two USB links, never merge, and the refusal is logged."""
    with capture_logs() as logs:
        collapsed = collapse_transports(transports)

    assert [(c.index, c.serial, c.aliases) for c in collapsed] == [
        (0, transports[0].serial, ()),
        (1, transports[1].serial, ()),
    ]
    refusals = [e for e in logs if e["event"] == "android_transport_collapse_refused"]
    assert [(e["serial"], e["model"], e["reasons"]) for e in refusals] == [
        (transports[1].serial, transports[1].model, [reason])
    ]


def test_collapse_usb_joins_a_wireless_only_group() -> None:
    with capture_logs() as logs:
        collapsed = collapse_transports([_t(IP_WATCH), _t(USB_PHONE)])

    assert _pairs(collapsed) == [(USB_PHONE, (IP_WATCH,))]
    assert logs == []


def test_collapse_refusal_still_merges_a_matching_transport_into_the_right_group() -> None:
    mdns_b = "adb-ABC123-x._adb-tls-connect._tcp"
    collapsed = collapse_transports(
        [
            _t("192.0.2.41:5555", hw="ABC123", model="ModelA"),
            _t("192.0.2.42:5555", hw="ABC123", model="ModelB"),
            _t(mdns_b, hw="ABC123", model="ModelB"),
        ]
    )

    assert _pairs(collapsed) == [("192.0.2.41:5555", ()), ("192.0.2.42:5555", (mdns_b,))]


def test_collapse_refusal_lists_every_group_reason() -> None:
    with capture_logs() as logs:
        collapse_transports(
            [
                _t("192.0.2.41:5555", hw="ABC123", model="ModelA"),
                _t("R3CFAKE00001", hw="ABC123", model="ModelB"),
                _t("R3CFAKE00002", hw="ABC123", model="ModelB"),
            ]
        )

    refusals = [e for e in logs if e["event"] == "android_transport_collapse_refused"]
    assert [(e["serial"], e["reasons"]) for e in refusals] == [
        ("R3CFAKE00001", ["model_mismatch"]),
        ("R3CFAKE00002", ["model_mismatch", "second_usb_link"]),
    ]
    assert all(e["log_level"] == "warning" for e in refusals)


# --- transport_serials / find_transport / alias_view --------------------------


def test_transport_serials_lists_every_primary_and_alias() -> None:
    entries = [_Entry(IP_WATCH, [MDNS_WATCH]), _Entry(USB_PHONE)]

    assert transport_serials(entries) == [IP_WATCH, MDNS_WATCH, USB_PHONE]
    assert transport_serials([]) == []


def test_find_transport_primary_returns_the_entry_itself() -> None:
    watch = _Entry(IP_WATCH, [MDNS_WATCH])

    assert find_transport([watch], IP_WATCH) is watch


def test_find_transport_alias_returns_a_view_on_the_named_transport() -> None:
    watch = _Entry(IP_WATCH, [MDNS_WATCH])

    view = find_transport(iter([watch]), MDNS_WATCH)

    assert view is not None and view is not watch
    assert (view.serial, view.aliases, view.model) == (MDNS_WATCH, [IP_WATCH], "Galaxy Watch7")
    assert (watch.serial, watch.aliases) == (IP_WATCH, [MDNS_WATCH])


def test_find_transport_prefers_a_primary_match_over_an_alias_match() -> None:
    other = _Entry(MDNS_WATCH)
    watch = _Entry(IP_WATCH, [MDNS_WATCH])

    assert find_transport([watch, other], MDNS_WATCH) is other


def test_find_transport_unknown_serial_and_empty_list_return_none() -> None:
    watch = _Entry(IP_WATCH, [MDNS_WATCH])

    assert find_transport([watch], "192.0.2.99:5555") is None
    assert find_transport([], IP_WATCH) is None


def test_contract_alias_view_is_stable_per_entry_and_bounded() -> None:
    """[contract] The same entry + alias yields the SAME view object (the _core
    stale-cache memo keys on identity), a different entry never reuses it, and
    the view cache never grows past its cap."""
    watch = _Entry(IP_WATCH, [MDNS_WATCH])
    rediscovered = _Entry(IP_WATCH, [MDNS_WATCH])

    first = find_transport([watch], MDNS_WATCH)
    again = find_transport([watch], MDNS_WATCH)
    fresh = find_transport([rediscovered], MDNS_WATCH)

    assert first is again
    assert fresh is not first
    for i in range(android_identity.ALIAS_VIEW_CAP + 5):
        find_transport([_Entry(f"192.0.2.{i % 250}:{5000 + i}", [f"alias-{i}"])], f"alias-{i}")
    assert len(android_identity._ALIAS_VIEWS) == android_identity.ALIAS_VIEW_CAP == 64


def test_alias_view_is_distinct_per_alias_of_one_entry() -> None:
    entry = _Entry(IP_WATCH, ["alias-a", "alias-b"])

    view_a = alias_view(entry, "alias-a")
    view_b = alias_view(entry, "alias-b")

    assert (view_a.serial, view_a.aliases) == ("alias-a", [IP_WATCH, "alias-b"])
    assert (view_b.serial, view_b.aliases) == ("alias-b", [IP_WATCH, "alias-a"])
    assert alias_view(entry, "alias-a") is view_a
    assert alias_view(entry, "alias-b") is view_b


def test_alias_view_cache_evicts_least_recently_used_first() -> None:
    """Overflowing the cap evicts the oldest view; a re-hit refreshes recency."""
    android_identity._ALIAS_VIEWS.clear()
    keep = _Entry(IP_WATCH, ["keep-alias"])
    first = alias_view(keep, "keep-alias")
    drop = _Entry("192.0.2.77:5555", ["drop-alias"])
    alias_view(drop, "drop-alias")
    fillers = [
        _Entry(f"192.0.2.{i}:6000", [f"fill-{i}"])
        for i in range(android_identity.ALIAS_VIEW_CAP - 2)
    ]
    for i, entry in enumerate(fillers):
        alias_view(entry, f"fill-{i}")
    assert alias_view(keep, "keep-alias") is first  # refresh recency

    overflow = _Entry("192.0.2.88:5555", ["overflow-alias"])
    alias_view(overflow, "overflow-alias")

    keys = list(android_identity._ALIAS_VIEWS)
    assert len(keys) == android_identity.ALIAS_VIEW_CAP
    assert (id(drop), "drop-alias") not in keys
    assert keys[-2:] == [(id(keep), "keep-alias"), (id(overflow), "overflow-alias")]
    assert alias_view(keep, "keep-alias") is first


def test_alias_view_rebuilds_when_a_cached_key_belongs_to_another_entry() -> None:
    """An ``id`` collision (same key, different object) never serves the old view."""
    entry = _Entry(IP_WATCH, ["alias-x"])
    impostor = _Entry("192.0.2.66:5555", ["alias-x"])
    android_identity._ALIAS_VIEWS[(id(entry), "alias-x")] = (
        impostor,
        _Entry("alias-x", ["stale"]),
    )

    view = alias_view(entry, "alias-x")

    assert (view.serial, view.aliases) == ("alias-x", [IP_WATCH])
    assert android_identity._ALIAS_VIEWS[(id(entry), "alias-x")] == (entry, view)


def test_contract_alias_view_concurrent_callers_share_one_view() -> None:
    """[contract] Concurrent lookups of one alias (daemon worker + stdio) get one
    view object under ``_alias_views_lock`` and leave the lock released."""
    entry = _Entry(IP_WATCH, ["concurrent-alias"])
    seen: list[Any] = []
    barrier = threading.Barrier(8)

    def _lookup() -> None:
        barrier.wait(timeout=5)
        seen.append(find_transport([entry], "concurrent-alias"))

    threads = [threading.Thread(target=_lookup) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(seen) == 8
    assert len({id(v) for v in seen}) == 1
    assert not android_identity._alias_views_lock.locked()
