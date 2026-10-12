# linc-nerve

Field-tested device control for AI coding agents — the self-healing tricks we learned keeping a
physical Android/iOS bench alive, as an MCP server and a plain CLI.

nerve exposes <!-- NERVE:TOOL-COUNT -->48<!-- /NERVE:TOOL-COUNT --> tools — screenshots, the UI
element tree, tap/swipe/type, app install/launch, logs, recording — to any MCP client, and the same
tools as `nerve <tool>` commands that print JSON.

## Field notes

A device bench fails in ways a single emulator never shows you. Each of these is a behaviour you can
read in the code:

- **adb can wedge while looking alive.** After a wireless device drops, the adb server can sit in a
  busy-spin: every `adb` call hangs, and `adb kill-server` hangs too because it talks to the same
  wedged socket. A watchdog treats "unresponsive **and** server at ≥ 80% CPU" as the wedge
  signature and recovers by killing the server's PID, never by `adb kill-server`; an unresponsive
  server with an idle CPU is left alone. → [`src/linc_nerve/core/adb_health.py`](https://github.com/LincForge/nerve/blob/main/src/linc_nerve/core/adb_health.py)
- **A leaked UiAutomation session silently breaks the element tree.** `uiautomator dump` is killed
  while another process (a leaked test driver) holds the device's single UiAutomation connection.
  Recovery is evidence-gated and escalates in tiers under one 45 s deadline, force-stopping only a
  known allowlist of test drivers — the Pixel Watch companion app, whose package name looks similar,
  is never touched. An empty element list therefore always means an empty screen, never a failed
  dump. → [`src/linc_nerve/core/android_ui.py`](https://github.com/LincForge/nerve/blob/main/src/linc_nerve/core/android_ui.py)
- **Readiness checks fail toward "unknown", not "ready".** Wake/lock state is parsed from `dumpsys`,
  whose format varies by OEM and Android version; an unrecognised format reads as `unknown`, so a
  caller skips rather than tapping a locked screen. → [`src/linc_nerve/core/device_state.py`](https://github.com/LincForge/nerve/blob/main/src/linc_nerve/core/device_state.py)
- **Screenshots handed to an agent are budgeted.** Inline images for MCP agents are rescaled to
  about 300k pixels (≈ 400 vision tokens); the full-resolution file on disk is left untouched for
  everything else. → [`src/linc_nerve/mcp/_core.py`](https://github.com/LincForge/nerve/blob/main/src/linc_nerve/mcp/_core.py)

<!-- field-notes essay link: added by the release PR (runbook R7) -->

## Quickstart

<!-- quickstart:start -->
Android emulator first — no physical device, Xcode or WebDriverAgent needed.

**Prerequisites:** Python 3.13+ (uv fetches it for you; with plain `pip` you need it installed),
[uv](https://docs.astral.sh/uv/), the Android SDK command-line tools, and platform-tools and
emulator on your `PATH` (`adb` and `emulator`; with Homebrew's `android-commandlinetools` add
`$(brew --prefix)/share/android-commandlinetools/emulator` to `PATH`). Then create an emulator.

**On Apple silicon, replace `x86_64` with `arm64-v8a` in the first two lines below.**
`avdmanager` asks whether to create a custom hardware profile; answer `no`.

```bash
sdkmanager "platform-tools" "emulator" "system-images;android-35;google_apis;x86_64"
avdmanager create avd -n nerve -k "system-images;android-35;google_apis;x86_64"
emulator -avd nerve -no-window &
adb wait-for-device
```

`adb wait-for-device` returns before Android has finished booting. Wait until
`adb shell getprop sys.boot_completed` prints `1` (usually a minute or so on first boot) before
step 1, or the first `state` call can hit a half-booted device.

**Install:** `pipx install --python python3.13 linc-nerve` (or `python3.13 -m pip install linc-nerve` inside a venv),
after which the same commands run as `nerve doctor`, `nerve discover` and
`nerve state --device <serial>`; and for Claude Code `claude mcp add nerve -- nerve bellows`.

**1. Check the environment.** The daemon, WebDriverAgent and `$LINC_HOME` (default `~/.linc`) are
optional, so on a fresh Android-only machine nothing fails (exit code 0); missing optional pieces
show as `!` warnings with an indented `hint:` line. The exact set of lines depends on your machine
(macOS runs the `xcode-clt` check, a running daemon shows `✓`). Sample from a fresh Linux machine,
trimmed:

```bash
uvx linc-nerve doctor
```

```text
  ✓ adb: adb responsive (/usr/local/bin/adb)
  ✓ device-auth: no unauthorized devices
  ✓ scrcpy-server: …/linc_nerve/vendor/scrcpy-server-v3.3.4
  ! wda-project: WDA_PROJECT_DIR not set — iOS tap/swipe/elements unavailable (Android works without it)
      hint: git clone https://github.com/appium/WebDriverAgent.git ~/.linc/wda/src && export WDA_PROJECT_DIR=~/.linc/wda/src
  ! wda-team-id: WDA_TEAM_ID not set — physical-iOS WDA signing will fail
      hint: …
  - xcode-clt: skipped — not macOS
  ! daemon: no nerve daemon on 127.0.0.1:8111 — CLI calls run in-process (the daemon is optional)
      hint: …
  ! linc-layout: missing directories: /home/you/.linc, /home/you/.linc/nerve/recordings — nerve creates them on first use
      hint: …

8 checks: 0 failed, 4 warned
```

**2. Find the emulator.**

```bash
uvx linc-nerve discover
```

```text
{"devices": [{"serial": "emulator-5554", "platform": "android", "device_type": "emulator", …, "state": "online"}], "count": 1, "android": 1, "ios": 0}
```

With several devices attached, `devices` lists all of them; pass the emulator's serial to the
later commands.

**3. Read the screen** — foreground app, element tree and a screenshot path in one call. Your
launcher package and element count depend on the system image, and the screenshot lands in your
temp directory (`$TMPDIR` on macOS, not `/tmp`):

```bash
uvx linc-nerve state --device emulator-5554
```

```text
{"device": "emulator-5554", "platform": "android", "foreground": {"package": "com.google.android.apps.nexuslauncher", …}, "element_count": 31, "elements": [ … ], "screenshot": "<temp dir>/nerve-output/…png", …}
```

**4. Give it to your agent.** For Claude Code:

```bash
claude mcp add nerve -- uvx linc-nerve bellows
```

Run `claude mcp list` and check that `nerve` shows as connected, then paste a first prompt:

> Take a screenshot of the emulator and tell me what app is in the foreground.
<!-- quickstart:end -->

Every MCP tool is also a CLI command: the tool name minus `nerve_`, with `_` → `-`
(`nerve_clear_app_data` → `nerve clear-app-data`). Output is JSON on stdout; for text, put `--human`
before the command (`nerve --human discover`, not `nerve discover --human`). Exit `0` success, `1`
tool error, `2` usage error. `nerve schema --json` prints the full command tree.

## iOS (experimental)

iOS simulators and physical devices work through [WebDriverAgent](https://github.com/appium/WebDriverAgent)
(WDA), which you build once. `nerve doctor` prints the same steps as hints:

```bash
git clone https://github.com/appium/WebDriverAgent.git ~/.linc/wda/src
export WDA_PROJECT_DIR=~/.linc/wda/src
export WDA_TEAM_ID=<Apple Developer Team ID>   # physical devices only; simulators don't need it
```

Needs macOS with Xcode. Expect rough edges: WDA always binds port 8100, first start takes ~4 s on a
simulator and 15–30 s on a physical device, physical devices need code signing, and watchOS is not
supported.

## Reference

### Configuration

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `NERVE_USAGE_LOG` | `redacted` | Local usage log mode: `off`, `redacted` or `full` (see [Trust](https://github.com/LincForge/nerve#trust)) |
| `LINC_HOME` | `~/.linc` | Where the usage log and recordings live; when set, the device-state database also moves to `$LINC_HOME/.local/share/linc-nerve/devices.db` (default `~/.local/share/linc-nerve/devices.db`) |
| `NERVE_ACTIONLOG_ROOTS` | `$LINC_HOME/nerve/actionlogs` | Colon-separated list of allowed host directory roots for `nerve_actionlog_start` and `nerve_replay` files |
| `WDA_PROJECT_DIR` | *(none)* | WebDriverAgent checkout (iOS only) |
| `WDA_TEAM_ID` | *(none)* | Apple Developer Team ID (physical iOS only) |
| `NERVE_HTTP_PORT` | `8111` | Port of the optional HTTP daemon: the port the daemon started by `nerve bellows` listens on and the port `nerve doctor` probes (`nerve daemon` takes `--port` instead) |
| `LINC_NERVE_DAEMON` | `http://127.0.0.1:8111` | Daemon URL that CLI commands call before falling back to in-process |
| `ANDROID_ADB_SERVER_PORT` / `ADB_SERVER_SOCKET` | `127.0.0.1:5037` | adb server nerve talks to (`ADB_SERVER_SOCKET` as `tcp:<port>` or `tcp:<host>:<port>` wins; loopback hosts only) — isolates nerve from the host's devices |
| `NERVE_ALLOWED_HOSTS` / `NERVE_ALLOWED_ORIGINS` | *(none)* | Extra Hosts / browser Origins the daemon accepts |

### HTTP daemon (optional)

`nerve daemon` (or `nerve bellows`, which also serves MCP over stdio) runs an HTTP server on
`127.0.0.1:8111`: `GET /health`, `GET /devices`, `POST /tools/<tool_name>` with a JSON body, and
`GET /stream?device=<serial>` for a server-sent-events log stream. CLI commands use the daemon when
one is running and otherwise run the tool in-process. A few stateful tools (recording, streaming,
reservations, `diff`, `session-stats`) need the daemon, and say so instead of answering wrongly.

<!-- NERVE:TOOLS-AUTO-START — generated by `python -m linc_nerve.docs_gen`, do not edit -->
## MCP Tools (48)

Tools are registered via `@device_tool` (requires a device serial) or `@global_tool` (no device needed) in `src/linc_nerve/mcp/*_tools.py`. This section — including the count — is generated from those registrations by `python -m linc_nerve.docs_gen`; `tests/test_docs_sync.py` fails CI when it is stale. Call `nerve_capabilities` for the per-platform support matrix.

### Discovery & Capabilities

| Tool | Type | Description |
|------|------|-------------|
| `nerve_capabilities` | global | Per-platform support matrix for nerve's capability groups |
| `nerve_discover` | global | List all connected devices (Android + iOS) |
| `nerve_pool` | global | Show device pool status — who has what reserved |

### Screenshots & Streaming

| Tool | Type | Description |
|------|------|-------------|
| `nerve_screenshot` | device | Capture one screenshot — a stateless PNG of the current device display |
| `nerve_stream_screenshot` | device | Pull the current frame from an active screen-mirror streaming session |
| `nerve_stream_start` | device | Open a live screen-mirror session for warming up fast screenshots |
| `nerve_stream_status` | global | Report status and health of active screen-mirror streaming sessions |
| `nerve_stream_stop` | device | Tear down the live mirror session opened by nerve_stream_start |

### UI Inspection

| Tool | Type | Description |
|------|------|-------------|
| `nerve_diff` | device | Return UI changes since the last nerve_state snapshot on this device |
| `nerve_elements` | device | Return the UI element tree only (clickable + text widgets) — no image bytes |
| `nerve_state` | device | Capture full device state in one call (screenshot + UI elements + foreground) |

### Navigation

| Tool | Type | Description |
|------|------|-------------|
| `nerve_navigate` | device | Navigate to a named screen in an app using its registered deep link |
| `nerve_navigate_screenshot` | device | Navigate to a screen and capture a screenshot after the UI settles |

### Input

| Tool | Type | Description |
|------|------|-------------|
| `nerve_density` | device | Read display density — dpi and the dp-to-px multiplier — for a device |
| `nerve_key` | device | Press a hardware/system key |
| `nerve_long_press` | device | Long press at screen coordinates |
| `nerve_swipe` | device | Two-point gesture from one xy coordinate to another — swipe, drag, or scroll |
| `nerve_tap` | device | Tap a single point on the device display at known x/y coordinates |
| `nerve_type` | device | Type text into the currently focused input field via the soft keyboard |

### App Management

| Tool | Type | Description |
|------|------|-------------|
| `nerve_apps` | device | List installed apps on the device |
| `nerve_clear_app_data` | device | Wipe an app's data + cache → first-install state |
| `nerve_grant_permission` | device | Grant a runtime permission to an app (Android ``pm grant``) — no dialog |
| `nerve_install` | device | Install an app on the device |
| `nerve_kill` | device | Force-stop / terminate an app |
| `nerve_launch` | device | Launch an app by package/bundle ID, optionally opening a deep link URI |
| `nerve_uninstall` | device | Uninstall an app from the device |

### Emulator / Simulator

| Tool | Type | Description |
|------|------|-------------|
| `nerve_emulator_list` | global | List available Android emulators (AVDs) and iOS Simulators, and their running state |
| `nerve_emulator_start` | global | Start an Android emulator (by AVD name) or iOS Simulator (by UDID/Name) |
| `nerve_emulator_stop` | device | Stop a running Android emulator or iOS Simulator |
| `nerve_inject_sensor` | device | Inject sensor data into an Android emulator via its telnet console |

### Recording

| Tool | Type | Description |
|------|------|-------------|
| `nerve_extract_frames` | global | Extract PNG frames at given timestamps from a host-side MP4 recording |
| `nerve_record_fixed` | device | Record a fixed-length screen video — self-terminating MP4 capture |
| `nerve_record_start` | device | Begin recording video to disk — open-ended MP4 capture on the device |
| `nerve_record_stop` | device | Finalise the MP4 file and end the durable recording session |

### Action Log

| Tool | Type | Description |
|------|------|-------------|
| `nerve_actionlog_start` | device | Start an action log — a JSONL record of every nerve tool call on a device |
| `nerve_actionlog_stop` | device | Stop the action log on a device and report the file and entry count |
| `nerve_replay` | device | Replay an action log on a device and report whether its steps ran |

### Device Readiness

| Tool | Type | Description |
|------|------|-------------|
| `nerve_ensure_ready` | device | Ensure a device is awake + unlocked before UI interaction, and report readiness |
| `nerve_network` | device | Report whether a device is online — default network transport + validated internet |

### Diagnostics & Observability

| Tool | Type | Description |
|------|------|-------------|
| `nerve_inspect` | device | Non-invasive post-mortem snapshot of a process — pid, RSS, crash signature |
| `nerve_logcat` | device | Tail the last N lines from a device's logcat buffer or iOS system log |
| `nerve_metrics` | global | Performance metrics for nerve commands |
| `nerve_session_stats` | global | Get state tracking statistics for all devices |
| `nerve_stream_logs` | device | Get the SSE subscription URL for continuous real-time log streaming |
| `nerve_usage_stats` | global | Usage analytics over the shared MCP call log (~/.linc/mcp-calls.jsonl) |

### Reservation

| Tool | Type | Description |
|------|------|-------------|
| `nerve_release` | device | Release a device reservation |
| `nerve_reserve` | device | Reserve a device exclusively for this session |

### Other

| Tool | Type | Description |
|------|------|-------------|
| `nerve_device_state` | device | Control Android device power/idle/standby state for adversarial testing |
<!-- NERVE:TOOLS-AUTO-END -->

## Trust

- **No telemetry, no phone-home.** nerve talks to your devices, your local adb server, and — for
  iOS — the WebDriverAgent it starts. Nothing else.
- **A local usage log, under your control.** Each MCP tool call is appended to
  `$LINC_HOME/mcp-calls.jsonl` (default `~/.linc/mcp-calls.jsonl`, rotated at 10 MB) with the tool
  name, timing, success and parameters. Set `NERVE_USAGE_LOG=off|redacted|full`:
  - `redacted` (default) — values of sensitive parameters (`text`, `password`, `passcode`, `pin`,
    `secret`, `token`, `otp`) and every value under `extras` and `uri` are replaced by their length
    only (no hash), including inside nested dicts and lists; binary values become their size, and
    long strings are truncated. The log file is created owner-only (`0600`).
  - `off` — nothing is written.
  - `full` — raw parameter values, including typed text (except `nerve_type` text sent with `secret=true`, which is length-only in every mode). Opt in only on a machine you trust.
- **The daemon is loopback-only.** It binds `127.0.0.1`, rejects non-loopback Hosts (DNS
  rebinding) and any browser Origin, and accepts tool calls only as `application/json`. Every value
  that reaches a device shell is validated or shell-quoted where it is used. Details and how to
  report a vulnerability: [SECURITY.md](https://github.com/LincForge/nerve/blob/main/SECURITY.md).

## How this repo works

This is a release mirror of LINC's private repository. The code is built by LINC's AI software
factory under human review; each release lands as one commit. Issues are welcome; pull requests by
arrangement — see [CONTRIBUTING.md](https://github.com/LincForge/nerve/blob/main/CONTRIBUTING.md).

---

Built by [LINC Innovations](https://lincinnovations.com/audit?utm_source=github&utm_medium=readme&utm_campaign=nerve) — we diagnose flaky device test suites.

Licensed under the [Apache License 2.0](https://github.com/LincForge/nerve/blob/main/LICENSE). Bundles scrcpy-server (Apache-2.0) — see [NOTICE](https://github.com/LincForge/nerve/blob/main/NOTICE).
