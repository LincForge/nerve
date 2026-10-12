"""Tests for the HTTP /tools/* router."""

import time
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.daemon import http_router
from linc_nerve.daemon.http_router import route_request


@pytest.fixture(autouse=True)
def _reset_devices_cache() -> Any:
    """Reset module-global cache between tests to prevent order-dependent bleed.

    Per council Lattner P0 — `_DEVICES_CACHE` is module-scoped global state;
    without this fixture, ``test_route_devices_caches`` populates the cache
    and ``test_route_devices_cache_expires`` reads stale data.
    """
    http_router._DEVICES_CACHE = None
    yield
    http_router._DEVICES_CACHE = None


@pytest.fixture
def mock_tool_registry() -> dict[str, Any]:
    """Registry with a fake tool for testing."""

    def fake_screenshot(device_id: str = "", annotate: bool = False) -> dict:
        return {"path": "/tmp/shot.png", "device": device_id}

    return {"nerve_screenshot": fake_screenshot}


def test_route_tool_call(mock_tool_registry: dict[str, Any]) -> None:
    """POST /tools/nerve_screenshot routes to the correct function."""
    result = route_request(
        method="POST",
        path="/tools/nerve_screenshot",
        body={"device_id": "ABC123"},
        registry=mock_tool_registry,
    )
    assert result["status"] == 200
    assert result["body"]["result"]["device"] == "ABC123"


def test_route_forwards_nested_dict_body() -> None:
    """A nested-dict body field (e.g. launch ``extras``) round-trips as a kwarg.

    Regression guard for nerve_launch extras (T6 / reactor 2034b544): the
    generic ``registry[tool](**body)`` forward must hand the JSON ``extras``
    object to the tool intact, not flatten or drop it.
    """

    def fake_launch(device: str = "", package: str = "", extras: dict | None = None) -> dict:
        return {"device": device, "package": package, "extras": extras}

    result = route_request(
        method="POST",
        path="/tools/nerve_launch",
        body={
            "device": "WATCH",
            "package": "com.lincmobile.level.watch",
            "extras": {"SKIP_CONSENT": True, "SEED": 7},
        },
        registry={"nerve_launch": fake_launch},
    )
    assert result["status"] == 200
    assert result["body"]["result"]["extras"] == {"SKIP_CONSENT": True, "SEED": 7}


def test_route_unknown_tool(mock_tool_registry: dict[str, Any]) -> None:
    """POST /tools/unknown returns 404."""
    result = route_request(
        method="POST",
        path="/tools/unknown_tool",
        body={},
        registry=mock_tool_registry,
    )
    assert result["status"] == 404


def test_route_health() -> None:
    """GET /health returns a lightweight liveness probe — no enumeration."""
    result = route_request(
        method="GET",
        path="/health",
        body={},
        registry={},
    )
    assert result["status"] == 200
    assert result["body"]["ok"] is True
    assert result["body"]["version"] == "1.0"
    assert "uptime_seconds" in result["body"]


def test_route_health_body_shape_is_exact() -> None:
    """Body has exactly {ok, version, uptime_seconds, git_sha, package_version}.

    Per council Lattner P0 — absence of `devices`, `device_count`, etc. is
    the contract; presence-only assertions don't enforce the break. `git_sha`
    was added (#58) and `package_version` (#79) so a monitor can detect a
    stale-code daemon. `adb` was added (#131 recurrence): the CACHED last
    watchdog reading of the adb transport — never an inline enumeration, which
    is why the no-controller-I/O and 50ms budget tests below still hold."""
    result = route_request(method="GET", path="/health", body={}, registry={})
    assert set(result["body"].keys()) == {
        "ok",
        "version",
        "uptime_seconds",
        "git_sha",
        "package_version",
        "adb",
    }
    assert isinstance(result["body"]["adb"]["lost_after_recovery"], list)


def test_build_health_includes_git_sha() -> None:
    """`_build_health()` returns a `git_sha` field (#58).

    The SHA lets an external monitor detect a daemon serving stale code."""
    body = http_router._build_health()
    assert "git_sha" in body
    assert body["git_sha"] == http_router._SERVED_GIT_SHA


def test_route_health_exposes_git_sha() -> None:
    """GET /health payload includes the served git SHA (#58 AC3)."""
    result = route_request(method="GET", path="/health", body={}, registry={})
    assert result["status"] == 200
    assert "git_sha" in result["body"]
    assert result["body"]["git_sha"] == http_router._SERVED_GIT_SHA


def test_route_health_exposes_installed_package_version() -> None:
    """GET /health payload includes the installed linc_nerve package version (#79 AC3).

    Lets the post-merge kickstart script (and any monitor) assert the daemon
    serves post-merge code, not a stale pre-merge process."""
    import importlib.metadata

    result = route_request(method="GET", path="/health", body={}, registry={})
    assert result["status"] == 200
    assert result["body"]["package_version"] == importlib.metadata.version("linc-nerve")


def test_package_version_is_resolved_once_at_import() -> None:
    """package_version is a module-level constant — /health stays no-I/O (#79)."""
    with patch.object(http_router, "_resolve_package_version", return_value="SHOULD_NOT_APPEAR"):
        body = http_router._build_health()
    assert body["package_version"] == http_router._SERVED_PACKAGE_VERSION
    assert body["package_version"] != "SHOULD_NOT_APPEAR"


def test_resolve_package_version_degrades_to_unknown() -> None:
    """A missing distribution yields 'unknown', never a raised exception."""
    assert http_router._resolve_package_version("definitely-not-a-real-dist-xyz") == "unknown"


def test_served_git_sha_is_resolved_once_at_import() -> None:
    """The SHA is a module-level constant resolved once, NOT per request.

    Patching the resolver after import must NOT change what `/health` serves —
    proving the value is cached at process start (#58 AC2, keeps /health a
    no-I/O sub-50ms probe)."""
    with patch.object(
        http_router, "_resolve_git_sha", return_value="DIFFERENT_SHA_SHOULD_NOT_APPEAR"
    ):
        body = http_router._build_health()
    # The cached constant wins; the resolver is never called per request.
    assert body["git_sha"] == http_router._SERVED_GIT_SHA
    assert body["git_sha"] != "DIFFERENT_SHA_SHOULD_NOT_APPEAR"


def test_resolve_git_sha_returns_a_string() -> None:
    """The resolver returns a non-empty string for a real git repo."""
    sha = http_router._resolve_git_sha()
    assert isinstance(sha, str)
    assert sha  # non-empty (either a real SHA or the "unknown" sentinel)


def test_resolve_git_sha_degrades_to_unknown_on_bad_path() -> None:
    """The resolver yields 'unknown' (not raise) when git metadata is absent (#58 AC2)."""
    sha = http_router._resolve_git_sha(repo_dir="/nonexistent/not-a-git-repo-xyz")
    assert sha == "unknown"


def test_resolve_git_sha_degrades_to_unknown_when_git_subprocess_fails() -> None:
    """Any subprocess failure (git missing, OSError) yields 'unknown', not a raise."""
    with patch.object(
        http_router.subprocess, "check_output", side_effect=OSError("git not found")
    ):
        assert http_router._resolve_git_sha() == "unknown"


