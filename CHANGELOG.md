# Changelog

All notable changes to linc-nerve will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

> Changelog starts from first documented baseline. For prior history, see `git log`.

## [Unreleased]

## [0.3.0] - 2026-11-19

### Changed
- `nerve --version` and R6 stranger-run documentation fixes (linc-nerve-3d1y): added `@click.version_option` on `nerve` reading installed `linc-nerve` package metadata (`importlib.metadata`); aligned `README.md` with the code (exit codes table including `DEVICE_NOT_FOUND` -> exit `2`, replaced ambiguous `devices` reference with `nerve discover`, corrected screenshot path shape to `nerve-output-<random>/<serial>_<NNNN>_<tool>.png`, removed unshipped private daemon plist / kickstart script references, restated personal-device protection precisely across resolved/unknown-identity network transports and unreadable/degraded registries, and noted positional vs `--device` CLI argument styles); updated `docs/ENGAGEMENT_MODE.md` (`tmp/` scratch dir and automatic in-process CLI routing under `NERVE_ENGAGEMENT`); and added a contract docs test verifying every CLI command named in `README.md` exists in `nerve schema --json`.
- Tool failures reach the agent with their cause on mcp 2.1+ (mcp-2x molecule, toolerror-boundary): `linc_nerve.mcp._mcpserver.NerveMCPServer` (the class of `_core.server`) overrides `call_tool` so a tool crash that mcp 2.1+ would hide as `UnexpectedToolError("Error executing tool <name>")` is re-raised as the SDK `ToolError` `"Error executing tool <name>: <ExceptionClass>: <message>"` (home paths `/Users/<u>`, `/home/<u>` and `$HOME` shown as `~`, capped at 500 chars, class name only when `str()` raises). Deliberate ToolErrors, argument-validation errors, nested crashes and protocol errors pass through unchanged; the `device_tool` / `global_tool` wrappers and their metrics / `log_mcp_call` bookkeeping are untouched. The daemon HTTP path and the CLI (sync registry) are unchanged.
- Port to the `mcp` Python SDK 2.3 (mcp-2x molecule, mcp2-port): `mcp>=2.3.0,<3` (lock: mcp 2.3.0 + mcp-types 2.3.0; httpx-sse, pydantic-settings and python-dotenv leave the tree), `MCPServer("linc-nerve", version=<nerve version>)` replaces FastMCP (serverInfo now reports nerve's version, not the SDK's), `buffered_stdio_server` parses with `jsonrpc_message_adapter` and serializes with `exclude_unset=True` (an error reply's `"id": null` was dropped by `exclude_none`) and closes its pipe transports on exit, bellows drives `_lowlevel_server`, and the mcp 1.x `ServerSession` monkeypatch (which imported the removed `ServerRequestResponder` and would crash bellows at start) is gone. strict_tools and the wire inputSchema are unchanged (pinned by `test_schema_help`).
- Automated stdio MCP round trip for `nerve bellows` (mcp-2x molecule, stdio-roundtrip-pin): `tests/test_mcp2_stdio_roundtrip.py` spawns bellows over real pipes in an isolated HOME/LINC_HOME/PATH and drives initialize, tools/list, five sequential `nerve_session_stats` calls, a malformed line and an unknown argument. Its hostile-line contracts exposed two transport bugs, now fixed: an invalid-UTF-8 line or a line over asyncio's 64 KiB readline limit killed the stdio reader (and the server); `buffered_stdio_server` now reports such a line as an Exception item and keeps reading (line limit 16 MiB). Bellows also exits on stdin EOF instead of serving its HTTP side forever.
- Android hardware-serial identity helper (linc-nerve-d7fc): pure `core/android_identity.py` reads `getprop ro.serialno` per adb transport under a hard timeout (a failure reads as unknown and logs a warning), normalizes it (empty, `unknown`, `0123456789ABCDEF` / `1234567890ABCDEF` / all-zero placeholders, control characters and values over 64 chars read as unknown and are counted by reason), and groups transports sharing a validated serial behind the preferred one (`USB > ip:port > _adb-tls-connect > other`) with sorted aliases; emulators and unknown serials never merge, and transports merge only when their model matches and at most one is USB (`android_transport_collapse_refused` otherwise). Not yet wired into `discover()` (follow-up linc-nerve-u2xa).
- Opt-in post-input UI settle wait (linc-nerve-byh0): `nerve_tap`, `nerve_long_press`, `nerve_swipe`, `nerve_type`, and `nerve_key` (and `nerve tap --wait-idle-ms`) accept `wait_idle_ms: int = 0` (`0..10000`, validated before the action runs), calling `wait_for_idle` via `_stability` (using the Android PIL fast path when available) when `wait_idle_ms > 0` and the action succeeded and attaching `idle: {stable, elapsed_ms, polls, reason}` to the response.
- Grounded prompt taps (linc-nerve-rlw2): guarded sessions return the grounding error dict when prompt grounding fails (recording the guard refusal without issuing a coordinate-less tap), `_LAST_GROUNDED_TAP` is bounded to 64 entries with a 60 s monotonic TTL and popped on every `_nerve_tap_sync` outcome, and a second pre-tap screenshot aborts the tap when its 16x16 average hash differs from the grounded screen.

### Fixed
- R4b fixes (#305): the fallback reader for a malformed `device-fleet.yaml` registers every identifier key of a broken one-line entry (a missing `}` used to leave all but the first unregistered, so a personal device stayed allowed); `owner_pid` beyond the platform pid range is a validation error instead of a crash; the `nerve doctor` daemon probe ignores `HTTP_PROXY`/`ALL_PROXY`; doctor hints name only shipped commands.
- Personal-device identity after an adb restart (linc-nerve-86ac): adb renumbers transport ids from 1 when its server restarts, so a cached identity could belong to a different device on the same address. Every enforcing decision (input, kill, launch, reservation, CLI and HTTP `/tools` calls) now reads `ro.serialno` afresh; the identity cache is keyed by adb transport id and serves discovery and display only. The engagement allow-list matches hardware serials only.
- Network transport detection and probe cost (linc-nerve-6c33): `tcp:`/`udp:` prefixes, bare IPv4/IPv6 hosts (with brackets or zone ids), `localhost:<port>` and mixed-case or `.local.` mDNS names are treated as network transports and verified; a failed identity probe is remembered for 5 s (it only ever refuses); enforcing calls no longer read the adb transport listing; `nerve doctor` probes network devices concurrently within a 15 s budget, counting unfinished probes as refused; `host:devices-l` lines are split on the first tab only.
- Personal-device refusal across every ADB transport (`ip:port`, `host:port`, `adb-<SERIAL>-<sfx>._adb._tcp`, `._adb-tls-connect._tcp`) (linc-nerve-eh10): `android_identity.is_network_transport` and `mdns_hardware_serial` identify network transports and parse embedded mDNS serials, `AndroidController.read_hardware_serial` resolves `ro.serialno` for unmapped network transports (every enforcing decision reads it afresh, because adb renumbers transport ids after a server restart; a short cache keyed by the adb transport id serves discovery and display only, linc-nerve-86ac), and `device_decision` fails closed (`error_category: "personal_device"`) whenever a personal device is registered and a network transport's hardware identity cannot be verified; MCP tools, HTTP `/devices`, CLI in-process routing, `nerve doctor`, and `DeviceManager.reserve` all route through the resolved policy.
- Hardened `device-fleet.yaml` parsing and degraded-registry warnings (linc-nerve-56eg): extracted registry parsing into `core/fleet_entries.py` and `core/fleet_parse.py`, parsing platform sections independently, merging duplicate YAML keys instead of last-wins, treating any non-boolean-`false` `personal` value as personal, bounding line-level recovery (`_MAX_RECOVERY_CHUNKS`, byte/wall-clock budgets, flow-depth cap) and marking recovered or identifier-less registries `degraded`, logging `fleet_registry_unreadable` / `fleet_registry_degraded` on every `device_decision`, refusing network transports when unreadable or degraded, and reporting `registry degraded` in `nerve doctor`.
- CLI in-process engagement isolation (linc-nerve-wp3e): when `NERVE_ENGAGEMENT` is non-empty, `NerveTransport.call` routes through `_call_in_process_under_engagement` so single-shot CLI commands run in-process under the caller's environment without contacting `:8111`, `daemon.port`, or `LINC_NERVE_DAEMON`, and `_DAEMON_ONLY` CLI commands fail with `DAEMON_UNAVAILABLE` rather than reaching a shared daemon.
- Bounds and error-path hardening (linc-nerve-hih6): `nerve_reserve` / `DeviceManager.reserve` validate `timeout_seconds` (`int` in `1..86400`) and `owner_pid` (`0` or alive same-UID pid `> 1`) before touching `devices.db`; `nerve_long_press` / `nerve_swipe` bound `duration_ms` (`int` in `0..60000`); `nerve_usage_stats` / `nerve_metrics` bound `since_hours` (finite `0..87600`) and `top_n` / `last_n` (`int` in `0..10000`, with `last_n=0` returning `[]`); HTTP daemon `POST /tools/*` rejects non-object JSON bodies with HTTP `400`; `frames.extract_request` returns a `validation_error` dict on refused frame writes; and `_safe_log_mcp_call` logs a warning instead of failing tool or HTTP responses when writing `mcp-calls.jsonl` raises `OSError`.
- Public-safe source scrub and expanded export guard (linc-nerve-vv14): scrubbed real-looking device serials, host-specific bench comments, and private repository references from shipped source and tests (internal tracker ids in code comments remain; they carry no data), and extended `tests/test_public_safe.py` to enforce those rules across exported files.
- Proxy-bypass update in `uv.lock` for `linc-cli-kit` `0.3.1` (`HttpDaemonTransport` uses `httpx.Client(trust_env=False)` so `HTTP_PROXY` / `http_proxy` / `ALL_PROXY` never intercept local daemon calls; `pyproject.toml` now requires `linc-cli-kit>=0.3.1`).
- Physical iOS `nerve_ensure_ready` lock detection (linc-nerve-48ox): `IOSController.lock_state(serial)` runs `devicectl device info lockState --device <serial> --json-output -` (`timeout=15`, `check=False`) and parses `result.passcodeRequired` / `result.unlockedSinceBoot` via pure `parse_lock_state(text)` (rejecting hostile/non-standard/oversized JSON and mapping `XcodeNotReadyError`, `TimeoutExpired`, `OSError`, and non-zero exits to `"unknown"`), and `mcp/state_tools.py` `_ios_verdict` returns `ready=False, reason="ios_locked", unlocked=False, report_only=False, lock_state="locked"` when locked, `ready=True, reason="ok", unlocked=True, report_only=False, lock_state="unlocked"` when unlocked, and today's `report_only=True, reason="ios_physical_state_not_detectable"` dict with `lock_state="unknown"` when unreadable.
- Per-tool stdio MCP timeout ceilings and `nerve_record_fixed` duration bounds (linc-nerve-us7g): `mcp/_core.py` passes `timeouts.mcp_ceiling(tool_name)` from `device_tool` through `_run_on_device` to `_run_with_timeout(fn, serial, timeout_s=_DEVICE_CALL_TIMEOUT, tool_name="")`, returning `"<tool> did not finish within <n>s and may still be running on the device; check its result before retrying."` for listed tools (`nerve_install` 150 s, `nerve_record_fixed` 160 s, `nerve_record_stop` 150 s) while keeping the 90 s unlisted message, and `_nerve_record_fixed_sync` rejects non-`int` / `bool` or out-of-range (`< 1` or `> 120`) `duration_seconds` with `{"error": "duration_seconds must be an integer from 1 to 120", "validation_error": True}` before touching the device.
- CLI long-operation timeout default and honest timeout hint (linc-nerve-frnc): `NerveTransport.call` substitutes `timeouts.cli_default(spec.name)` (`200` s) when `--timeout` is not passed for tools in `CLI_DEFAULT_S` while preserving explicit `--timeout` and the kit's `30` s default for unlisted tools, `honest_toolerror` replaces the `upstream_timeout` hint for listed tools with `"the operation may still be running on the device; check its result (for install: `nerve apps <device>`) before retrying, or pass --timeout"`, and `timeouts.py` validates the timeout table at import time via `ValueError`.
- `nerve_state` capture-failure reporting (linc-nerve-8m9g): `_collect_android_state` and `_collect_ios_state` now record `screenshot_error` (truncated to 300 chars) instead of silently swallowing screenshot exceptions, `_collect_ios_state` sets `elements_error = "element tree not collected by nerve_state on iOS; call nerve_elements"` without starting WDA so iOS state captures never look like an empty screen and `attach_visual_grounding_hint` receives `elements_error`, and `_collect_android_state` truncates `elements_error` to 300 chars.
- Timed-out `adb install` is no longer retried and reports that the install may still be completing (linc-nerve-uwfd): `src/linc_nerve/core/timeouts.py` defines `INNER_BUDGET_S`, `MCP_CEILING_S`, `DAEMON_CEILING_S` (`180.0`, read by `sse_server._DEFAULT_TOOL_TIMEOUT`), and `CLI_DEFAULT_S` with `inner < mcp <= daemon < cli` (`nerve_install`, `nerve_record_fixed`, `nerve_record_stop`, `nerve_emulator_start`; unlisted defaults `90` / `30`), and `AndroidController.install` treats `TimeoutError` from `_adb_install` as terminal after one attempt with `"adb install timed out after 120s; the install may still be completing on the device. Check nerve_apps for the package before retrying."` while retaining `OSError` / `ConnectionResetError` retries.
- Coexisting with `scrcpy` bench proof and docs (linc-nerve-ntkv): documented nerve's versioned `/data/local/tmp/nerve-scrcpy-server-v3.3.4.jar` device path and `0x6e65xxxx` (`localabstract:scrcpy_6e65xxxx`) `scid` isolation in `README.md` (`### Coexisting with scrcpy`) and `LINC.md` (`Operational Notes`), pinned by `tests/test_scrcpy_coexist_docs.py` and `tests/test_public_docs.py`.
- Scoped scrcpy cleanup and forward sweep (linc-nerve-he7c): `ScrcpyManager._cleanup_orphans` matches only nerve's versioned device path (`adb.*{re.escape(_DEVICE_PATH)}`) instead of `adb.*scrcpy-server`, and `ScrcpyManager._sweep_orphan_forwards` removes only `localabstract:scrcpy_<8-hex>` forwards in the nerve scid range while skipping and counting foreign or malformed lines in `scrcpy_foreign_forwards_kept`.
- `nerve_launch` / `nerve launch --activity`: optional fully qualified Android `activity`, passed through to `AndroidController.launch` (validated by `_ACTIVITY_RE.fullmatch` before any shell) and echoed in the result. A package with two launcher activities (a debug build with a second launcher) could not be launched through the tool at all ("has 2 launcher activities ... pass activity= to choose one").
- scrcpy namespace isolation (linc-nerve-cj7w): `ScrcpyStream` pushes the vendored `scrcpy-server` binary to `/data/local/tmp/nerve-scrcpy-server-v{SCRCPY_VERSION}.jar`, verifies both remote byte size and `sha256sum` against the cached vendored SHA-256 before skipping a push (re-pushing and logging `scrcpy_server_pushed` with `reason` on any mismatch or unreadable hash), and formats both the `adb forward` remote (`scid_socket_name`) and `scid=` server argument from one 31-bit `nerve_scid(port)` (`0x6e650000 | (port & 0xffff)`) helper.
- Bounded UI-settle helper (linc-nerve-q11w): `state_engine.wait_for_idle(screenshot_fn, timeout_ms, *, pil_mode=False, sleep=time.sleep, clock=time.monotonic)` validates `timeout_ms` as an `int` (not `bool`) in `0..10000` (`ValueError`), wraps `wait_for_stable_ui` to return `{stable, elapsed_ms, polls, reason}` (`""` when stable, `"timeout"`, or `"capture_failed: <exc class>: <first 200 chars>"` after waiting out the remaining budget), and `nerve_navigate_screenshot` delegates `_poll_stability` to it, rejects invalid `settle_ms` before navigating, and adds `stability_reason` to its result.
- ADB Local Network denial classification (linc-nerve-3p1v): `adb_health.classify_adb_connect` and `_reconnect` probe direct TCP reachability on LAN-failure `adb connect` outputs, log `adb_reconnect_classified`, surface `local_network_denied` serials on `/health` (clearing on a subsequent `ok` reconnect), and report `adb_local_network` `"fail"` in `nerve doctor` with the GUI terminal restart remedy.
- Honest guard refusal & empty-recording error messages (linc-nerve-gm5g): `_core._app_refusal` returns a `"not found"` refusal dict when `_resolve_device` fails on a guarded deep-link launch instead of interpolating that sentence as the platform in `uri_launch_refusal`, and `recording.assert_recording_playable` takes `is_wear: bool = False` (passed from `android_sensor._stop_guest_recording`) so non-Wear frame-less recordings report a phone/tablet empty-recording message suggesting re-recording while Wear OS keeps the `scrcpy` advice.
- A refused/malformed `ADB_SERVER_SOCKET` / `ANDROID_ADB_SERVER_PORT` now stops every emulator, `nerve doctor` and scrcpy-teardown adb subprocess with one explicit error naming the variable (zero adb spawns); `nerve_emulator_list` reports `adb_refused` (linc-nerve-k2yj).

- File hardening (linc-nerve-lgkt): `frames._save_png` opens frame PNGs with `O_WRONLY|O_CREAT|O_NOFOLLOW|O_NONBLOCK` (without `O_TRUNC`), verifies `S_ISREG` and `st_nlink == 1` via `fstat` before truncating and writing, `action_log._require_regular` closes its fd on any `fstat` exception, and `pyproject.toml` raises the public Pillow floor to `pillow>=13.0.0`.

### Known issues
- After a failed identity probe, that network address is refused for up to 5 s even if the device behind it is allowed; the next call after that probes again.
- The serial embedded in an mDNS transport name (`adb-<SERIAL>-<suffix>._adb-tls-connect._tcp`) is taken as the device's identity without reading `ro.serialno`, so a device advertising another device's name is identified by that name.
- In engagement mode, a fleet entry keyed by `ip:port` with `personal: false` clears whatever device is at that address, even when its identity probe fails.
- A fleet file with a non-UTF-8 byte (or in UTF-16) is treated as unreadable as a whole rather than partly recovered; network transports are then refused and a warning is logged on every decision.
- Refusal messages name the full `$LINC_HOME` path of the fleet file.
- CLI commands print debug log lines to stderr by default.
- When a fleet file cannot be parsed, the fallback reader can over-register: an unclosed flow list also takes in the next valid entry, and a `serial:` inside a broken entry's free text is registered too, so an affected bench device is refused (it fails closed; no personal device is exposed). Fix the file to restore it.
- The scrcpy stream creates its adb forward before starting its client, so a second nerve process starting a scrcpy cleanup in that window can remove the live forward; the stream then fails to connect and must be restarted.
- Error dicts returned by a tool for an expected failure can carry absolute host paths; only unexpected crashes are reported with the home directory shown as `~`.
- Parsing a deeply nested 1 MiB flow file takes tens of seconds (PyYAML), though it stays bounded.

## [0.2.0] - 2026-10-27

### Added
- Visual UI grounding via Meta SAM 3.1: new `nerve_ground(device, prompt, annotate=False)` MCP tool
  and `nerve ground` CLI command (`src/linc_nerve/core/grounding.py`, `src/linc_nerve/mcp/ui_tools.py`)
  segment screenshots using `sam-3.1` (`POST /v1/responses`), decode 1-bit base85 masks via
  `meta-sam-parser` to compute true mask center-of-mass, and scale source-image coordinates into
  native device tap space (`1080x2340` Android phone, `432x432` Wear OS watch, iOS points).
  `nerve_tap(device, prompt=...)` resolves semantic prompts to physical `(x, y)` coordinates before
  coordinate guards and action logging, and `nerve_elements` / `nerve_state` emit
  `visual_grounding_hint` when the UI tree is empty, failed, or has unlabelled clickable controls.
  Grounding is opt-in (`NERVE_GROUNDING=1`, because it uploads screenshots to Meta), and
  `meta-sam-parser` is the optional extra `linc-nerve[grounding]` because it is distributed under
  Meta's SAM License. The extra is pinned `<0.2` and lazy-imported, so core nerve never needs it.
  A prompt tap fires only on one clear match on a known screen size (confidence ≥ 0.5, ≥ 0.1 ahead
  of the runner-up), and oversized or over-area mask responses are rejected before decode.
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
