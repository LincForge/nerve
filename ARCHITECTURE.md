# Architecture — linc-nerve

## Overview

linc-nerve is a device-control layer for AI agents, providing unified access to Android and iOS devices through two transports: an MCP stdio server (for AI agent integration) and an optional HTTP daemon on `127.0.0.1:8111` (for programmatic access). Every tool is available through both transports and as a `nerve <tool>` CLI command; the CLI uses the daemon when one is running and otherwise runs the tool in-process.

Layered design: MCP tool functions (thin wrappers) call core controllers (platform-specific logic), which talk to devices via persistent connections (adbutils sockets, scrcpy binary protocol, WDA HTTP, simctl/devicectl subprocesses).

## Component Map

```
                    ┌─────────────────┐
                    │   MCP Clients   │  (Claude Code, AI agents)
                    └────────┬────────┘
                             │ stdio (JSON-RPC)
                    ┌────────▼────────┐
                    │ stdio_buffered  │  Patched transport (buffer=32)
                    └────────┬────────┘
         ┌───────────────────▼───────────────────────┐
         │             FastMCP Server                 │
         │  @device_tool / @global_tool decorators    │
         │  Per-device semaphore + thread dispatch    │
         └───────────────────┬───────────────────────┘
                             │
         ┌───────────────────▼───────────────────────┐
         │      Tool Modules (mcp/*_tools.py)         │
         │  screenshot, ui, input, app, discovery,    │
         │  diagnostic, emulator, reservation,        │
         │  recording, sse_server                     │
         └───────────────────┬───────────────────────┘
                             │
         ┌───────────────────▼───────────────────────┐
         │          Core Controllers                  │
         │  AndroidController ─── IOSController       │
         │       │                    │               │
         │  ScrcpyManager        WDAManager           │
         │  + ControlSocket      + WDASession         │
         └───────────────────┬───────────────────────┘
                             │
         ┌───────────────────▼───────────────────────┐
         │           Device Layer                     │
         │  adbutils socket │ scrcpy binary │ simctl  │
         │                  │ protocol      │ WDA HTTP│
         └───────────────────────────────────────────┘

    ┌──────────────────────────────┐
    │  HTTP Daemon (:8111)          │
    │  POST /tools/<name>           │
    │  GET /health                  │
    │  GET /stream (SSE logs)       │
    └──────────────────────────────┘

    ┌──────────────────────────────┐
    │  Support Systems              │
    │  StateSessionMgr (delta det.) │
    │  DeviceManager (reservations) │
    │  MetricsCollector (ring buf)  │
    │  EmulatorManager (AVD life.)  │
    │  Observability (JSONL logger) │
    └──────────────────────────────┘
```

## Key Abstractions

| Class | File | Responsibility |
|-------|------|----------------|
| `NerveDevice` | `core/device.py` | Discovered device data (serial, platform, type, model, OS) |
| `AndroidController` | `core/android.py` | Persistent ADB socket, scrcpy, circuit breaker, retry |
| `IOSController` | `core/ios.py` | simctl (sims) + devicectl (physical) + WDA routing |
| `WDAManager` | `core/wda.py` | WDA process lifecycle: build, launch, health, session |
| `ScrcpyManager` | `core/scrcpy.py` | H264 stream lifecycle: push binary, forward port, decode via PyAV |
| `StateSessionManager` | `core/state_engine.py` | Per-device state tracking for delta detection |
| `DeviceManager` | `core/device_manager.py` | Cross-process device reservation via SQLite WAL |
| `DeviceCircuitBreaker` | `core/android.py` | 3 failures → 30s cooldown per device |
| `MetricsCollector` | `core/metrics.py` | Ring buffer (500 entries) with p50/p95/p99 stats |

## Data Flow

### Screenshot (Android, scrcpy path)

