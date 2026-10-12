"""HTTP daemon port resolution — shared by the server and its clients.

Side-effect free (no logging, no singletons) so CLI-side callers such as
``nerve doctor`` can import it without pulling in ``mcp._core`` / the SSE
server. Resolution order: explicit arg > ``NERVE_HTTP_PORT`` env > 8111.
"""

from __future__ import annotations

import os
from collections.abc import Callable

DEFAULT_HTTP_PORT = 8111
HTTP_PORT_ENV = "NERVE_HTTP_PORT"


def resolve_http_port(
    explicit: int | None,
    *,
    on_invalid: Callable[[str], None] | None = None,
) -> int:
    """Pick the port: explicit arg > NERVE_HTTP_PORT env > DEFAULT_HTTP_PORT.

    A non-integer env value falls back to the default; ``on_invalid`` (if
    given) receives the raw value so the caller can report it its own way.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get(HTTP_PORT_ENV)
    if not env:
        return DEFAULT_HTTP_PORT
    try:
        return int(env)
    except ValueError:
        if on_invalid is not None:
            on_invalid(env)
        return DEFAULT_HTTP_PORT