def test_route_health_does_no_controller_io() -> None:
    """`_build_health()` must not instantiate Android/iOS controllers.

    Patch the constructors and assert call count is 0 across 100 calls."""
    with (
        patch("linc_nerve.core.android.AndroidController.__init__", return_value=None) as mock_a,
        patch("linc_nerve.core.ios.IOSController.__init__", return_value=None) as mock_i,
    ):
        for _ in range(100):
            route_request(method="GET", path="/health", body={}, registry={})
    assert mock_a.call_count == 0, "/health must not instantiate AndroidController"
    assert mock_i.call_count == 0, "/health must not instantiate IOSController"


def test_route_health_latency_budget() -> None:
    """`/health` p95 latency must be under 50ms wall clock over 10 runs."""
    samples: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        route_request(method="GET", path="/health", body={}, registry={})
        samples.append(time.perf_counter() - t0)
    samples.sort()
    p95 = samples[int(0.95 * len(samples))] if len(samples) > 1 else samples[-1]
    assert p95 < 0.050, f"/health p95 = {p95 * 1000:.1f}ms exceeds 50ms budget"


def test_route_devices_returns_enumeration() -> None:
    """`GET /devices` returns the device enumeration body."""
    from linc_nerve.core.device import DeviceType, NerveDevice, Platform

    real_dev = NerveDevice(
        serial="PIXEL8PRO",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="14",
        state="online",
    )

    with (
        patch("linc_nerve.core.android.AndroidController") as MockA,
        patch("linc_nerve.core.ios.IOSController") as MockI,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        MockA.return_value.discover.return_value = [real_dev]
        MockI.return_value.discover.return_value = []
        result = route_request(method="GET", path="/devices", body={}, registry={})

    assert result["status"] == 200
    body = result["body"]
    assert body["device_count"] == 1
    assert len(body["devices"]) == 1
    assert body["devices"][0]["serial"] == "PIXEL8PRO"
    # The daemon must serialize `state` — crucible's online filter relies on it
    # (improvement d07eef8f). Missing state silently disables the filter.
    assert body["devices"][0]["state"] == "online"
    assert body["platform_counts"]["android"] == 1
    assert body["platform_counts"]["ios"] == 0
    assert body["ttl_seconds"] == http_router._DEVICES_CACHE_TTL
    assert "cache_age_seconds" in body
    # `reservations` and old `version` field removed from /devices shape
    assert "reservations" not in body


def test_route_devices_serves_cached_result_within_ttl() -> None:
    """Two back-to-back calls within TTL: `.discover()` is called only once.

    Folded stampede + cache-hit test (council resolution: event-loop
    serialization makes classical thread-stampede impossible)."""
    with (
        patch("linc_nerve.core.android.AndroidController") as MockA,
        patch("linc_nerve.core.ios.IOSController") as MockI,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        MockA.return_value.discover.return_value = []
        MockI.return_value.discover.return_value = []
        route_request(method="GET", path="/devices", body={}, registry={})
        route_request(method="GET", path="/devices", body={}, registry={})
        route_request(method="GET", path="/devices", body={}, registry={})
    assert MockA.return_value.discover.call_count == 1
    assert MockI.return_value.discover.call_count == 1


def test_route_devices_cache_expires_after_ttl() -> None:
    """After TTL elapses, `.discover()` runs again on the next call."""
    with (
        patch("linc_nerve.core.android.AndroidController") as MockA,
        patch("linc_nerve.core.ios.IOSController") as MockI,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        MockA.return_value.discover.return_value = []
        MockI.return_value.discover.return_value = []
        route_request(method="GET", path="/devices", body={}, registry={})
        # Manually expire the cache (simulate clock moving past TTL)
        ts, body = http_router._DEVICES_CACHE
        http_router._DEVICES_CACHE = (ts - http_router._DEVICES_CACHE_TTL - 1, body)
        route_request(method="GET", path="/devices", body={}, registry={})
    assert MockA.return_value.discover.call_count == 2
    assert MockI.return_value.discover.call_count == 2


def test_route_devices_cache_age_reflects_staleness() -> None:
    """`cache_age_seconds` grows between calls within the same TTL window."""
    with (
        patch("linc_nerve.core.android.AndroidController") as MockA,
        patch("linc_nerve.core.ios.IOSController") as MockI,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        MockA.return_value.discover.return_value = []
        MockI.return_value.discover.return_value = []
        first = route_request(method="GET", path="/devices", body={}, registry={})
        # Backdate cache timestamp to simulate 1s of staleness
        ts, body = http_router._DEVICES_CACHE
        http_router._DEVICES_CACHE = (ts - 1.0, body)
        second = route_request(method="GET", path="/devices", body={}, registry={})
    assert first["body"]["cache_age_seconds"] == 0.0
    assert second["body"]["cache_age_seconds"] >= 1.0


def test_route_devices_unknown_method_404() -> None:
    """`POST /devices` is not a route (only GET)."""
    result = route_request(method="POST", path="/devices", body={}, registry={})
    assert result["status"] == 404


def test_route_devices_cache_hits_even_when_enumeration_exceeds_ttl() -> None:
    """Regression: e2e hotfix for inbox 9abb5f9d. If enumeration itself
    takes longer than ``_DEVICES_CACHE_TTL`` (51 iOS simulators on the
    real fleet takes ~6s), the cache must still hit on subsequent calls.
    The cache timestamp must reflect when storage happened, not when the
    request started — otherwise every call appears as a miss."""
    call_count = [0]

    def counting_discover() -> list:
        call_count[0] += 1
        return []

    # Simulated monotonic clock: call 1 starts at 0, enumeration finishes
    # at 7s (> 5s TTL); call 2 starts at 7.1s. With the bug, call 2 would
    # see cache_age = 7.1 - 0 = 7.1s > TTL → miss. With the fix, the
    # stored timestamp is 7s (end of enumeration) → cache_age = 0.1s → hit.
    clock_values = iter([0.0, 7.0, 7.1, 7.2, 7.3, 7.4])

    with (
        patch("linc_nerve.core.android.AndroidController") as MockA,
        patch("linc_nerve.core.ios.IOSController") as MockI,
        patch("linc_nerve.mcp._core._ensure_initialized"),
        patch.object(http_router.time, "monotonic", side_effect=lambda: next(clock_values, 7.5)),
    ):
        MockA.return_value.discover.side_effect = counting_discover
        MockI.return_value.discover.return_value = []
        route_request(method="GET", path="/devices", body={}, registry={})
        route_request(method="GET", path="/devices", body={}, registry={})

    assert call_count[0] == 1, f"expected 1 enumeration, got {call_count[0]}"


def test_route_tool_error(mock_tool_registry: dict[str, Any]) -> None:
    """Tool that raises returns 500 with error message."""

    def bad_tool(**kwargs: Any) -> None:
        raise RuntimeError("device exploded")

    registry = {"nerve_bad": bad_tool}
    result = route_request(
        method="POST",
        path="/tools/nerve_bad",
        body={},
        registry=registry,
    )
    assert result["status"] == 500
    assert "device exploded" in result["body"]["error"]


def test_route_tool_call_logs_to_jsonl(mock_tool_registry: dict[str, Any], tmp_path: Any) -> None:
    """Successful tool call writes a JSONL record via log_mcp_call."""
    from unittest.mock import patch

    with patch("linc_nerve.daemon.http_router.log_mcp_call") as mock_log:
        result = route_request(
            method="POST",
            path="/tools/nerve_screenshot",
            body={"device_id": "ABC123"},
            registry=mock_tool_registry,
        )
    assert result["status"] == 200
    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[0][0] == "linc-nerve"  # server
    assert call_args[0][1] == "nerve_screenshot"  # tool
    assert call_args[0][4] is True  # success


def test_route_tool_error_logs_failure(mock_tool_registry: dict[str, Any]) -> None:
    """Tool that raises logs success=False to JSONL."""
    from unittest.mock import patch

    def bad_tool(**kwargs: Any) -> None:
        raise RuntimeError("device exploded")

    registry = {"nerve_bad": bad_tool}
    with patch("linc_nerve.daemon.http_router.log_mcp_call") as mock_log:
        result = route_request(
            method="POST",
            path="/tools/nerve_bad",
            body={},
            registry=registry,
        )
    assert result["status"] == 500
    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[0][4] is False  # success=False
    assert "device exploded" in call_args[0][5]  # error message


def test_route_tool_type_error_logs_failure(mock_tool_registry: dict[str, Any]) -> None:
    """Tool called with wrong params logs success=False and returns 400."""
    from unittest.mock import patch

    def strict_tool(required_param: str) -> dict:
        return {"ok": required_param}

    registry = {"nerve_strict": strict_tool}
    with patch("linc_nerve.daemon.http_router.log_mcp_call") as mock_log:
        result = route_request(
            method="POST",
            path="/tools/nerve_strict",
            body={},  # missing required_param → TypeError
            registry=registry,
        )
    assert result["status"] == 400
    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[0][4] is False  # success=False


def test_route_health_does_not_log(mock_tool_registry: dict[str, Any]) -> None:
    """GET /health does NOT call log_mcp_call."""
    from unittest.mock import patch

    with patch("linc_nerve.daemon.http_router.log_mcp_call") as mock_log:
        result = route_request(
            method="GET",
            path="/health",
            body={},
            registry={},
        )
    assert result["status"] == 200
    mock_log.assert_not_called()


def test_route_rejects_oversized_payload() -> None:
    """A POST body over the size budget is rejected 413 before dispatch.

    Adversarial-probe hardening (linc-nerve#128): an attacker can't force a
    tool to even run by sending a huge JSON body.
    """
    calls = []

    def fake_tool(device: str = "", text: str = "") -> dict:
        calls.append(text)
        return {"success": True}

    huge_text = "A" * (http_router._MAX_BODY_BYTES + 1)
    result = route_request(
        method="POST",
        path="/tools/nerve_type",
        body={"device": "D1", "text": huge_text},
        registry={"nerve_type": fake_tool},
    )
    assert result["status"] == 413
    assert calls == [], "oversized payload must be rejected before the tool runs"


def test_route_allows_payload_within_budget() -> None:
    """A body under the size budget dispatches normally (no false positive)."""

    def fake_tool(device: str = "", text: str = "") -> dict:
        return {"success": True, "text": text}

    result = route_request(
        method="POST",
        path="/tools/nerve_type",
        body={"device": "D1", "text": "hello world"},
        registry={"nerve_type": fake_tool},
    )
    assert result["status"] == 200


def test_route_rejects_path_traversal_in_field() -> None:
    """A `../` traversal sequence anywhere in the body is rejected 400 before dispatch."""
    calls = []

    def fake_tool(device: str = "") -> dict:
        calls.append(device)
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_screenshot",
        body={"device": "../../etc/passwd"},
        registry={"nerve_screenshot": fake_tool},
    )
    assert result["status"] == 400
    assert calls == [], "path-traversal payload must be rejected before the tool runs"


def test_route_rejects_path_traversal_in_nested_field() -> None:
    """Traversal sequences nested inside a dict body value are also caught."""

    def fake_tool(device: str = "", extras: dict | None = None) -> dict:
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_launch",
        body={"device": "D1", "extras": {"path": "../../../secrets"}},
        registry={"nerve_launch": fake_tool},
    )
    assert result["status"] == 400


def test_route_rejects_null_byte_in_field() -> None:
    """A null byte in a string field is rejected 400 (classic path-truncation attack)."""

    def fake_tool(device: str = "") -> dict:
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_screenshot",
        body={"device": "valid\x00.png"},
        registry={"nerve_screenshot": fake_tool},
    )
    assert result["status"] == 400


def test_route_normal_path_is_not_flagged_as_traversal() -> None:
    """An ordinary device serial / value does not false-positive."""

    def fake_tool(device: str = "") -> dict:
        return {"success": True, "device": device}

    result = route_request(
        method="POST",
        path="/tools/nerve_screenshot",
        body={"device": "PIXEL8PRO-1234"},
        registry={"nerve_screenshot": fake_tool},
    )
    assert result["status"] == 200


def test_route_allows_relative_looking_text_in_non_path_field() -> None:
    """linc-nerve#128/129 finding 7 regression guard: the traversal guard
    used to blanket-scan EVERY string value anywhere in the body, so a UI
    test typing a literal relative path (`nerve_type(text="../docs/readme")`)
    got wrongly rejected 400. The check must be scoped to path-shaped field
    names, not every string — `text` is not one of them."""

    def fake_tool(device: str = "", text: str = "") -> dict:
        return {"success": True, "device": device, "chars": len(text)}

    result = route_request(
        method="POST",
        path="/tools/nerve_type",
        body={"device": "D1", "text": "../docs/readme"},
        registry={"nerve_type": fake_tool},
    )
    assert result["status"] == 200


def test_route_still_rejects_traversal_in_nested_path_shaped_field() -> None:
    """Scoping-by-name still catches a nested path-shaped field (e.g. the
    existing `extras.path` regression guard) — only non-path-shaped fields
    like `text` got the exemption."""

    def fake_tool(device: str = "", extras: dict | None = None) -> dict:
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_launch",
        body={"device": "D1", "extras": {"path": "../../../secrets"}},
        registry={"nerve_launch": fake_tool},
    )
    assert result["status"] == 400


