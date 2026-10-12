"""Design-by-Contract helpers for shipped code.

Python strips ``assert`` under ``-O``, so a "this component exists" check
written as ``assert x is not None`` silently vanishes in an optimised run and
the caller fails later with an opaque ``AttributeError`` on ``None``. Use
``require()`` instead: it always runs, keeps the type narrowing, and names
the missing component.

Dependency-free so both ``core/`` and ``mcp/`` can import it without a cycle.
"""

from __future__ import annotations


def require[T](value: T | None, what: str) -> T:
    """Return ``value`` unchanged, or raise if it is ``None``.

    Only ``None`` trips the contract — falsy-but-present values (``0``,
    ``""``, an empty list) are returned as-is.

    Raises:
        RuntimeError: ``"<what> is not initialised"`` when ``value`` is None.
    """
    if value is None:
        raise RuntimeError(f"{what} is not initialised")
    return value
