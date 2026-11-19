# linc-nerve

Field-tested device control for AI coding agents — the self-healing tricks we learned keeping a
physical Android/iOS bench alive, as an MCP server and a plain CLI.

nerve exposes <!-- NERVE:TOOL-COUNT -->49<!-- /NERVE:TOOL-COUNT --> tools — screenshots, the UI
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
  ! device-fleet: registry absent (/home/you/.linc/device-fleet.yaml); engagement=off; refused=0
      hint: Create /home/you/.linc/device-fleet.yaml listing personal devices with personal: true.

9 checks: 0 failed, 5 warned
```

**2. Find the emulator.**

```bash
uvx linc-nerve discover
```

```text
{"devices": [{"serial": "emulator-5554", "platform": "android", "device_type": "emulator", …, "state": "online"}], "count": 1, "android": 1, "ios": 0}
```

With several devices attached, `nerve discover` lists all of them in the `"devices"` array; pass
the emulator's serial to the later commands.

**3. Read the screen** — foreground app, element tree and a screenshot path in one call. Your
launcher package and element count depend on the system image, and the screenshot lands in your
temp directory (`$TMPDIR` on macOS, not `/tmp`) as `nerve-output-<random>/<serial>_<NNNN>_<tool>.png`:

```bash
uvx linc-nerve state --device emulator-5554
```

```text
{"device": "emulator-5554", "platform": "android", "foreground": {"package": "com.google.android.apps.nexuslauncher", …}, "element_count": 31, "elements": [ … ], "screenshot": "<temp dir>/nerve-output-<random>/emulator-5554_0001_state.png", …}
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
before the command (`nerve --human discover`, not `nerve discover --human`). `nerve --version`
prints the installed package version, and `nerve schema --json` prints the full command tree.

Note on argument style: the six hand-written CLI overrides take positional arguments
(`nerve screenshot <serial>`, `nerve elements <serial>`, `nerve tap <serial> <x> <y>`,
`nerve launch <serial> <package>`, `nerve kill <serial> <package>`, and zero-argument
`nerve discover`), whereas generated tool commands take `--device <serial>` (for example,
`nerve state --device <serial>` or `nerve logcat --device <serial>`). Run `nerve <command> --help`
or `nerve schema --json` to inspect a command's options.

| Exit code | Meaning | Error codes / examples |
|-----------|---------|------------------------|
| `0` | Success | Command completed, or `nerve doctor` passed with no failed checks (warnings allowed) |
| `1` | Tool or runtime error | `TOOL_ERROR`, `DEVICE_RESERVED`, `DAEMON_UNAVAILABLE`, `UPSTREAM_TIMEOUT`, `DAEMON_ERROR`, or a failed `nerve doctor` check |
| `2` | Usage, validation, or bad-input error | Invalid CLI flags/args (`USAGE_ERROR`), `VALIDATION_ERROR`, `UNSUPPORTED`, or unknown device serial (`DEVICE_NOT_FOUND`) |

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

## Visual grounding (optional, Meta SAM 3.1)

`nerve_ground` and `nerve_tap(prompt=...)` find controls that the UI tree cannot label, such as
custom canvases or unlabelled icons. They do this by sending the current screenshot to Meta's
`sam-3.1` model. Grounding is **off by default**, and it needs three things:

```bash
pip install 'linc-nerve[grounding]'   # adds meta-sam-parser
export NERVE_GROUNDING=1              # opt in: screenshots are uploaded to api.meta.ai
export META_AI_API_KEY=...            # or NERVE_KEYCHAIN_SERVICE=<macOS Keychain service>
```