def test_route_crucible_install_payload_shape_is_not_broken() -> None:
    """linc-crucible's nerve_client.install() posts {'device':, 'path':} with
    an ABSOLUTE build-output path (no literal `../` in practice) — confirm
    that shape still dispatches normally."""

    def fake_install(device: str = "", path: str = "") -> dict:
        return {"success": True, "device": device, "path": path}

    result = route_request(
        method="POST",
        path="/tools/nerve_install",
        body={"device": "PIXEL8PRO", "path": "/tmp/build/outputs/apk/debug/app-debug.apk"},
        registry={"nerve_install": fake_install},
    )
    assert result["status"] == 200


def test_route_rejects_shell_metacharacters_in_package_field() -> None:
    """linc-nerve#128/129 finding 3: a shell-metacharacter payload in the
    `package` field must be rejected 400 before dispatch — AndroidController
    interpolates `package` verbatim into `am force-stop {package}` /
    `am start ...` without validation (core/, out of scope for this PR), so
    the router is the one safe chokepoint left to close for this field."""
    calls = []

    def fake_kill(device: str = "", package: str = "") -> dict:
        calls.append(package)
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_kill",
        body={"device": "D1", "package": "com.x; id > /sdcard/pwn"},
        registry={"nerve_kill": fake_kill},
    )
    assert result["status"] == 400
    assert calls == [], "shell-metacharacter payload must be rejected before the tool runs"