```
nerve_screenshot(device, annotate=true)
  → _resolve_device(serial) → NerveDevice
  → _check_guard(serial) → reservation check
  → AndroidController.screenshot(serial)
    → ScrcpyManager: grab latest decoded PIL frame
    → _downscale_if_needed() (cap at 768KB)
  → annotate_screenshot(png_bytes, elements) → numbered bounding boxes
  → write to <tmp>/nerve-output-<random>/ (private mkdtemp, created on first use)
  → return {screenshot: path, elements: [...]}
```

### Delta Detection (nerve_diff)

```
nerve_diff(device)
  → StateSessionManager → DeviceStateSession
  → screenshot → perceptual hash (64-bit, 8x8 grayscale)
  → hamming distance > 5 bits → changed
  → element diff by identity key (resource_id+bounds)
  → return {changes: {screenshot, elements, foreground, orientation}, any_change}
```

## File Guide

| Path | Purpose |
|------|---------|
| `mcp/_core.py` | FastMCP server, singletons, `@device_tool`/`@global_tool` decorators, 90s timeout |
| `mcp/server.py` | Entry point, imports all tool modules, signal handlers |
| `mcp/screenshot_tools.py` | `nerve_screenshot`, stream tools |
| `mcp/ui_tools.py` | `nerve_elements`, `nerve_navigate`, `nerve_state`, `nerve_diff` |
| `mcp/input_tools.py` | `nerve_tap`, `nerve_swipe`, `nerve_type`, `nerve_key` |
| `mcp/app_tools.py` | `nerve_install`, `nerve_launch`, `nerve_kill`, `nerve_apps` |
| `mcp/diagnostic_tools.py` | `nerve_logcat`, `nerve_inspect`, `nerve_metrics` |
| `mcp/emulator_tools.py` | `nerve_emulator_list/start/stop`, `nerve_inject_sensor` |
| `mcp/reservation_tools.py` | `nerve_reserve`, `nerve_release` |
| `mcp/recording_tools.py` | `nerve_record_start/stop/fixed` |
| `mcp/sse_server.py` | HTTP daemon: SSE streaming, POST /tools/*, GET /health |
| `mcp/request_guard.py` | Daemon request guard: loopback Host allowlist, browser-Origin rejection, JSON-only POST |
| `mcp/stdio_buffered.py` | Patched stdio transport fixing mcp SDK deadlocks |
| `core/android.py` | AndroidController: adbutils, scrcpy, circuit breaker |
| `core/ios.py` | IOSController: simctl, devicectl, WDA routing |
| `core/wda.py` | WDAManager + WDASession: lifecycle, HTTP client |
| `core/scrcpy.py` | H264 decode via PyAV, frame ring buffer |
| `core/state_engine.py` | Perceptual hash, element diff, stability polling |
| `core/device_manager.py` | SQLite WAL reservation DB, heartbeat, PID reaper |
| `core/emulator.py` | AVD/Simulator lifecycle management |
| `core/metrics.py` | Ring buffer metrics, tool counters |
| `core/annotator.py` | Screenshot annotation (numbered bounding boxes) |
| `core/observability.py` | JSONL usage log (`$LINC_HOME/mcp-calls.jsonl`, `NERVE_USAGE_LOG=off\|redacted\|full`, 10MB rotation) |
| `daemon/http_router.py` | HTTP routing for POST /tools/* |
| `daemon/polling.py` | Device polling loop |
| `daemon/wda_manager.py` | Daemon WDA lifecycle (auto-start, crash restart) |
| `cli/main.py` | Click CLI: every tool as a command, plus `bellows` (MCP) and `daemon` (HTTP) |
| `cli/doctor.py` | `nerve doctor`: environment diagnostics; daemon, WDA and `~/.linc` are optional (warn) |

## Design Decisions

| Decision | Chosen | Rejected | Rationale |
|----------|--------|----------|-----------|
| ADB connection | adbutils persistent socket | subprocess `adb shell` | 1-5ms vs 15-50ms; reusable connection |
| Screenshot capture | scrcpy H264 decode | `adb screencap -p` | 35-70ms vs 200-500ms; hardware-accelerated |
| Input injection | `adb shell input` (Android), WDA (iOS) | scrcpy control socket | scrcpy-control input was reverted over touch-coordinate scaling and stream-startup timeouts; the control-socket plumbing is kept for a future re-enable |
| iOS interaction | WebDriverAgent | Direct XCTest | Battle-tested, HTTP-based, works on sims+physical |
| iOS physical ops | devicectl | ios-deploy | Apple's official tool, no third-party deps |
| MCP transport | Patched buffered channels | Stock mcp SDK | mcp 1.26.0 zero-buffer deadlocks fixed |
| Reservation | SQLite WAL | File locks, Redis | No external deps; heartbeat + PID reaper |
| Delta detection | 64-bit perceptual hash | Pixel diff, SSIM | Fast (8x8 grayscale), robust to jitter |
| Stability polling | Hash polling (50ms, 300ms stable) | Fixed sleep | Adapts to actual UI speed |
| Concurrency | Per-device semaphore | Global lock | Serializes per-device, parallel across devices |

## Extension Points

1. **New MCP tool**: Add function in appropriate `mcp/*_tools.py`, decorate with `@device_tool`/`@global_tool`. Modules named `<name>_tools.py` are discovered automatically for both stdio and HTTP
2. **New platform**: Implement controller matching Android/IOSController interface, add to `_ensure_initialized()`
3. **New daemon route**: Add to `daemon/http_router.py` via `route_request`
4. **New crash patterns**: Add regex to `core/inspect_helpers.py`
5. **New deep links**: Add to `_ROUTE_REGISTRY` in `mcp/ui_tools.py`

## Dependencies

| Package | Purpose |
|---------|---------|
| `adbutils` >= 2.0 | Persistent ADB socket |
| `av` (PyAV) >= 14.0 | H264 frame decoding |
| `httpx` >= 0.27 | WDA HTTP client |
| `linc-device-kit` | Shared Platform/DeviceType enums |
| `mcp` >= 1.0.0 | FastMCP server |
| `pillow` >= 11.0 | Image processing |
| `structlog` >= 25.0 | Structured logging |
| scrcpy-server v3.3.4 | Vendored in `vendor/` |

## Failure Modes

| Failure | Detection | Recovery |
|---------|-----------|----------|
| ADB server crash | adbutils connection error | Auto-recreate client; after 3 failures restart ADB server |
| Per-device failure | Circuit breaker: 3 consecutive | 30s hard cooldown, HALF_OPEN probe on next call |
| scrcpy stream dies | Decode thread exits | Automatic fallback to screencap; restartable via stream_start |
| WDA crash | `proc.poll()` check | Auto-restart WDA |
| Device disconnect | Missing from discover() | Cache invalidated; operations return error |
| Tool call timeout | 90s queue.get timeout | Error returned; semaphore released |
| Reservation leak | PID dies or heartbeat stops | Background reaper auto-deletes |
| Large response | >100 lines logcat, >768KB screenshot | Auto-save to file, return path; auto-downscale |

## Concurrency Model

- **MCP tools**: async def wrappers delegating to threads via `anyio.to_thread.run_sync`
- **Per-device semaphore** (async, capacity=1): one tool per device at a time, parallel across devices
- **Thread dispatch**: daemon threads with 90s hard timeout via `queue.Queue.get(timeout=...)`
- **Cache lock**: non-blocking `acquire(blocking=False)` prevents thread pile-up during discovery
- **Reservation DB**: SQLite WAL, `busy_timeout=5000ms`, `threading.Lock` for local serialization
- **Heartbeat thread**: daemon thread updates `last_heartbeat` every 30s
- **scrcpy decode thread**: per-stream daemon thread, thread-safe via PIL Image reference swap

<!-- last verified: 2026-10-02 -->
