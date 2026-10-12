# Changelog

All notable changes to linc-nerve will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

> Changelog starts from first documented baseline. For prior history, see `git log`.

## [Unreleased]

## [0.1.0]

First public release.

- Shell-injection hardening: package, activity, intent-extras keys and logcat tags are validated at the device-shell sinks on every transport.
- Daemon: Host allowlist, browser-Origin rejection, JSON-only tool calls; wildcard CORS removed.
- Privacy: the local usage log redacts parameter values by default (`NERVE_USAGE_LOG`).
- `nerve doctor`: the daemon and WebDriverAgent are optional (warn, not fail); so is a not-yet-created `~/.linc`, which nerve creates on first use.
- `linc-nerve` console script (same entry point as `nerve`), so `uvx linc-nerve <command>` works.
- Licence: Apache-2.0.

### Added
- `nerve_launch` / `AndroidController.launch()` accept an `extras` dict of Android
  intent extras, emitted as type-dispatched `am start` flags (`bool`→`--ez`,
  `int`→`--ei`, `str`→`--es`). Lets consent-gated apps run unattended BVTs without
  a manual `adb shell am start … --ez SKIP_CONSENT true` priming step. The HTTP
  daemon route forwards the nested `extras` object unchanged.
- `IOSController.launch()` accepts `extras` on the **simulator** path, mapped to
  `simctl` launch arguments (NSUserDefaults argument domain, `-KEY VALUE`).
  Physical-device and deep-link launches raise `NotImplementedError` for extras —
  iOS has no clean `am start` analog there.

### Changed
- Daemon plist: `ProcessType` is `Interactive` (was `Background`, ~2.8x slower discover on the mini), and `scripts/post_merge_kickstart.py` reloads via bootout + bootstrap when the installed `ProcessType` differs from the loaded `spawn type` and warns on template drift.

### Fixed
- Android recording: `nerve_record_stop` raises "no decodable video frames" when the pulled MP4 decodes no video frame (Wear OS `screenrecord` exits 0 but writes an ~8 KB empty MP4) instead of returning its path.
- Action log: unguarded tap annotation bounds the UI hierarchy read to one quick dump (timeout 3s) without recovery or force-stopping UiAutomation holders.
- Device cache: `_stale_ready_android` remembers successful adb liveness checks per serial for `_CACHE_TTL`, avoiding repeated probes on an expired cache.

### Security
- Usage log: full mode honours nerve_type secret=true by recording text by length only, and error strings scrub secret text in every mode.
- Usage log: redaction is length-only (the reversible short SHA-256 prefix is gone), also covers
  `extras` values, `uri`, and dicts nested in lists, and the log file is created `0600`.
- The MCP server no longer opens `/tmp/nerve_threads.txt` at import; SIGUSR1 thread dumps go to
  stderr, or to an owner-only `$LINC_HOME/nerve/nerve_threads.txt` when `NERVE_THREAD_DUMP=1`.
- Screenshot output uses a private `mkdtemp()` directory created on first use; devicectl JSON temp
  files are removed after each call.
- Action-log paths: nerve_actionlog_start and nerve_replay accept only paths under operator-set roots ($LINC_HOME/nerve/actionlogs or NERVE_ACTIONLOG_ROOTS), opened with O_NOFOLLOW.

## Private baseline - 2026-04-19 (pre-release, documented state)

### Current State

#### Core Controllers
- AndroidController with persistent ADB socket (adbutils, 1-5ms per call)
- IOSController with simctl (simulators) and devicectl (physical devices)
- WDAManager/WDASession for iOS interaction via WebDriverAgent
- ScrcpyManager/ScrcpyStream for hardware-accelerated H264 screenshots (~35-70ms)
- scrcpy v3.x control socket for sub-millisecond input injection
- Per-device circuit breaker (3 failures → 30s cooldown)
- API 35+ blank screenshot detection and fallback

#### MCP Tools (33)
- Discovery: `nerve_discover`, `nerve_pool`
- Screenshots: `nerve_screenshot` (annotate, skip_if_unchanged), stream tools (start/stop/status/screenshot)
- UI: `nerve_elements` (scroll+merge), `nerve_navigate`, `nerve_navigate_screenshot`, `nerve_state`, `nerve_diff`
- Input: `nerve_tap`, `nerve_long_press`, `nerve_swipe`, `nerve_type`, `nerve_key`
- Apps: `nerve_install`, `nerve_launch`, `nerve_kill`, `nerve_apps`, `nerve_uninstall`
- Emulators: `nerve_emulator_list/start/stop`, `nerve_inject_sensor`
- Recording: `nerve_record_start/stop/fixed`
- Diagnostics: `nerve_logcat`, `nerve_stream_logs`, `nerve_inspect`, `nerve_session_stats`, `nerve_metrics`
- Reservation: `nerve_reserve`, `nerve_release`

#### State Engine (Delta Detection)
- 64-bit perceptual hash for screenshot change detection
- Element diffing by identity key
- UI stability polling (50ms intervals, 300ms stable threshold)
- Per-device session statistics

#### HTTP Daemon (:8111)
- SSE server for real-time log streaming
- JSON tool call routing (POST /tools/*)
- Health endpoint with device pool status
- Device polling loop with connect/disconnect events
- Daemon WDA lifecycle management

#### Cross-Process Device Reservation
- SQLite WAL database with heartbeat thread and PID reaper
- Guard check on all device tools
- Automatic cleanup on process exit

#### Infrastructure
- Click CLI: bellows (MCP+HTTP), daemon (HTTP-only), discover
- Buffered stdio transport fixing mcp SDK deadlocks
- JSONL observability logging (10MB rotation)
- MetricsCollector with ring buffer and percentile stats
- 672 tests across 40+ test modules

[0.1.0]: https://github.com/LincForge/linc-nerve/releases/tag/v0.1.0