def test_route_rejects_shell_redirect_in_package_field_without_semicolon() -> None:
    """linc-nerve#128/129 round 1 finding 2 (round0-3-PARTIAL): the round-0
    denylist (`;&|`$()\n\r`) missed `>`, `<`, `*`, quotes, and space —
    `com.x > /sdcard/pwn` contains no denylisted character at all and
    reached the tool at 200. The router now uses the allowlist
    `^[A-Za-z][A-Za-z0-9_.]*$`, so ANY value not shaped like a real Android
    package identifier is rejected."""
    calls = []

    def fake_kill(device: str = "", package: str = "") -> dict:
        calls.append(package)
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_kill",
        body={"device": "D1", "package": "com.x > /sdcard/pwn"},
        registry={"nerve_kill": fake_kill},
    )
    assert result["status"] == 400
    assert calls == [], "shell-redirect payload must be rejected before the tool runs"


def test_route_rejects_package_with_leading_dash() -> None:
    """A leading-dash `package` (flag injection into `am force-stop`/`am
    start`) is rejected by the allowlist even though it contains no
    metacharacter at all — the round-0 denylist would have let this through."""

    def fake_kill(device: str = "", package: str = "") -> dict:
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_kill",
        body={"device": "D1", "package": "-n com.evil.package"},
        registry={"nerve_kill": fake_kill},
    )
    assert result["status"] == 400


def test_route_normal_package_name_is_not_flagged_as_shell_metacharacter() -> None:
    """No false positive: an ordinary Android package identifier dispatches fine."""

    def fake_kill(device: str = "", package: str = "") -> dict:
        return {"success": True, "package": package}

    result = route_request(
        method="POST",
        path="/tools/nerve_kill",
        body={"device": "D1", "package": "com.lincmobile.level.watch"},
        registry={"nerve_kill": fake_kill},
    )
    assert result["status"] == 200


def test_route_allows_empty_package_field() -> None:
    """linc-nerve#128/129 round 2 finding 1 (REGRESSION): the round-1
    allowlist rejected `package=""` outright — VERBATIM the payload
    linc-crucible's `NerveClient.navigate()` sends for every BVT navigate
    step (`{"device":..., "package":"", "uri":...}`). `package=""` is a
    supported "no specific package" launch shape (`core/android_apps.py`
    builds `am start -a VIEW -d {uri} {package}` with an empty trailing
    package), so it must dispatch, not 400."""

    def fake_launch(device: str = "", package: str = "", uri: str = "") -> dict:
        return {"success": True, "package": package, "uri": uri}

    result = route_request(
        method="POST",
        path="/tools/nerve_launch",
        body={"device": "D1", "package": "", "uri": "precisionlevel://settings"},
        registry={"nerve_launch": fake_launch},
    )
    assert result["status"] == 200


def test_route_allows_hyphenated_ios_bundle_id_in_package_field() -> None:
    """linc-nerve#128/129 round 2 finding 1 (REGRESSION): the round-1
    allowlist grammar (`^[A-Za-z][A-Za-z0-9_.]*$`) had no hyphen, so a
    hyphenated iOS bundle ID 400'd even though `package` legitimately
    carries iOS bundle IDs too (`app_tools.py:56,101`) and the iOS path
    passes it as ARGV, never a shell (`core/ios_apps_logs.py`)."""

    def fake_kill(device: str = "", package: str = "") -> dict:
        return {"success": True, "package": package}

    result = route_request(
        method="POST",
        path="/tools/nerve_kill",
        body={"device": "D1", "package": "com.my-app.Runner"},
        registry={"nerve_kill": fake_kill},
    )
    assert result["status"] == 200


def test_route_still_rejects_shell_metacharacters_with_widened_grammar() -> None:
    """The round-2 fix widens the allowlist to permit `-`, but must still
    reject real shell metacharacters — confirms the widening didn't
    reopen the round-0/round-1 injection classes."""

    def fake_kill(device: str = "", package: str = "") -> dict:
        return {"success": True}

    for hostile in ("com.x; id > /sdcard/pwn", "com.x > /sdcard/pwn", "-n com.evil.package"):
        result = route_request(
            method="POST",
            path="/tools/nerve_kill",
            body={"device": "D1", "package": hostile},
            registry={"nerve_kill": fake_kill},
        )
        assert result["status"] == 400, f"{hostile!r} should still be rejected"


def test_route_rejects_leading_dash_filter_str() -> None:
    """linc-nerve#128/129 round 1 finding 3: `filter`/`filter_str` were in
    neither guard's param-name set, so a flag-shaped value flowed straight
    through to the tool's subprocess argv unvalidated (`nerve_stream_logs`
    posts `filter_str` as a top-level JSON body key)."""
    calls = []

    def fake_stream_logs(device: str = "", filter_str: str = "") -> dict:
        calls.append(filter_str)
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_stream_logs",
        body={"device": "D1", "filter_str": "-c"},
        registry={"nerve_stream_logs": fake_stream_logs},
    )
    assert result["status"] == 400
    assert calls == [], "flag-shaped filter must be rejected before the tool runs"


def test_route_allows_normal_filter_str() -> None:
    """No false positive: a real bundle-ID-shaped filter dispatches fine."""

    def fake_stream_logs(device: str = "", filter_str: str = "") -> dict:
        return {"success": True, "filter_str": filter_str}

    result = route_request(
        method="POST",
        path="/tools/nerve_stream_logs",
        body={"device": "D1", "filter_str": "com.app.bundle"},
        registry={"nerve_stream_logs": fake_stream_logs},
    )
    assert result["status"] == 200


def test_route_rejects_shell_metacharacter_in_logcat_tag() -> None:
    """linc-nerve#128/129 round 2 finding 2: `nerve_logcat`'s `tag` field
    was in NONE of the guard param sets — it reaches
    `core/android_ui.py`'s `logcat()`, which STRING-INTERPOLATES it into
    `self._shell(serial, f"logcat -d -s {tag}")`, a plain string handed to
    on-device `sh` (worse than the argv-injection case `filter`/
    `filter_str` already guard). Must be rejected before dispatch."""
    calls = []

    def fake_logcat(device: str = "", lines: int = 50, tag: str = "") -> dict:
        calls.append(tag)
        return {"success": True}

    result = route_request(
        method="POST",
        path="/tools/nerve_logcat",
        body={"device": "D1", "tag": "; id > /sdcard/pwn"},
        registry={"nerve_logcat": fake_logcat},
    )
    assert result["status"] == 400
    assert calls == [], "shell-metacharacter tag must be rejected before the tool runs"


def test_route_allows_normal_logcat_tag() -> None:
    """No false positive: an ordinary logcat tag / process-name filter
    dispatches fine, and an absent (default empty) tag is untouched."""

    def fake_logcat(device: str = "", lines: int = 50, tag: str = "") -> dict:
        return {"success": True, "tag": tag}

    result = route_request(
        method="POST",
        path="/tools/nerve_logcat",
        body={"device": "D1", "tag": "MyTag-1"},
        registry={"nerve_logcat": fake_logcat},
    )
    assert result["status"] == 200

    result = route_request(
        method="POST",
        path="/tools/nerve_logcat",
        body={"device": "D1"},
        registry={"nerve_logcat": fake_logcat},
    )
    assert result["status"] == 200


def test_run_pre_dispatch_guards_rejects_leading_dash_filter_key() -> None:
    """The `filter` key (not `filter_str`) is the shape sse_server's
    `_reject_via_stream_guards` uses internally for `/stream` — confirm the
    guard chain rejects that key name too."""
    rejection = http_router._run_pre_dispatch_guards(
        "nerve_logcat_stream", {"device": "D1", "filter": "-c"}
    )
    assert rejection is not None
    assert rejection["status"] == 400


