"""Request guards for the :8111 HTTP daemon — applied before any routing.

The daemon binds loopback, but a loopback bind alone does not stop a browser
on the same machine:

- **Cross-origin simple POST.** Any web page can fire a ``text/plain`` POST
  at ``http://127.0.0.1:8111/tools/...`` with no CORS preflight; the tool
  runs even though the page can never read the answer.
- **DNS rebinding.** A page on ``evil.example`` rebinds its hostname to
  127.0.0.1 and then reads ``GET /devices`` as same-origin — the request
  reaches the daemon carrying ``Host: evil.example``.

``check_request`` closes both, rejecting (never sanitising):

1. ``Host`` must name a loopback host (``127.0.0.1`` / ``localhost`` /
   ``[::1]``, any port) or an entry from ``NERVE_ALLOWED_HOSTS``
   (comma-separated hostnames, ports optional and ignored). A request with
   NO Host header is a raw non-browser client — browsers always send one —
   so it passes.
2. Any ``Origin`` header — browsers attach one to cross-origin requests —
   must be in ``NERVE_ALLOWED_ORIGINS`` (comma-separated, empty by default).
3. Every POST must declare ``Content-Type: application/json``; a browser
   cannot send that cross-origin without a preflight this daemon never
   answers.

Browsers omit ``Origin`` on no-cors GETs, so ``<img src=.../stream?device=…>``
on any page used to reach device discovery and start a logcat stream
(2026-10-02 pre-release security review, finding L2). Fetch Metadata closes
it: a request whose ``Sec-Fetch-Site`` is ``cross-site``/``same-site`` or
whose ``Sec-Fetch-Mode`` is ``no-cors``/``navigate`` is rejected on every
route. Non-browser clients (curl, httpx, launchd probes) send no
``Sec-Fetch-*`` headers and are unaffected.

Header names are expected lower-cased (``sse_server._read_headers``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]"})

HOST_REJECTED = (403, "host not allowed")
ORIGIN_REJECTED = (403, "origin not allowed")
CONTENT_TYPE_REJECTED = (415, "content-type must be application/json")
CROSS_SITE_REJECTED = (403, "cross-site request")

_CROSS_SITE_FETCH_SITES: frozenset[str] = frozenset({"cross-site", "same-site"})
_CROSS_SITE_FETCH_MODES: frozenset[str] = frozenset({"no-cors", "navigate"})


def _split_env(name: str) -> list[str]:
    """Non-empty, trimmed, lower-cased comma-separated entries of env ``name``."""
    raw = os.environ.get(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _hostname(host: str) -> str:
    """Lower-cased hostname of a Host value, port removed.

    A port that is present but not all digits is left attached, so the value
    can never match an allowlist entry (reject, never sanitise).
    """
    value = host.strip().lower()
    if value.startswith("["):
        name, bracket, rest = value.partition("]")
        if bracket and (rest == "" or (rest[:1] == ":" and rest[1:].isdigit())):
            return name + bracket
        return value
    name, colon, port = value.rpartition(":")
    if colon and port.isdigit():
        return name
    return value


def allowed_hosts_from_env() -> frozenset[str]:
    """Loopback hostnames plus every hostname in ``NERVE_ALLOWED_HOSTS``."""
    return LOOPBACK_HOSTS | {_hostname(item) for item in _split_env("NERVE_ALLOWED_HOSTS")}


def allowed_origins_from_env() -> frozenset[str]:
    """Browser origins allowed by ``NERVE_ALLOWED_ORIGINS`` (none by default)."""
    return frozenset(_split_env("NERVE_ALLOWED_ORIGINS"))


def is_cross_site_fetch(headers: Mapping[str, str]) -> bool:
    """True when Fetch Metadata marks the request as a cross-site browser fetch.

    Absent ``Sec-Fetch-*`` headers (every non-browser client) never match.
    """
    site = headers.get("sec-fetch-site", "").strip().lower()
    mode = headers.get("sec-fetch-mode", "").strip().lower()
    return site in _CROSS_SITE_FETCH_SITES or mode in _CROSS_SITE_FETCH_MODES


def _is_json(content_type: str | None) -> bool:
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def check_request(
    method: str,
    headers: Mapping[str, str],
    *,
    allowed_hosts: frozenset[str] | None = None,
    allowed_origins: frozenset[str] | None = None,
) -> tuple[int, str] | None:
    """Return ``(status, reason)`` if the request must be rejected, else None.

    Checks run Host -> Fetch Metadata -> Origin -> Content-Type; the first
    failure wins.
    Allowlists default to the environment, read per call.
    """
    host = headers.get("host")
    if host is not None:
        hosts = allowed_hosts if allowed_hosts is not None else allowed_hosts_from_env()
        if _hostname(host) not in hosts:
            return HOST_REJECTED
    if is_cross_site_fetch(headers):
        return CROSS_SITE_REJECTED
    origin = headers.get("origin")
    if origin is not None:
        origins = allowed_origins if allowed_origins is not None else allowed_origins_from_env()
        if origin.strip().lower() not in origins:
            return ORIGIN_REJECTED
    if method == "POST" and not _is_json(headers.get("content-type")):
        return CONTENT_TYPE_REJECTED
    return None
