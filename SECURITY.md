# Security

## Report a vulnerability

Use GitHub's **private vulnerability reporting** on this repository (Security → Report a
vulnerability). Please do not open a public issue for security problems. We aim to
acknowledge within 3 business days.

## Threat model in one paragraph

nerve runs on your machine and drives devices you connect. The optional HTTP daemon binds
127.0.0.1 only, rejects requests whose Host is not a loopback name (DNS rebinding), rejects
any request carrying a browser Origin, rejects requests a browser marks as cross-site via
Fetch Metadata (`Sec-Fetch-Site` cross-site/same-site or `Sec-Fetch-Mode` no-cors/navigate,
so an `<img>` or link on a web page cannot trigger discovery or log streams), and accepts
tool calls only as `application/json` (no cross-origin "simple" requests). Every value that reaches a device shell is validated
(identifiers) or shell-quoted (free text) at the point of use. Configure extra
Hosts/Origins with `NERVE_ALLOWED_HOSTS` / `NERVE_ALLOWED_ORIGINS`. The daemon has no
authentication: it trusts every local process that can reach it on loopback. To keep nerve
away from devices it should not touch, export `ANDROID_ADB_SERVER_PORT` (or `ADB_SERVER_SOCKET`) to point it at an adb server
that sees only the devices you intend, such as an emulator-only host or container; nerve's
client and its adb self-heal then use that server only.

## Independent review

### 2026-10-02 pre-release review

An adversarial security review conducted on 2026-10-02 evaluated the pre-release codebase. The review used a separate Claude session with no access to fix history, operating entirely in-process against fakes with no live device contact.

Findings: 1 High, 1 Medium, 4 Low, all fixed before release:
- **Command injection / argument splitting (High):** Unanchored allowlist regular expressions allowed trailing newlines to split device-shell commands in `am start`. Fixed by anchoring all allowlist patterns with `\A...\Z`, enforcing `re.fullmatch`, and shell-quoting tokens interpolated into device shell strings.
- **Emulator console command injection (Medium):** Unvalidated sensor type parameters reached the emulator console sink. Fixed by enforcing a strict sensor name allowlist and validating parameters before console I/O.
- **Cross-site request exposure (Low):** Browser fetches without CORS could reach discovery and logcat endpoints. Fixed by inspecting Fetch Metadata headers and rejecting cross-site and same-site browser-initiated requests.
- **Temporary file and symlink handling (Low):** Shared paths in temporary directories could follow symlinks. Fixed by removing fixed temporary paths at import, using private temporary directories with `mkdtemp()`, and cleaning up temporary files.
- **Sensitive data exposure in usage logs (Low):** Short hash prefixes in redacted text were vulnerable to brute-force reversal, and nested attributes could leak. Fixed by replacing digest prefixes with length-only redaction, extending redaction to URI and extras structures, and restricting log file permissions to `0600`.
- **Parameter expansion in shell commands (Low):** Fixed by strictly quoting parameters passed to device shells.

Candidate wheel SHA-256: `bfde80de08fb2312611f5b5847eb59c75c841da456b6c9bcfde5c19aa861b47e`.

### 2026-10-10 post-review surface review

A second review on 2026-10-10 examined the surface added after the 2026-10-02 review. Testing was conducted in-process against fakes with no live device contact across seven groups: (a) action log, (b) guarded sessions, (c) replay, (d) frame extraction (`nerve_extract_frames`), (e) network connectivity probe (`nerve_network` / `ensure_ready`), (f) adb server selection, and (g) usage log secret handling.

Results: 37 checks performed; 28 PASS, 9 FAIL (0 High, 1 Medium, 8 Low). All 9 findings were resolved with regression tests:
- **Symlink traversal in frame extraction (Medium):** Extraction followed pre-planted symlinks in output directories. Fixed by opening output frame files with `O_NOFOLLOW`.
- **Action log blocking on FIFO (Low):** Opening a FIFO named `*.jsonl` could block the worker. Fixed by opening with `O_NONBLOCK` and verifying regular file type.
- **Guarded iOS URI launch escape (Low):** Guarded deep-link launches could navigate outside the guarded application on platforms that do not restrict URIs to bundle IDs. Fixed by refusing unconfined URI launches in guarded sessions.
- **Replay gesture budget overflow (Low):** Replaying gestures with long durations could outlast the execution budget. Fixed by checking gesture duration against remaining time budget before dispatch.
- **Unbounded frame extraction inputs (Low):** Frame extraction lacked bounds on timestamp count and frame dimensions. Fixed by capping timestamp count to 500 and frame size to 7680x4320 pixels.
- **Non-loopback adb server contact in background tasks (Low):** The watchdog probe and self-heal routines invoked `adb` against non-loopback server sockets before validating configuration. Fixed by checking server validity before running adb subprocesses.
- **Cleartext logging of rejected secrets (Low):** Type-rejected tool calls with non-boolean secret flags logged text in cleartext. Fixed by treating non-boolean secret arguments as secret during log sanitization.