def test_route_tool_value_error_maps_to_400_not_500() -> None:
    """A ValueError from a tool (e.g. unknown device serial) is a client input
    error, not a server crash — maps to 400 like TypeError, not 500.

    Regression guard for linc-nerve#128: `recording_tools.get_device_sync`
    raises exactly `ValueError(f"Device not found: {platform}")` for an
    unknown/hostile device serial; today that bubbles to a 500, masking a
    hostile request as a server crash instead of a rejected bad request.
    """

    def fake_tool(device: str = "") -> dict:
        raise ValueError(f"Device not found: {device}")

    result = route_request(
        method="POST",
        path="/tools/nerve_record_start",
        body={"device": "NONEXISTENT-SERIAL-XYZ"},
        registry={"nerve_record_start": fake_tool},
    )
    assert result["status"] == 400
    assert "Device not found" in result["body"]["error"]


def test_route_tool_key_error_maps_to_400() -> None:
    """A KeyError from a tool (missing/bad key lookup) also maps to 400."""

    def fake_tool(device: str = "", action: str = "") -> dict:
        raise KeyError(action)

    result = route_request(
        method="POST",
        path="/tools/nerve_device_state",
        body={"device": "D1", "action": "bogus"},
        registry={"nerve_device_state": fake_tool},
    )
    assert result["status"] == 400


def test_route_unexpected_exception_still_maps_to_500() -> None:
    """A genuinely unexpected exception (not a client-input error type) stays 500."""

    def fake_tool(device: str = "") -> dict:
        raise RuntimeError("device exploded")

    result = route_request(
        method="POST",
        path="/tools/nerve_bad",
        body={"device": "D1"},
        registry={"nerve_bad": fake_tool},
    )
    assert result["status"] == 500


def test_route_rejects_call_on_device_reserved_by_another_session(tmp_path: Any) -> None:
    """A tool call for a device reserved by a DIFFERENT nerve session is
    rejected 409 before the tool runs — the caller holds no reservation on it.

    Regression guard for linc-nerve#128 (Abject 'pnpm hack' transplant): the
    daemon's individual device tools already call `_core._check_guard` and
    return a soft `{"error": ...}` (still HTTP 200) when blocked — a hostile
    or misbehaving caller sees a "successful" response. The router itself
    must reject the request at the transport level before dispatch.
    """
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    rival = DeviceManager(db_path=db_path)
    rival.reserve("SHARED-DEVICE", purpose="rival session")

    daemon_mgr = DeviceManager(db_path=db_path)
    calls = []

    def fake_tool(device: str = "") -> dict:
        calls.append(device)
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_tap",
            body={"device": "SHARED-DEVICE"},
            registry={"nerve_tap": fake_tool},
        )

    assert result["status"] == 409
    assert calls == [], "tool must not run when the device is reserved by another session"
    rival.release_all()
    daemon_mgr.release_all()


def test_route_allows_call_on_device_reserved_by_same_session(tmp_path: Any) -> None:
    """No false positive: a device reserved by THIS daemon's own session dispatches fine."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    daemon_mgr = DeviceManager(db_path=db_path)
    daemon_mgr.reserve("OWN-DEVICE", purpose="self")

    def fake_tool(device: str = "") -> dict:
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_tap",
            body={"device": "OWN-DEVICE"},
            registry={"nerve_tap": fake_tool},
        )

    assert result["status"] == 200
    daemon_mgr.release_all()


def test_route_rejects_call_on_device_reserved_by_different_owner(tmp_path: Any) -> None:
    """Audit F5: a same-session lease held by "lane-a" refuses a POST that
    names owner "lane-b" — 409 with main's held_by text, tool never runs."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    daemon_mgr = DeviceManager(db_path=tmp_path / "devices.db")
    daemon_mgr.reserve("A", owner="lane-a")
    calls = []

    def fake_tool(device: str = "") -> dict:
        calls.append(device)
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_tap",
            body={"device": "A", "owner": "lane-b"},
            registry={"nerve_tap": fake_tool},
        )

    assert result["status"] == 409
    assert "held by lane-a" in result["body"]["error"]
    assert "another session" not in result["body"]["error"]
    assert calls == [], "tool must not run when the device is held by a different owner"
    daemon_mgr.release_all()


def test_route_allows_call_on_device_reserved_by_same_owner(tmp_path: Any) -> None:
    """The matching owner dispatches 200, and `owner` is popped before #170's
    typed-param coercion and the splat — a tool with no `owner` parameter
    would otherwise 400 with "Invalid parameters"."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    daemon_mgr = DeviceManager(db_path=tmp_path / "devices.db")
    daemon_mgr.reserve("A", owner="lane-a")
    received = []

    def fake_tool(device: str = "", x: int = 0) -> dict:
        received.append({"device": device, "x": x})
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_tap",
            body={"device": "A", "owner": "lane-a", "x": "5"},
            registry={"nerve_tap": fake_tool},
        )

    assert result["status"] == 200, result
    assert received == [{"device": "A", "x": 5}]
    daemon_mgr.release_all()


def test_route_owner_is_popped_even_without_a_device(tmp_path: Any) -> None:
    """A non-exempt call with `owner` but no `device` still never splats
    `owner` into the tool."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    daemon_mgr = DeviceManager(db_path=tmp_path / "devices.db")

    def fake_tool(device: str = "") -> dict:
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_tap",
            body={"owner": "lane-a"},
            registry={"nerve_tap": fake_tool},
        )

    assert result["status"] == 200, result
    daemon_mgr.release_all()


def test_route_exempt_tool_keeps_owner(tmp_path: Any) -> None:
    """Pin: nerve_reserve takes `owner` itself, so the router forwards it."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    daemon_mgr = DeviceManager(db_path=tmp_path / "devices.db")
    received = []

    def fake_reserve(device: str = "", owner: str = "") -> dict:
        received.append(owner)
        return {"success": True}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_reserve",
            body={"device": "A", "owner": "lane-a"},
            registry={"nerve_reserve": fake_reserve},
        )

    assert result["status"] == 200
    assert received == ["lane-a"]
    daemon_mgr.release_all()


def test_route_reservation_exempt_tools_bypass_the_guard(tmp_path: Any) -> None:
    """Reservation-management tools (reserve/release/pool/discover) must
    remain callable even when the device is reserved by another session —
    they're the mechanism for inspecting/negotiating that exact state."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    rival = DeviceManager(db_path=db_path)
    rival.reserve("SHARED-DEVICE", purpose="rival session")

    daemon_mgr = DeviceManager(db_path=db_path)

    def fake_reserve(device: str = "") -> dict:
        return {"error": f"Device '{device}' is reserved by session ..."}

    with patch.object(_core, "_device_mgr", daemon_mgr):
        result = route_request(
            method="POST",
            path="/tools/nerve_reserve",
            body={"device": "SHARED-DEVICE"},
            registry={"nerve_reserve": fake_reserve},
        )

    # Dispatched normally (200 + the tool's own soft error body) — NOT
    # short-circuited 409 by the router, since nerve_reserve is exempt.
    assert result["status"] == 200
    assert "error" in result["body"]["result"]
    rival.release_all()
    daemon_mgr.release_all()


