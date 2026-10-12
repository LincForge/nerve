"""Emulator-console line builders with input validation (security review M1).

Every value interpolated into a line for the AUTHENTICATED Android emulator
telnet console goes through here. The console executes one verb per line, so a
``\\r`` or ``\\n`` in any field runs a second verb (``kill``, ``avd snapshot
delete``, ``screenrecord start <host path>`` …) with the user's console token.
See docs/reviews/2026-10-02-linc-nerve-0.1.0-prerelease-security-review.md, M1.

Dependency-free on purpose: ``daemon/http_router.py`` imports the allowlist for
its pre-dispatch guard and must stay import-light (no controller imports).
"""

from __future__ import annotations

from typing import Any

#: Sensor names accepted by the emulator console's ``sensor set`` verb. An
#: explicit set rather than a grammar: membership is exact, so no whitespace,
#: control character, or second console verb can ride along.
EMULATOR_SENSOR_NAMES = frozenset(
    {
        "acceleration",
        "acceleration-uncalibrated",
        "gyroscope",
        "gyroscope-uncalibrated",
        "magnetic-field",
        "magnetic-field-uncalibrated",
        "orientation",
        "temperature",
        "proximity",
        "light",
        "pressure",
        "humidity",
        "hinge-angle0",
        "hinge-angle1",
        "hinge-angle2",
        "heart-rate",
        "rgbc-light",
        "wrist-tilt",
        "heading",
    }
)


def is_allowed_sensor_type(value: Any) -> bool:
    """True only for an exact, allowlisted emulator sensor name."""
    return isinstance(value, str) and value in EMULATOR_SENSOR_NAMES


def validate_sensor_type(value: Any) -> str:
    """Return ``value`` if it is an allowlisted sensor name, else raise ValueError."""
    if not is_allowed_sensor_type(value):
        raise ValueError(
            f"sensor_type {value!r} is not a supported emulator sensor; "
            f"expected one of: {', '.join(sorted(EMULATOR_SENSOR_NAMES))}"
        )
    return value


def _require_number(name: str, value: Any) -> None:
    """Raise ValueError unless ``value`` is an int/float (formats with no line break)."""
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")


def sensor_set_line(sensor_type: Any, x: Any, y: Any, z: Any) -> bytes:
    """Build the ``sensor set`` console line, validating every field first.

    Formatting of numeric values is unchanged from the original f-string, so
    ``(acceleration, 0, 0, 0)`` still yields ``sensor set acceleration 0:0:0``.
    """
    validate_sensor_type(sensor_type)
    for name, value in (("x", x), ("y", y), ("z", z)):
        _require_number(name, value)
    return f"sensor set {sensor_type} {x}:{y}:{z}\n".encode()


def auth_line(token: str) -> bytes:
    """Build the console ``auth`` line; refuse a token carrying a line break."""
    if "\r" in token or "\n" in token:
        raise ValueError("emulator console auth token contains a line break")
    return ("auth " + token + "\n").encode()
