"""MCP tool call observability — JSONL logger.

Logs every MCP tool call with timing, params, and success/failure
to ~/.linc/mcp-calls.jsonl for usage analysis.
Parameters are PII-redacted for sensitive keys (text, password, passcode,
pin, secret, token, otp) and for keys whose every value is treated as secret
(extras, uri). Redaction is length-only — no hash, which would be reversible
for short secrets such as a 4-digit PIN. The log file is created 0600.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_DEFAULT_MAX_SIZE = 10 * 1024 * 1024  # 10 MB

_SENSITIVE_PARAM_KEYS = frozenset(
    {"text", "password", "passcode", "pin", "secret", "token", "otp"}
)

# Keys whose values are secret-bearing as a whole: intent extras routinely
# carry auth tokens, and deep-link URIs carry query tokens.
_REDACT_ALL_KEYS = frozenset({"extras", "uri"})


def linc_home() -> Path:
    """Resolve the LINC home directory ($LINC_HOME or ~/.linc)."""
    env = os.getenv("LINC_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".linc"


def usage_log_mode() -> str:
    """Return the usage log mode: 'off' | 'redacted' | 'full' from NERVE_USAGE_LOG."""
    val = (os.getenv("NERVE_USAGE_LOG") or "").strip().lower()
    if val == "off":
        return "off"
    if val == "full":
        return "full"
    return "redacted"


def _should_log() -> bool:
    """Return True if MCP tool call logging is enabled."""
    return usage_log_mode() != "off"


def _default_log_path() -> Path:
    """Resolve default mcp-calls log path lazily; honors LINC_HOME for tests."""
    return linc_home() / "mcp-calls.jsonl"


def _write_log_entry(log_path: Path, entry: dict, max_size_bytes: int) -> None:
    """Write one MCP tool call record if logging is enabled."""
    if not _should_log():
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > max_size_bytes:
        rotated = log_path.with_suffix(f".{int(time.time())}.jsonl")
        log_path.rename(rotated)
    with _open_private_append(log_path) as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _open_private_append(log_path: Path) -> Any:
    """Open *log_path* for append, owner-only (0600), never following a symlink.

    ``fchmod`` also tightens a pre-existing world-readable log from older builds.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(log_path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a")


def log_mcp_call(
    server: str,
    tool: str,
    params: dict,
    duration_ms: float,
    success: bool,
    error: str | None = None,
    log_path: Path | None = None,
    max_size_bytes: int = _DEFAULT_MAX_SIZE,
) -> None:
    """Append one MCP tool call record to the JSONL log."""
    if log_path is None:
        log_path = _default_log_path()
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": server,
        "tool": tool,
        "params": _safe_params(params),
        "duration_ms": round(duration_ms, 1),
        "success": success,
        "error": _scrub_error(error, params),
        "session_id": os.environ.get("FORGE_RUN_ID", "manual"),
    }
    _write_log_entry(log_path, entry, max_size_bytes)


def _scrub_error(error: str | None, params: dict) -> str | None:
    """Replace occurrences of secret text in error with <redacted>."""
    if error is None:
        return None
    if params.get("secret") is True:
        text = params.get("text")
        if isinstance(text, str) and text:
            return error.replace(text, "<redacted>")
    return error


def _redacted(value: str | bytes) -> str:
    """Length-only placeholder — deliberately no digest (reversible for short secrets)."""
    return f"<redacted len={len(value)}>"


def _redact_all(value: Any) -> Any:
    """Redact every leaf of *value*, preserving dict/list shape and dict keys."""
    if isinstance(value, dict):
        return {k: _redact_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_all(v) for v in value]
    if isinstance(value, (str, bytes)):
        return _redacted(value)
    return _redacted(str(value))


def _redacts_value(key: Any) -> bool:
    """True when every value under *key* must be redacted (case-insensitive)."""
    lowered = str(key).lower()
    return lowered in _SENSITIVE_PARAM_KEYS or lowered in _REDACT_ALL_KEYS


def _safe_value(key: Any, value: Any) -> Any:
    """Redact secret-bearing keys; otherwise sanitize the value."""
    if _redacts_value(key):
        return _redact_all(value)
    return _sanitize(value)


def _sanitize(value: Any) -> Any:
    """Recurse into dicts/lists, summarize bytes, and truncate long strings."""
    if isinstance(value, dict):
        return {k: _safe_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


def _redact_flagged_secrets(params: dict) -> dict:
    """In full mode, redact sensitive keys when secret is exactly True."""
    if params.get("secret") is not True:
        return dict(params)
    out = dict(params)
    for k, v in params.items():
        lowered = str(k).lower()
        if lowered in _SENSITIVE_PARAM_KEYS and lowered != "secret":
            out[k] = _redacted(v) if isinstance(v, (str, bytes)) else _redacted(str(v))
    return out


def _params_for_mode(params: dict) -> dict:
    """Sanitize parameters according to the current usage log mode."""
    if usage_log_mode() == "full":
        return _redact_flagged_secrets(params)
    return {k: _safe_value(k, v) for k, v in params.items()}


def _safe_params(params: dict) -> dict:
    """Redact large binary values, sensitive keys, and truncate long strings."""
    return _params_for_mode(params)