def test_route_teardown_tools_stay_callable_under_a_foreign_reservation(
    tmp_path: Any,
) -> None:
    """`nerve_record_stop` / `nerve_stream_stop` must NOT be 409'd by the router.

    MINOS round-cap finding `teardown-leak-still-open-reservation-exempt-set-is-
    unchanged`: the guard this PR adds introduces a resource leak of its own.
    Session A starts a recording/stream on device X; session B then reserves X;
    A's stop call is now rejected *before dispatch*. `nerve_record_stop` is the
    sole way to flush the MP4 trailer and pull the artifact
    (`recording_tools.py`), and `nerve_stream_stop` is the sole way to close the
    scrcpy device socket, the host TCP forward and the decoder thread
    (`screenshot_tools.py`) — so a blocked stop leaks all of them until the
    daemon restarts. Neither tool calls `_core._check_guard` internally, so
    there is no second line of defence.

    Teardown of a resource the caller already owns is exempt for the same reason
    `nerve_release` is: the reservation is not what authorises the call, and
    refusing it destroys state rather than protecting it.
    """
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    rival = DeviceManager(db_path=db_path)
    rival.reserve("SHARED-DEVICE", purpose="rival session")

    daemon_mgr = DeviceManager(db_path=db_path)

    for tool in ("nerve_record_stop", "nerve_stream_stop"):
        calls: list[str] = []

        def fake_stop(device: str = "", _calls: list[str] = calls) -> dict:
            _calls.append(device)
            return {"status": "success"}

        with patch.object(_core, "_device_mgr", daemon_mgr):
            result = route_request(
                method="POST",
                path=f"/tools/{tool}",
                body={"device": "SHARED-DEVICE"},
                registry={tool: fake_stop},
            )

        assert result["status"] == 200, (
            f"{tool} was rejected {result['status']} under a foreign reservation "
            f"— the recording/stream can never be torn down"
        )
        assert calls == ["SHARED-DEVICE"], f"{tool} must actually dispatch"

    rival.release_all()
    daemon_mgr.release_all()


def test_route_still_guards_destructive_tools_under_a_foreign_reservation(
    tmp_path: Any,
) -> None:
    """The teardown exemption stays narrow: `stop` is not a blanket pass.

    `nerve_emulator_stop` and `nerve_kill` also read as "stop something", but
    they destroy state belonging to whoever holds the device — exactly what the
    guard exists to prevent. Guard against the exemption being widened by
    name-shape rather than by ownership.
    """
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    rival = DeviceManager(db_path=db_path)
    rival.reserve("SHARED-DEVICE", purpose="rival session")

    daemon_mgr = DeviceManager(db_path=db_path)

    for tool in ("nerve_emulator_stop", "nerve_kill"):
        calls: list[str] = []

        def fake_tool(device: str = "", _calls: list[str] = calls) -> dict:
            _calls.append(device)
            return {"success": True}

        with patch.object(_core, "_device_mgr", daemon_mgr):
            result = route_request(
                method="POST",
                path=f"/tools/{tool}",
                body={"device": "SHARED-DEVICE"},
                registry={tool: fake_tool},
            )

        assert result["status"] == 409, f"{tool} must stay guarded"
        assert calls == [], f"{tool} must not run"

    rival.release_all()
    daemon_mgr.release_all()


def test_route_no_device_field_skips_reservation_guard() -> None:
    """A global tool call with no `device` field is unaffected by the guard."""

    def fake_tool(app_id: str = "") -> dict:
        return {"success": True, "app_id": app_id}

    result = route_request(
        method="POST",
        path="/tools/nerve_apps",
        body={"app_id": "com.example"},
        registry={"nerve_apps": fake_tool},
    )
    assert result["status"] == 200


def test_tool_result_with_inline_image_sentinel_is_json_safe() -> None:
    """01c1763d: daemon must strip _INLINE_IMAGE_KEY — json.dumps(bytes) raises.

    The FastMCP stdio wrapper pops the sentinel and re-emits it as an
    ImageContent block; the daemon path serializes the result dict to JSON
    verbatim and crashed on every real nerve_screenshot call since #97
    (test registry fakes never carried the sentinel, so CI missed it).
    """
    import json

    from linc_nerve.mcp._core import _INLINE_IMAGE_KEY

    def fake_screenshot(device: str = "") -> dict:
        return {
            "screenshot": "/tmp/x.png",
            "device": device,
            "size_bytes": 4,
            _INLINE_IMAGE_KEY: b"\x89PNG",
        }

    result = route_request(
        method="POST",
        path="/tools/nerve_screenshot",
        body={"device": "D1"},
        registry={"nerve_screenshot": fake_screenshot},
    )
    assert result["status"] == 200
    assert _INLINE_IMAGE_KEY not in result["body"]["result"]
    json.dumps(result)  # must not raise


def test_router_and_core_identifier_grammars_are_deliberately_divergent() -> None:
    """Pin the divergence the comment block describes, so no future edit "syncs" it.

    MINOS round-cap finding `carry-over-3-package-guard-comment-still-self-
    contradictory`: the guard comment used to end "Keep both patterns in sync by
    hand if either changes" — following that instruction re-introduces the round-2
    regression (hyphenated iOS bundle IDs 400'd at the router). The router's
    `package` field carries BOTH Android package names and iOS bundle IDs, so it
    must permit `-`; `core/android_apps.py` guards Android identifiers on their
    way into a shell, where forbidding `-` is correct.
    """
    from linc_nerve.core.android_apps import _SAFE_IDENTIFIER_RE as core_re
    from linc_nerve.daemon.http_router import _ROUTER_IDENTIFIER_RE as router_re

    ios_bundle_id = "com.my-app.Runner"
    assert router_re.match(ios_bundle_id), "router must accept hyphenated iOS bundle IDs"
    assert not core_re.match(ios_bundle_id), (
        "core's Android-only grammar must stay strict — if this starts passing, "
        "the two patterns have been wrongly unified"
    )

    android_pkg = "com.example.app"
    assert router_re.match(android_pkg) and core_re.match(android_pkg)

    # Both still block flag injection, which is the property that must never diverge.
    assert not router_re.match("-n com.evil.package")
    assert not core_re.match("-n com.evil.package")


# ---------------------------------------------------------------------------
# /health reflects the adb transport (linc-nerve#131 recurrence, 2026-09-03)
#
# Before this suite, `/health` was ok=True unconditionally: it stayed green
# through a 28h bench outage caused by an adb server busy-spinning at 99.7%
# CPU. A health check that cannot fail is not a health check.
# ---------------------------------------------------------------------------


@pytest.fixture
def _adb_registry() -> Any:
    from linc_nerve.core import adb_health

    adb_health.reset_transport_status()
    yield adb_health
    adb_health.reset_transport_status()


def _wedged_status(adb_health: Any, checked_at: float) -> Any:
    return adb_health.AdbTransportStatus(
        responsive=False,
        wedged=True,
        device_count=None,
        probe_seconds=8.0,
        server_pid=4242,
        server_cpu_percent=99.7,
        detail="adb devices timed out after 8.0s; server pid 4242 at 99.7% CPU",
        checked_at=checked_at,
    )


def test_health_is_not_ok_when_the_adb_transport_is_wedged(_adb_registry: Any) -> None:
    """The 2026-09-03 incident, in one assertion."""
    adb_health = _adb_registry
    adb_health.mark_watchdog_started(now=time.time() - 600)
    adb_health.record_transport_status(_wedged_status(adb_health, time.time()))

    result = route_request(method="GET", path="/health", body={}, registry={})

    assert result["status"] == 200, "must stay 200 — _probe_existing_daemon reads the status line"
    assert result["body"]["ok"] is False
    assert result["body"]["adb"]["state"] == "wedged"