### Release candidate verification

The reviewed v0.1.0 candidate contains all fixes above:
- Source: private linc-nerve commit `ff33e7b` (tree `04abc4c39af9…`). Commits after it and before the v0.1.0 tag change only this file and tests, not the package.
- Reviewed candidate wheel SHA-256: `931663ad655060222760af5c3e98d5de9350cdd5045ee43ba47dce1786054c81`
- Public release: tag `v0.1.0` of LincForge/nerve, commit `547786321703b7c8640e3f8a610e34f561fe7c9e` (tree `321e50e2aba0e668107b364a94852ee7796b42c1`), exported from private commit `c40f9be`. Its `src` tree (`d529522e15dd…`) is the same as the reviewed candidate's at `ff33e7b`.
- Published wheel SHA-256 (PyPI, `linc_nerve-0.1.0-py3-none-any.whl`): `747798cf8c869e4e0f6d49426faada97221cfd55e70fef540afa3fe2d949ca19`
- The published wheel's SHA-256 differs from the candidate's even though its contents are the same, because a wheel records each file's timestamp and the published build's timestamps differ from the candidate's.
- Verify: `pip download linc-nerve==0.1.0 --no-deps -d .` then `shasum -a 256 linc_nerve-0.1.0-py3-none-any.whl`, and compare the result with the published hash above.

### 2026-11-19 v0.3.0 pre-release review

The changes since v0.2.0 (the `mcp` 2.3 migration, personal-device refusal on network transports, engagement mode for CLI calls, fleet-registry hardening, and input and lease bounds) were reviewed adversarially before the v0.3.0 release in two rounds (an initial review of about 150 attempts and a post-fix verification of about 210), each by a separate session with no access to fix history. Both rounds ran against an installed candidate wheel with an isolated home directory, recording shims for `adb`, `xcrun` and `devicectl`, proxies pointed at a closed port, and a guard that refused connections to the host's live daemons; no device was contacted. The findings were fixed before release, except the items listed as known issues in the `[0.3.0]` section of the [changelog](https://github.com/LincForge/nerve/blob/main/CHANGELOG.md):
- **Personal device reachable over a second transport (High, first round):** a personal device registered by its USB serial could be driven over its Wi-Fi adb transport (`ip:port` or mDNS), which carries a different serial. Fixed by reading `ro.serialno` for every unmapped network transport and refusing when a personal device is registered and the identity cannot be verified. Follow-up fixes make every enforcing decision read it afresh (adb reuses transport ids after a server restart) and recognise `tcp:`/`udp:`, bare IP, IPv6 and `.local` transport forms.
- **Fleet registry failed open (Medium, both rounds):** a malformed line, deep nesting or a wrong type in `device-fleet.yaml` could drop personal entries silently, and a malformed one-line entry registered only its first identifier. Fixed by parsing each section independently, treating any `personal` value other than false as personal, registering every identifier of an entry that cannot be parsed, and warning on every decision while the registry is unreadable or degraded.
- **Engagement mode did not cover daemon-served CLI calls (Medium, first round):** under `NERVE_ENGAGEMENT`, CLI commands were sent to a shared daemon that does not run under the caller's engagement. Fixed by running CLI tools in-process whenever engagement mode is set.
- **CLI daemon calls went through `HTTP_PROXY` (Medium, both rounds):** text sent with `--secret` reached an ambient proxy. Fixed in `linc-cli-kit` 0.3.1, which this release requires; the `nerve doctor` daemon probe also ignores proxy variables now.
- **Lease and input bounds (Medium and Low, first round):** negative, huge or non-finite reservation timeouts, owner pids and input durations were accepted, and a failed call-log write turned a completed action into an error. Fixed by validating them at the tool boundary and tolerating call-log write errors.
- **Shipped source hygiene (Low, first round):** real-looking device serials and host-specific comments in the shipped source. Removed, with a test that scans the exported files.

After `v0.3.0` is published, this section names the public `v0.3.0` tag, its commit, and the SHA-256 of the wheel PyPI serves. To check that hash: `pip download linc-nerve==0.3.0 --no-deps -d .` then `shasum -a 256 linc_nerve-0.3.0-py3-none-any.whl`.