- **License:** `meta-sam-parser` is distributed under Meta's
  [SAM License](https://github.com/meta-models/meta-sam), not Apache-2.0. It restricts some fields
  of use, including ITAR-controlled, military and weapons applications. Read it before you install
  the extra. Core nerve never imports it.
- **Privacy:** never enable grounding on a device that shows personal data.
- **Tap safety:** a prompt tap fires only on one clear match, meaning confidence ≥ 0.5 and at
  least 0.1 ahead of the next match on a known screen size. Otherwise it taps nothing and returns
  the candidates.

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
| `LINC_NERVE_DAEMON` | `http://127.0.0.1:8111` | Daemon URL that CLI commands call before falling back to in-process (ignored while `NERVE_ENGAGEMENT` is active, when CLI commands run in-process) |
| `ANDROID_ADB_SERVER_PORT` / `ADB_SERVER_SOCKET` | `127.0.0.1:5037` | adb server nerve talks to (`ADB_SERVER_SOCKET` as `tcp:<port>` or `tcp:<host>:<port>` wins; loopback hosts only) — isolates nerve from the host's devices |
| `NERVE_ALLOWED_HOSTS` / `NERVE_ALLOWED_ORIGINS` | *(none)* | Extra Hosts / browser Origins the daemon accepts |
| `NERVE_GROUNDING` | *(off)* | `1` enables SAM 3.1 visual grounding (`nerve_ground`, `nerve_tap(prompt=...)`, `visual_grounding_hint`); needs the `[grounding]` extra |
| `META_AI_API_KEY` | *(none)* | Meta API key for grounding (`META_API_KEY` also accepted) |
| `NERVE_KEYCHAIN_SERVICE` | *(none)* | macOS Keychain service to read the Meta API key from when it is not in the environment |
| `NERVE_OUTPUT_KEEP` | `200` | Maximum number of output files retained per process `nerve-output-*` temp directory (`0` or invalid falls back to `200`) |
| `NERVE_OUTPUT_MAX_AGE_S` | `86400` | Maximum age in seconds (`24h` default; `0` or invalid falls back to `86400`) before an idle `nerve-output-*` directory is swept on the next process's first output |
| `NERVE_ARTIFACTS_ROOT` | *(unset)* | Existing, writable absolute directory with no symlink in any path component, confining all recordings, outputs, action logs, transient scratch temp files, call log, and thread dumps under one private (`0700`/`0600`) root |
| `NERVE_ENGAGEMENT` | *(off)* | Engagement identifier matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; when non-empty (even if invalid), requires a valid `NERVE_ARTIFACTS_ROOT` or refuses all writers with `error_category: "engagement_misconfigured"` |
| `NERVE_ALLOW_PERSONAL_SERIALS` | *(unset)* | Comma-separated serials temporarily permitted to bypass `personal: true` in `device-fleet.yaml` while engagement mode is OFF (completely ignored when `NERVE_ENGAGEMENT` is active) |

### Output files

Screenshots (`nerve_screenshot`, `nerve_stream_screenshot`, `nerve_navigate_screenshot`), state/diff captures (`nerve_state`, `nerve_diff`), and large `nerve_logcat` dumps are written to a per-process private (`0700`) directory `<temp dir>/nerve-output-<random>/<serial>_<NNNN>_<tool>.<ext>` (under `$TMPDIR` on macOS, `/tmp` on Linux), created lazily on first output and kept on process exit so callers can read returned paths after a CLI invocation finishes:

- **Retention cap:** Each process directory keeps at most `NERVE_OUTPUT_KEEP` (default `200`) `<serial>_<NNNN>_<tool>.<ext>` files, pruning the oldest (by counter, then mtime) before writing a new one.
- **Owner lock:** Each process holds an exclusive `flock` on `<dir>/.nerve-output.lock` for its lifetime (the OS drops it when the process exits). A directory whose lock is held — or whose lock state cannot be read — is never removed by the sweep or by `nerve cleanup`, however long its process (an idle daemon or MCP server) has gone without writing. If a process's directory is deleted anyway, nerve creates a fresh one on the next output.
- **Stale sweep:** On the first output directory creation in a process, nerve sweeps `<temp dir>/nerve-output-*` directories owned by the current UID (never following symlinks) that no live process holds and whose newest entry is older than `NERVE_OUTPUT_MAX_AGE_S` seconds (default `86400`). Set `NERVE_OUTPUT_SWEEP=0` to disable it (it is also off under pytest unless `NERVE_OUTPUT_SWEEP=1`).
- **On-demand cleanup:** `nerve cleanup` removes all current-UID, non-symlink `nerve-output-*` directories in the temp root except those held by a running nerve process and those modified in the last 60 seconds (`nerve cleanup --all` removes the recently modified ones too, never a live process's), printing JSON `{"removed": [...], "skipped": [...], "bytes_freed": N}` (or text with `nerve --human cleanup`).

### Engagement mode and the artifacts root

Set `NERVE_ARTIFACTS_ROOT=/path/to/vault` (and optionally `NERVE_ENGAGEMENT=<id>`) to keep every customer-engagement artifact under one pre-created, owner-only root instead of `~/.linc` and the system temp dir:

| Artifact | Location under `$NERVE_ARTIFACTS_ROOT` | Default when unset | Mode | Notes |
|---|---|---|---|---|
| Recordings & extracted frames | `recordings/` | `$LINC_HOME/nerve/recordings` | Dir `0700`, frames `0600` | Android guest/emulator, iOS simulator MP4s, `nerve_extract_frames` (`out_dir` confined under root) |
| Output spills | `output/nerve-output-*/` | `$TMPDIR/nerve-output-*/` | Dirs `0700`, files `0600` | Screenshots, `.agent.png`, state/diff/grounded captures, spilled logcat; `nerve cleanup` and stale sweep stay inside `output/` |
| Action logs | `actionlogs/` | `$LINC_HOME/nerve/actionlogs` | Dir `0700`, JSONL `0600` | `nerve_actionlog_start` / `nerve_replay`; symlinks/`..` escapes and external `NERVE_ACTIONLOG_ROOTS` are refused |
| Transient scratch files | `tmp/` | System `$TMPDIR` | Dir `0700`, files `0600` | Ephemeral `simctl` screenshot PNGs and `devicectl` JSON temp files (unlinked immediately) |
| MCP call log | `mcp-calls.jsonl` | `$LINC_HOME/mcp-calls.jsonl` | File `0600` | Usage/audit log (`NERVE_USAGE_LOG`) |
| Thread dump (optional) | `nerve_threads.txt` | `$LINC_HOME/nerve/nerve_threads.txt` | File `0600` | Written only when `NERVE_THREAD_DUMP` is set |

`NERVE_ARTIFACTS_ROOT` must already exist as a writable directory (absolute path, no `..`, no control characters or trailing newlines) and must be **canonical**: no component of the path may be a symlink, leaf or ancestor. nerve compares `realpath(root)` with `normpath(root)` and refuses any mismatch, so a symlinked vault is refused however it is spelled (`/Volumes/vault-link`, `/Volumes/vault-link/`, `/Volumes/vault-link/.`, or `/Volumes/vault-link/acme`). Trailing separators on a real directory are fine (`/Volumes/vault/` is used as `/Volumes/vault`); a leading `//` is refused. This deliberately fails closed on OS-level ancestor links too: on macOS `/tmp` → `/private/tmp` and `/var` → `/private/var`, so spell such a root by its resolved path (`/private/tmp/...`). Mount points such as `/Volumes/<name>` are not symlinks and are accepted. When `NERVE_ENGAGEMENT` is non-empty (must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; invalid values keep engagement enabled and fail validation), any misconfiguration fails closed with `error_category: "engagement_misconfigured"` and writes nothing to `HOME`, `LINC_HOME`, or `TMPDIR`.

- **Lease housekeeping stays available:** `nerve_release` and `nerve_pool` write no engagement artifacts, so they keep working on a misconfigured engagement in a process that has already initialised its device manager (for example, the vault unmounted mid-session) and a reservation is never stranded. In a fresh, misconfigured process they are refused like every other tool, so `devices.db` is never created there.
- **`nerve doctor`** reports a bad root as one failed `artifacts-root` check (exit 1) instead of erroring out, and `nerve doctor --fix` plans no `mkdir` while the root is misconfigured. With a valid root the check is `ok`; with neither variable set it is omitted.
- **`NERVE_ACTIONLOG_ROOTS`** entries outside the artifacts root are dropped and logged as an `actionlog_root_outside_artifacts_root` warning (the remaining roots stay confined).
- **Every process resolves the root from its own environment.** When `NERVE_ENGAGEMENT` is active, CLI commands run in-process under the caller's exported environment and never contact a running `:8111` daemon (daemon-only stateful tools refuse with `DAEMON_UNAVAILABLE`; use an MCP session such as `nerve bellows` under that environment instead). When `NERVE_ENGAGEMENT` is unset and only `NERVE_ARTIFACTS_ROOT` is set, CLI calls still route to a running daemon if one is listening — so either launch that daemon with `NERVE_ARTIFACTS_ROOT` in its own environment or point `LINC_NERVE_DAEMON` at an unused port so CLI calls run in-process.

#### Personal devices

nerve reads `$LINC_HOME/device-fleet.yaml` (reloaded automatically on mtime or size change) to separate personal hardware from shared lab/bench devices using the three engagement/fleet variables: `NERVE_ENGAGEMENT`, `NERVE_ARTIFACTS_ROOT`, and `NERVE_ALLOW_PERSONAL_SERIALS`.

- **Personal devices (`personal: true`) are refused on every transport once identity is resolved:** A device is matched (case-insensitively) against the `serial`, `id`, or `hardware_id` of any entry in `$LINC_HOME/device-fleet.yaml` (top-level `devices:`, `ios:`, `garmin:`, or nested `phones:` / `watches:` sections parsed independently, duplicate keys merged, and any non-boolean-`false` `personal` value treated as personal). For mDNS Wi-Fi transports (`adb-<SERIAL>-<suffix>._adb._tcp` or `._adb-tls-connect._tcp`), nerve extracts the embedded hardware serial; for unmapped network transports such as `host:port` or `ip:port`, when `NERVE_ENGAGEMENT` is active or the registry has any `personal: true` entry, nerve resolves `ro.serialno` (cached for 30 s) before deciding. A matching `personal: true` device is refused on every transport before ADB/controller work, reservation writes, or recording starts (`error_category: "personal_device"`). Discover (`nerve_discover` / `GET /devices`) keeps listing it with `usable: false` and a `refusal` string while leaving usable devices untouched.
- **Network transports with unknown identity are refused while personal entries exist:** When `$LINC_HOME/device-fleet.yaml` contains any `personal: true` entry (or is `unreadable` or `degraded`), any network transport whose hardware identity cannot be verified (for example, `ip:port` where `ro.serialno` is missing, placeholder, or times out) is refused fail-closed (`error_category: "personal_device"`). USB devices remain usable outside engagement mode unless matched as `personal: true`.
- **Unreadable or degraded registry warns:** When `$LINC_HOME/device-fleet.yaml` is `unreadable` (unparseable YAML, recovery budget exceeded, non-regular file, or > 1 MiB) or `degraded` (rebuilt via line-level recovery or holding malformed entries without usable identifiers), every device decision logs a structured warning (`fleet_registry_unreadable` or `fleet_registry_degraded`), network transports are refused across all modes, and USB devices stay usable outside engagement mode.
- **Fail-closed gating in engagement mode:** When `NERVE_ENGAGEMENT` is active (valid non-empty ID, or any invalid non-empty value which is treated as on-and-misconfigured), a device is usable **only** when `$LINC_HOME/device-fleet.yaml` is not `absent` or `unreadable` and explicitly registers one of the device's identifiers with boolean `personal: false` (network transports remain refused when `degraded`). Absent, unreadable, unregistered, unverified, and non-boolean `personal` values (`"false"`, `0`, `null`) are refused (`error_category: "engagement_misconfigured"`, or `"personal_device"` for `personal: true` / degraded network transports).
- **`NERVE_ALLOW_PERSONAL_SERIALS` override:** Outside an engagement (`NERVE_ENGAGEMENT` unset or `""`), listing a resolved identifier in `NERVE_ALLOW_PERSONAL_SERIALS` permits temporary access to a `personal: true` device. During an engagement (`NERVE_ENGAGEMENT` set to any non-empty value), `NERVE_ALLOW_PERSONAL_SERIALS` is ignored and personal devices remain blocked.
- **`nerve doctor`** includes a `device-fleet` check reporting the registry state (`loaded`, `degraded`, `absent`, or `unreadable`), engagement state (`off`, `on`, or `misconfigured`), and count of refused attached adb and iOS devices (warning when engagement is off and registry is absent/unreadable/degraded; failing when engagement mode cannot fail closed or is misconfigured).

**Explicitly exempt (never placed inside `$NERVE_ARTIFACTS_ROOT`):**
- **`devices.db` (`~/.local/share/linc-nerve/devices.db`, or `$LINC_HOME/.local/share/linc-nerve/devices.db` only when `LINC_HOME` is set):** Operational reservation and transport lease metadata; contains no captured UI/media data and is never initialized when engagement validation fails.
- **WDA build caches (`~/.linc/wda/build/`, `~/.linc/wda/build-device/`, `WDA_PROJECT_DIR`):** Shared Xcode WebDriverAgent runner builds and temporary `.xctestrun` manifests stored beside the build tree rather than per-engagement artifacts.
- **`docs_gen` (`python -m linc_nerve.docs_gen`):** Offline developer tool that updates `README.md` and `LINC.md` in the source checkout; never invoked by runtime device tools.

### HTTP daemon (optional)

`nerve daemon` (or `nerve bellows`, which also serves MCP over stdio) runs an HTTP server on
`127.0.0.1:8111`: `GET /health`, `GET /devices`, `POST /tools/<tool_name>` with a JSON body, and
`GET /stream?device=<serial>` for a server-sent-events log stream. CLI commands use the daemon when
one is running and otherwise run the tool in-process — except when `NERVE_ENGAGEMENT` is active,
in which case CLI commands always run in-process without contacting the daemon. A few stateful tools
(recording, streaming, reservations, `diff`, `session-stats`) need the daemon, and say so instead
of answering wrongly.

### Coexisting with scrcpy

On Android, nerve's fast screenshot stream pushes its bundled `scrcpy-server-v3.3.4` binary to
`/data/local/tmp/nerve-scrcpy-server-v3.3.4.jar` (verifying both byte size and `sha256sum`) and
opens abstract sockets in the reserved `0x6e65xxxx` `scid` range (`localabstract:scrcpy_6e65xxxx`).
Orphan process cleanup and `adb forward` sweeps match only that nerve-owned jar path and `scid`
prefix, so nerve never overwrites `/data/local/tmp/scrcpy-server.jar`, kills, or unforwards an
external `scrcpy` session (such as `scrcpy --record`) running on the same device.

<!-- NERVE:TOOLS-AUTO-START — generated by `python -m linc_nerve.docs_gen`, do not edit -->
## MCP Tools (49)

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
| `nerve_ground` | device | Locate UI controls visually by natural-language concept prompt using Meta SAM 3.1 |
| `nerve_state` | device | Capture full device state in one call (screenshot + UI elements + foreground) |

### Navigation

| Tool | Type | Description |
|------|------|-------------|
| `nerve_navigate` | device | Navigate to a named screen of a REGISTERED app (route registry) by its deep link |
| `nerve_navigate_screenshot` | device | Navigate to a screen and capture a screenshot after the UI settles |

### Input

| Tool | Type | Description |
|------|------|-------------|
| `nerve_density` | device | Read display density — dpi and the dp-to-px multiplier — for a device |
| `nerve_key` | device | Press a hardware/system key |
| `nerve_long_press` | device | Long press at screen coordinates |
| `nerve_swipe` | device | Two-point gesture from one xy coordinate to another — swipe, drag, or scroll |
| `nerve_tap` | device | Tap a single point on the device display at known x/y coordinates or by visual prompt |
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

## Related projects

| Package | Role |
|---------|------|
| [`linc-crucible`](https://github.com/LincForge/crucible) | Quality gates, build-and-deploy routing, and visual/a11y baselines (delegates device I/O to nerve) |
| [`linc-spectra`](https://github.com/LincForge/spectra) | Runtime debugger (Android JDWP via `kotlin-debug-adapter` or zero-config `jdb`; iOS Simulator `lldb-dap`) |
| [`mobile-agent-bench`](https://github.com/LincForge/mobile-agent-bench) | Pre-registered 600-cell benchmark comparing `nerve + crucible + spectra` against Maestro, `mobile-mcp`, and `agent-device` ([lincforge.com](https://lincforge.com)) |

## How this repo works

This is a release mirror of LINC's private repository. The code is built by LINC's AI software
factory under human review; each release lands as one commit. Issues are welcome; pull requests by
arrangement — see [CONTRIBUTING.md](https://github.com/LincForge/nerve/blob/main/CONTRIBUTING.md).

---

Built by [LINC Innovations](https://lincinnovations.com/audit?utm_source=github&utm_medium=readme&utm_campaign=nerve) — we diagnose flaky device test suites.

Licensed under the [Apache License 2.0](https://github.com/LincForge/nerve/blob/main/LICENSE). Bundles scrcpy-server (Apache-2.0) — see [NOTICE](https://github.com/LincForge/nerve/blob/main/NOTICE).