def test_health_is_not_ok_when_the_watchdog_stops_reporting(_adb_registry: Any) -> None:
    """A watchdog blocked in the same hung adb call reports nothing at all."""
    adb_health = _adb_registry
    adb_health.mark_watchdog_started(now=time.time() - 3600)

    result = route_request(method="GET", path="/health", body={}, registry={})

    assert result["body"]["ok"] is False
    assert result["body"]["adb"]["state"] == "stale"


def test_health_is_not_ok_when_nerve_cannot_see_a_device_adb_can(
    _adb_registry: Any,
) -> None:
    """Drift: the Galaxy Watch7 sat connected in `adb devices` while nerve's
    view lacked it — the android-watch nightly lane stayed dark all week."""
    adb_health = _adb_registry
    adb_health.mark_watchdog_started(now=time.time() - 600)
    adb_health.record_transport_status(
        adb_health.AdbTransportStatus(
            responsive=True,
            wedged=False,
            device_count=5,
            probe_seconds=0.1,
            server_pid=4242,
            server_cpu_percent=0.2,
            detail="5 device(s)",
            checked_at=time.time(),
            adb_serials=("R3CFAKE00001", "192.0.2.10:35593"),
            drift=("192.0.2.10:35593",),
            drift_seconds=600.0,
        )
    )

    body = route_request(method="GET", path="/health", body={}, registry={})["body"]

    assert body["ok"] is False
    assert body["adb"]["state"] == "drift"
    assert body["adb"]["missing_from_nerve"] == ["192.0.2.10:35593"]


def test_health_carries_the_android_device_count(_adb_registry: Any) -> None:
    """Callers must be able to assert on device count, not on a boolean."""
    adb_health = _adb_registry
    adb_health.mark_watchdog_started(now=time.time() - 600)
    adb_health.record_transport_status(
        adb_health.AdbTransportStatus(
            responsive=True,
            wedged=False,
            device_count=2,
            probe_seconds=0.1,
            server_pid=4242,
            server_cpu_percent=0.2,
            detail="2 device(s)",
            checked_at=time.time(),
        )
    )

    body = route_request(method="GET", path="/health", body={}, registry={})["body"]

    assert body["ok"] is True
    assert body["adb"]["device_count"] == 2


def test_health_stays_ok_in_a_process_with_no_watchdog(_adb_registry: Any) -> None:
    """`nerve bellows` and library consumers run no watchdog — their /health
    must not go red on a transport nobody is sampling."""
    body = route_request(method="GET", path="/health", body={}, registry={})["body"]
    assert body["ok"] is True
    assert body["adb"]["state"] == "unmonitored"


def test_health_reads_cached_status_and_does_no_adb_io(_adb_registry: Any) -> None:
    """/health must never shell out to adb — that is the very call that hangs."""
    adb_health = _adb_registry
    adb_health.mark_watchdog_started(now=time.time() - 600)
    with patch.object(adb_health, "probe_transport", side_effect=AssertionError("no I/O")):
        route_request(method="GET", path="/health", body={}, registry={})


