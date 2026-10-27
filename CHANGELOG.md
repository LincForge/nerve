# Changelog

All notable changes to linc-nerve will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

> Changelog starts from first documented baseline. For prior history, see `git log`.

## [Unreleased]

### Added
- Guarded action log (linc-nerve-1k7f): `denied_label` refusals now include a `hint` explaining how
  to override the default deny list (or the session's custom `deny_words`) in place. Calling
  `nerve_actionlog_start` on an active guarded session with the same resolved `path` and
  `target_package` atomically swaps a fresh `GuardConfig` in place, appends a `guard_updated` JSONL
  event, and returns `rearmed: True` with updated guard metadata instead of failing `"already
  active"`. Conflicting starts include a `hint` to call `nerve_actionlog_stop` first, and
  `nerve_replay` skips `guard_updated` lines.
- Output retention & cleanup (linc-nerve-si3y): `_next_output_path` caps each process `nerve-output-*`
  directory at `NERVE_OUTPUT_KEEP` (default `200`) files via `_prune_output_dir`, the first
  `_output_dir()` creation sweeps current-UID non-symlink `nerve-output-*` directories older than
  `NERVE_OUTPUT_MAX_AGE_S` (default `86400` s) via `_sweep_stale_output_dirs`, and the new CLI-only
  `nerve cleanup [--all] [--human]` command wipes idle/stale `nerve-output-*` directories on demand.
  Each process holds an exclusive `flock` on `<dir>/.nerve-output.lock`, and neither the sweep nor
  `nerve cleanup` (even with `--all`) removes a directory whose lock is held or unreadable, so an
  idle daemon's output directory is never deleted; `_output_dir()` re-creates its directory if it
  vanishes. `NERVE_OUTPUT_SWEEP=0` disables the sweep (off under pytest unless set to `1`).
- `nerve_logcat` (linc-nerve-jdt9): optional `package=` reads only that app's lines (via its
  running pid; an error if it is not running) and `grep=` keeps lines containing a
  case-insensitive substring, matched in Python and never sent to the device. A `tag=` that
  matches nothing exactly now falls back to the app's pid (package-shaped tag, app running) or a
  case-insensitive substring search over the last 5000 lines, and says so in `matched_via` +
  `hint`. `lines` must be >= 1. CLI: `nerve logcat --package/--grep`.
- Input aliases (linc-nerve-vwkl): `nerve_key` accepts `keycode=` as an alias for `key`, and
  `nerve_swipe` accepts `x1`/`y1`/`x2`/`y2` as aliases for `start_x`/`start_y`/`end_x`/`end_y`.
  Aliases fold into the canonical names before the guard, the action log and `nerve_replay`
  see the call. Missing, conflicting or mixed canonical/alias arguments are a
  `validation_error` naming the parameters. CLI: `nerve key --keycode`, `nerve swipe --x1 ..`.
- `nerve_navigate` (linc-nerve-vwkl): an unknown-app error carries a `hint` pointing at
  `nerve_launch(device, package, uri=<deep link>)` for apps outside the route registry.
- CLI: `nerve launch --uri <uri>` (linc-nerve-o2ro) opens a deep link (e.g.
  `market://details?id=<pkg>` or an https app link) with an Android VIEW intent; PACKAGE confines
  the intent. With no `--uri` the request payload is unchanged.

### Security
- Launch URI validation (linc-nerve-o2ro): an RFC 3986 shape check (`validate_launch_uri`,
  `re.fullmatch`, max 2048 chars) is enforced on `uri` in `AndroidController.launch`,
  `nerve_launch` (Android and iOS) and the daemon HTTP router (`_reject_bad_uri`, HTTP 400).
  `&` stays legal, so multi-param deep links pass; `_q(uri)` quoting remains the second layer.

### Fixed
- Coordinate bounds (linc-nerve-v6fn): `nerve_tap`, `nerve_long_press` and `nerve_swipe`
  (canonical `start_x`..`end_y` and `x1`..`y2` aliases) require integer coordinates in
  `0..32767` (`COORDINATE_MAX`, `coordinate_in_range`). Values outside `0..32767` return a
  `validation_error` (`"Invalid coordinates (integers in 0..32767 required): ..."`) on every
  path (MCP, daemon, CLI) and are refused with `guard: "target_unreadable"` under a guarded
  action-log session without calling the device controller.
- Guard / coordinates (linc-nerve-vwkl): a guarded session refuses a `nerve_tap`,
  `nerve_long_press` or `nerve_swipe` whose coordinates are missing or not integers
  (`guard: "target_unreadable"`, a guard line in the log) instead of skipping the label check.
  The tool bodies refuse non-integer coordinates with a `validation_error` on every path. The
  daemon coerces and type-checks `X | None` / `Optional[X]` params like plain `X` (a bad value
  is a 400), and `nerve schema --json` types the swipe coordinate options as `INT`.

### Changed
- `nerve_ensure_ready`, `nerve_launch`, `nerve_release` (linc-nerve-6xem): `device` now defaults to
  `""` and is auto-selected in the `device_tool` wrapper when the caller holds exactly one active
  reservation (`_pick_reserved_device`), adding `"device": <serial>` and
  `"device_auto_selected": True` to the result. Zero or multiple reservations held by the caller
  return an explicit error; non-opted tools continue to require `device`.
- `nerve_logcat` (linc-nerve-g92x): returns up to 250 lines and 24 KB (sum of UTF-8 line lengths)
  inline before spilling to a file, and when output spills to a file also returns `tail_lines`
  (the last 50 lines, each truncated to 500 chars with a trailing `…` when cut) plus `tail_count`
  and an updated `hint`.
- Reservations (linc-nerve-l7pm): `nerve daemon` stops auto-renewing anonymous (session-id-owned)
  leases so an abandoned CLI lease expires after `timeout_seconds` unless re-reserved; stdio
  bellows keeps renewing its own. `DeviceManager` accepts keyword-only `renew_anonymous` (False
  in daemon mode). `nerve_reserve` reports `"renewal": "daemon" | "caller"`.
- Reservations (linc-nerve-jtt8): `nerve_reserve` with `takeover=True` but no `owner` and no
  `owner_pid` is refused when the device is held, leaving the lease intact. The conflict refusal no
  longer suggests `takeover=True`; it says to re-run with the same owner to renew. `owner_pid`
  without `owner` now stores owner `pid:<owner_pid>` (release with that owner). The CLI fills
  `owner` for `reserve`/`release` from `NERVE_OWNER` (an explicit `--owner` wins; an invalid
  value exits 2). The reserve result gains `shared_owner` (+ a `warning` for a session-id-owned
  lease), `renewed` and `expires_in_s`.
- SECURITY.md names the published v0.1.0 release: public tag `v0.1.0` of LincForge/nerve (commit `547786321703`) and the SHA-256 of the wheel PyPI serves.

### Fixed
- `nerve doctor` (linc-nerve-yml1): stops the `adb` server only when its own probe started it —
  the pre-probe connect to the server address (`ANDROID_ADB_SERVER_PORT` / `ADB_SERVER_SOCKET`) was
  positively refused AND the `adb devices` stderr reports `daemon started successfully`. It then runs
  `adb kill-server` once and notes `(server started for the check and stopped)` in the `adb` check
  detail. Every other outcome — an already-running server, a connect timeout, an unexpected probe
  error, an `ADB_SERVER_SOCKET` nerve rejects, or a server another process started in between — leaves
  the server running. An explicit `nerve doctor --fix` start stays running. `nerve doctor --human`
  is accepted as an alias for the default text output (`--human --json` exits 2 with a usage error).

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
