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
- The wheel published on PyPI will have a different SHA-256 even though its contents are the same, because a wheel records each file's timestamp and the published build's timestamps differ from the candidate's.
- After v0.1.0 is published, this section names the public v0.1.0 tag, its commit, and the SHA-256 of the wheel PyPI serves. To check that hash: `pip download linc-nerve==0.1.0 --no-deps -d .` then `shasum -a 256 linc_nerve-0.1.0-py3-none-any.whl`.