def test_devices_distinguishes_a_failed_discovery_from_an_empty_fleet() -> None:
    """`android: 0` from a swallowed exception read identically to a real zero
    for 28 hours. Discovery failures must be named on the wire."""
    with (
        patch("linc_nerve.core.android.AndroidController") as mock_a,
        patch("linc_nerve.core.ios.IOSController") as mock_i,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        mock_a.return_value.discover.side_effect = TimeoutError("adb wedged")
        mock_i.return_value.discover.return_value = []

        body = route_request(method="GET", path="/devices", body={}, registry={})["body"]

    assert body["device_count"] == 0
    assert body["ok"] is False
    assert "adb wedged" in body["platform_errors"]["android"]
    assert "ios" not in body["platform_errors"]


def test_devices_ok_true_on_a_genuinely_empty_fleet() -> None:
    with (
        patch("linc_nerve.core.android.AndroidController") as mock_a,
        patch("linc_nerve.core.ios.IOSController") as mock_i,
        patch("linc_nerve.mcp._core._ensure_initialized"),
    ):
        mock_a.return_value.discover.return_value = []
        mock_i.return_value.discover.return_value = []

        body = route_request(method="GET", path="/devices", body={}, registry={})["body"]

    assert body["ok"] is True
    assert body["platform_errors"] == {}


# --- Typed-parameter coercion at the dispatch chokepoint (audit 2026-09-30 F1,
# spec linc-nerve-df3 / bead linc-nerve-df3.1). The stdio path gets pydantic
# validation from FastMCP for free; the :8111 path used to forward the raw JSON
# body straight into `registry[tool](**body)`, so `{"x": "1 MARKER"}` reached
# `input tap 1 MARKER 2` on the device shell. `_dispatch_tool` now coerces every
# int/bool/float-annotated parameter and 400s anything that is not that type.


class _Recorder:
    """Typed fakes mirroring the real `_nerve_*_sync` signatures; each call is
    recorded so a rejection test can prove the tool was NEVER invoked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def registry(self) -> dict[str, Any]:
        calls = self.calls

        def tap(device: str, x: int, y: int) -> dict:
            calls.append(("nerve_tap", {"device": device, "x": x, "y": y}))
            return {"device": device, "x": x, "y": y}

        def swipe(
            device: str,
            start_x: int,
            start_y: int,
            end_x: int,
            end_y: int,
            duration_ms: int = 300,
        ) -> dict:
            calls.append(("nerve_swipe", {"duration_ms": duration_ms}))
            return {"duration_ms": duration_ms}

        def long_press(device: str, x: int, y: int, duration_ms: int = 500) -> dict:
            calls.append(("nerve_long_press", {"x": x}))
            return {"x": x}

        def logcat(device: str, lines: int = 50, tag: str = "") -> dict:
            calls.append(("nerve_logcat", {"lines": lines}))
            return {"lines": lines}

        def state(device: str, track_changes: bool = False) -> dict:
            calls.append(("nerve_state", {"track_changes": track_changes}))
            return {"track_changes": track_changes}

        def inject(device: str, x: float = 0.0) -> dict:
            calls.append(("nerve_inject_sensor", {"x": x}))
            return {"x": x}

        def type_text(device: str, text: str) -> dict:
            calls.append(("nerve_type", {"text": text}))
            return {"text": text}

        def launch(
            device: str, package: str, uri: str = "", extras: dict[str, Any] | None = None
        ) -> dict:
            calls.append(("nerve_launch", {"package": package, "uri": uri, "extras": extras}))
            return {"package": package, "uri": uri, "extras": extras}

        return {
            "nerve_tap": tap,
            "nerve_swipe": swipe,
            "nerve_long_press": long_press,
            "nerve_logcat": logcat,
            "nerve_state": state,
            "nerve_inject_sensor": inject,
            "nerve_type": type_text,
            "nerve_launch": launch,
        }


def _post_tool(tool: str, body: dict[str, Any], recorder: _Recorder) -> dict[str, Any]:
    return route_request(
        method="POST", path=f"/tools/{tool}", body=body, registry=recorder.registry()
    )


@pytest.mark.parametrize(
    ("tool", "body", "bad_field"),
    [
        ("nerve_tap", {"device": "ABC", "x": "1 MARKER_TOKEN", "y": 1}, "x"),
        (
            "nerve_swipe",
            {
                "device": "ABC",
                "start_x": 1,
                "start_y": 2,
                "end_x": 3,
                "end_y": 4,
                "duration_ms": "300; id",
            },
            "duration_ms",
        ),
        ("nerve_long_press", {"device": "ABC", "x": 1.5, "y": 2}, "x"),
        ("nerve_logcat", {"device": "ABC", "lines": "50 MARKER"}, "lines"),
        ("nerve_state", {"device": "ABC", "track_changes": "yes"}, "track_changes"),
        ("nerve_tap", {"device": "ABC", "x": True, "y": 1}, "x"),
        ("nerve_tap", {"device": "ABC", "x": None, "y": 1}, "x"),
        ("nerve_tap", {"device": "ABC", "x": "12\n", "y": 1}, "x"),
        ("nerve_tap", {"device": "ABC", "x": float("inf"), "y": 1}, "x"),
        ("nerve_tap", {"device": "ABC", "x": "1.0", "y": 1}, "x"),
        ("nerve_inject_sensor", {"device": "ABC", "x": "1.5; id"}, "x"),
        ("nerve_inject_sensor", {"device": "ABC", "x": False}, "x"),
    ],
)
def test_route_rejects_wrongly_typed_param_with_400_and_never_calls_tool(
    tool: str, body: dict[str, Any], bad_field: str
) -> None:
    recorder = _Recorder()
    result = _post_tool(tool, body, recorder)
    assert result["status"] == 400
    error = result["body"]["error"]
    assert "Invalid parameters" in error
    assert bad_field in error
    assert recorder.calls == []


def test_route_type_rejection_names_expected_and_json_type() -> None:
    result = _post_tool("nerve_tap", {"device": "ABC", "x": "1 MARKER", "y": 1}, _Recorder())
    assert result["body"]["error"] == "Invalid parameters: x must be int, got string"


def test_route_type_rejection_names_the_first_bad_field() -> None:
    result = _post_tool("nerve_tap", {"device": "ABC", "x": [1], "y": {"a": 1}}, _Recorder())
    assert result["body"]["error"] == "Invalid parameters: x must be int, got array"


def test_route_type_rejection_logs_failure_like_the_type_error_path() -> None:
    with patch("linc_nerve.daemon.http_router.log_mcp_call") as mock_log:
        result = _post_tool("nerve_tap", {"device": "ABC", "x": "1 MARKER", "y": 1}, _Recorder())
    assert result["status"] == 400
    mock_log.assert_called_once()
    args = mock_log.call_args[0]
    assert args[0] == "linc-nerve"
    assert args[1] == "nerve_tap"
    assert args[4] is False
    assert "x must be int" in args[5]


@pytest.mark.parametrize(
    ("tool", "body", "field", "expected"),
    [
        ("nerve_tap", {"device": "ABC", "x": "12", "y": 1}, "x", 12),
        ("nerve_tap", {"device": "ABC", "x": "-7", "y": 1}, "x", -7),
        ("nerve_tap", {"device": "ABC", "x": 12, "y": 1}, "x", 12),
        ("nerve_tap", {"device": "ABC", "x": 540.0, "y": 1}, "x", 540),
        ("nerve_state", {"device": "ABC", "track_changes": "true"}, "track_changes", True),
        ("nerve_state", {"device": "ABC", "track_changes": "false"}, "track_changes", False),
        ("nerve_state", {"device": "ABC", "track_changes": True}, "track_changes", True),
        ("nerve_inject_sensor", {"device": "ABC", "x": "1.5"}, "x", 1.5),
        ("nerve_inject_sensor", {"device": "ABC", "x": "-2"}, "x", -2.0),
        ("nerve_inject_sensor", {"device": "ABC", "x": 3}, "x", 3),
        ("nerve_inject_sensor", {"device": "ABC", "x": 9.81}, "x", 9.81),
    ],
)
def test_route_coerces_well_formed_typed_params(
    tool: str, body: dict[str, Any], field: str, expected: Any
) -> None:
    recorder = _Recorder()
    result = _post_tool(tool, body, recorder)
    assert result["status"] == 200
    assert len(recorder.calls) == 1
    received = recorder.calls[0][1][field]
    assert received == expected
    assert type(received) is type(expected)


def test_route_str_and_dict_params_pass_through_byte_identical() -> None:
    """Pin: only int/bool/float hints are touched — `text` and `extras` are not."""
    recorder = _Recorder()
    text = "1 MARKER; 12 true"
    result = _post_tool("nerve_type", {"device": "ABC", "text": text}, recorder)
    assert result["status"] == 200
    assert recorder.calls[0][1]["text"] is text

    extras = {"SEED": "7", "FLAG": "true", "N": 1.5}
    result = _post_tool(
        "nerve_launch", {"device": "ABC", "package": "com.x", "extras": extras}, recorder
    )
    assert result["status"] == 200
    assert recorder.calls[1][1]["extras"] == {"SEED": "7", "FLAG": "true", "N": 1.5}


def test_route_crucible_navigate_payload_shape_still_200s() -> None:
    """Pin: crucible's NerveClient.navigate() posts {device, package: "", uri}."""
    recorder = _Recorder()
    result = _post_tool(
        "nerve_launch",
        {"device": "ABC", "package": "", "uri": "linc://settings?x=1"},
        recorder,
    )
    assert result["status"] == 200
    assert recorder.calls[0][1]["uri"] == "linc://settings?x=1"


def test_route_unresolvable_annotations_skip_coercion() -> None:
    """A tool whose hints cannot be resolved is dispatched untouched (no 500)."""

    def odd(device: str, x: "UndefinedName") -> dict:  # type: ignore[name-defined]  # noqa: F821
        return {"x": x}

    result = route_request(
        method="POST",
        path="/tools/nerve_odd",
        body={"device": "ABC", "x": "1 MARKER"},
        registry={"nerve_odd": odd},
    )
    assert result["status"] == 200
    assert result["body"]["result"] == {"x": "1 MARKER"}


def test_type_hints_are_cached_per_function() -> None:
    def tool(device: str, x: int) -> dict:
        return {}

    with patch("linc_nerve.daemon.http_router.typing.get_type_hints") as mock_hints:
        mock_hints.return_value = {"device": str, "x": int}
        http_router._coerce_typed_params(tool, {"x": 1})
        http_router._coerce_typed_params(tool, {"x": 2})
    mock_hints.assert_called_once_with(tool)


@pytest.mark.parametrize(
    ("module", "fn_name", "tool", "body"),
    [
        (
            "linc_nerve.mcp.input_tools",
            "_nerve_tap_sync",
            "nerve_tap",
            {"device": "ABC", "x": "1 MARKER_TOKEN", "y": 1},
        ),
        (
            "linc_nerve.mcp.diagnostic_tools",
            "_nerve_logcat_sync",
            "nerve_logcat",
            {"device": "ABC", "lines": "50 MARKER_TOKEN"},
        ),
    ],
)
def test_route_rejects_bad_types_against_the_real_tool_functions(
    module: str, fn_name: str, tool: str, body: dict[str, Any]
) -> None:
    """Real `_nerve_*_sync` shapes (from __future__ annotations included) resolve
    and are rejected before any controller/device I/O."""
    import importlib

    fn = getattr(importlib.import_module(module), fn_name)
    with patch("linc_nerve.mcp._core._resolve_device") as mock_resolve:
        result = route_request(
            method="POST", path=f"/tools/{tool}", body=body, registry={tool: fn}
        )
    assert result["status"] == 400
    assert "Invalid parameters" in result["body"]["error"]
    mock_resolve.assert_not_called()
