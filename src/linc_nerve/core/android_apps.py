"""Android app lifecycle — install, launch, kill, list, uninstall, clear, grant.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. References to test-patched / android.py module-globals (``subprocess``,
``time``, ``logger``, ``ADB_PATH``, ``classify_error``, constants) are routed
through the parent module (``_android.<name>``) so patches on
``linc_nerve.core.android.*`` keep working after the move.
"""

from __future__ import annotations

import re

from . import android as _android

# Package names and permission strings are interpolated directly into
# ``adb shell`` command lines (uninstall/clear_app_data/grant_permission —
# see issue #94). Anchor on a leading letter (blocks leading-dash flag
# injection, e.g. "-n com.evil.package") and restrict the body to
# letters/digits/underscore/dot (blocks shell metacharacters like ``;`` and
# backticks). This matches the Android package-name grammar and covers every
# real permission string (e.g. "android.permission.CAMERA").
#
# Anchored ``\A...\Z`` AND applied with ``fullmatch`` (2026-10-02 security
# review H1): ``^...$`` + ``re.match`` accepts a trailing ``\n`` because ``$``
# matches just before a final newline, and a newline in a device-shell
# string starts a second command. ``\Z`` keeps a stray ``.match`` safe too.
_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_.]*\Z")
_ACTIVITY_RE = re.compile(r"\A[A-Za-z_.][A-Za-z0-9_.$]*\Z")
# Deep-link URIs go into ``am start -d`` (Android) and ``simctl openurl`` / WDA
# ``/url`` (iOS). The grammar is an RFC 3986 scheme followed only by RFC 3986
# unreserved / reserved / percent characters: no whitespace, no control
# characters, no quotes other than the sub-delim ``'``, no backslash or
# backtick. ``&`` is legal, so multi-param deep links pass (finding 7). Applied
# with ``fullmatch``; ``_q(uri)`` quoting stays as the second layer.
_URI_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+\Z")
_URI_MAX_LEN = 2048


def validate_launch_uri(uri: str) -> None:
    """Raise ``ValueError`` unless ``uri`` is an RFC 3986-shaped deep link (<= 2048 chars)."""
    if len(uri) > _URI_MAX_LEN or not _URI_RE.fullmatch(uri):
        raise ValueError(
            f"Invalid uri {uri!r}: must be an RFC 3986 URI (scheme ':' then only "
            f"unreserved/reserved/percent-encoded characters; no whitespace, control "
            f"characters, quotes, backslash or backtick) of at most {_URI_MAX_LEN} characters"
        )


def _q(token: str) -> str:
    """Shell-quote one token for a device ``sh -c`` string.

    Defence in depth on top of the allowlists: a no-op (byte-identical) for
    plain identifiers, and it keeps ``$`` in inner-class activity names
    (``.Main$Inner``) from expanding on the device (review L1).
    """
    return _android.shlex.quote(token)


def _q_optional(token: str) -> str:
    """Like :func:`_q`, but an empty token stays empty (not ``''``).

    The deep-link launch supports ``package=""`` (linc-crucible's navigate
    shape) and must keep rendering ``-d URI `` byte-identically.
    """
    return _q(token) if token else ""


def _component(package: str, activity: str) -> str:
    """Quoted ``package/activity`` component for ``am start -n``."""
    return _q(f"{package}/{activity}")


def _validate_shell_identifier(value: str, kind: str) -> None:
    """Reject package/permission strings that could escape the shell command.

    Raises ``ValueError`` rather than silently sanitizing — a rejected
    identifier means the caller passed something that doesn't look like a
    real package/permission name, and guessing at a "fixed" value would be
    worse than failing loudly.
    """
    if not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"Invalid {kind} {value!r}: must match {_SAFE_IDENTIFIER_RE.pattern} "
            "(letters, digits, underscore, dot; must start with a letter)"
        )


def _validate_activity(value: str) -> None:
    """Reject activity names that could escape the shell command.

    Raises ``ValueError`` when the activity does not match the valid
    component/activity class grammar (letters, digits, underscore, dot, dollar,
    with a leading letter, underscore, or dot).
    """
    if not _ACTIVITY_RE.fullmatch(value):
        raise ValueError(
            f"Invalid activity {value!r}: must match {_ACTIVITY_RE.pattern} "
            "(letters, digits, underscore, dot, dollar; "
            "must start with a letter, underscore, or dot)"
        )


class _AppsMixin:
    """App install/launch/lifecycle methods for AndroidController."""

    def install(self, serial: str, apk_path: str) -> None:
        """Install an APK with retry on transient failures.

        Shells out to ``adb install`` (streamed pathway) rather than the legacy
        adbutils push+pm-install path. Lenovo Android 15 returns bare ``FAIL``
        through the legacy path, which adbutils surfaces as an opaque error;
        ``adb install`` exposes the real stderr (e.g. "Broken pipe", signature
        mismatch reasons). See improvement a387b40b.
        """
        with self._device_lock(serial):
            self._check_circuit(serial)
            t0 = _android.time.monotonic()
            last_exc: BaseException | None = None

            for attempt in range(_android._MAX_RETRIES + 1):
                if attempt > 0:
                    backoff = _android._BACKOFF_BASE * (2 ** (attempt - 1))
                    _android.logger.info(
                        "install_retry",
                        serial=serial,
                        attempt=attempt + 1,
                        backoff_s=backoff,
                    )
                    _android.time.sleep(backoff)

                try:
                    self._adb_install(serial, apk_path)
                    self._clear_device_failure(serial)
                    self._record_metric("install", serial, t0, success=True)
                    return
                except _android._RETRYABLE_ERRORS as exc:
                    last_exc = exc
                    self._record_device_failure(serial)
                    _android.logger.warning(
                        "install_failed",
                        serial=serial,
                        error=str(exc),
                        error_category=_android.classify_error(exc).value,
                        attempt=attempt + 1,
                    )
                except Exception as exc:
                    # Non-retryable errors (e.g., invalid APK) fail immediately
                    self._record_metric("install", serial, t0, success=False, error=exc)
                    raise

            self._record_metric("install", serial, t0, success=False, error=last_exc)
            raise last_exc  # type: ignore[misc]

    def _adb_install(self, serial: str, apk_path: str) -> None:
        """Invoke ``adb -s SERIAL install -r -t PATH`` and raise with full output."""
        cmd = [_android.ADB_PATH, "-s", serial, "install", "-r", "-t", apk_path]
        try:
            result = _android.subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except _android.subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"adb install timed out after 120s: {exc}") from exc

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0 or "Success" not in stdout:
            combined = "\n".join(s for s in (stdout, stderr) if s) or "<no output>"
            raise RuntimeError(f"adb install failed (exit={result.returncode}): {combined}")

    @staticmethod
    def _format_extras(extras: dict[str, str | bool | int] | None) -> str:
        """Render Android intent extras as ``am start`` flags (type-dispatched).

        ``bool`` → ``--ez KEY true|false`` (checked BEFORE ``int`` because a
        Python ``bool`` is a subclass of ``int``), ``int`` → ``--ei KEY N``,
        ``str`` → ``--es KEY V``. Returns ``""`` for ``None``/empty so launch
        commands stay byte-identical when no extras are supplied. Any other
        value type is a validation error (raised), never a silent drop.
        """
        if not extras:
            return ""
        parts: list[str] = []
        for key, value in extras.items():
            _validate_shell_identifier(key, "extras key")
            if isinstance(value, bool):
                parts.append(f"--ez {_q(key)} {str(value).lower()}")
            elif isinstance(value, int):
                parts.append(f"--ei {_q(key)} {value}")
            elif isinstance(value, str):
                parts.append(f"--es {_q(key)} {_q(value)}")
            else:
                raise ValueError(
                    f"Unsupported intent extras value type for {key!r}: "
                    f"{type(value).__name__} (expected str, bool, or int)"
                )
        return " ".join(parts)

    @staticmethod
    def _with_extras(cmd: str, extra_flags: str) -> str:
        """Append pre-formatted extras flags to an ``am start`` command."""
        return f"{cmd} {extra_flags}" if extra_flags else cmd

    def launch(
        self,
        serial: str,
        package: str,
        activity: str = "",
        uri: str = "",
        extras: dict[str, str | bool | int] | None = None,
    ) -> None:
        """Launch an app by package name, optionally to a deep link URI.

        ``extras`` are Android intent extras (e.g. ``{"SKIP_CONSENT": True}``)
        emitted as ``--ez``/``--es``/``--ei`` flags into ``am start`` so
        consent-gated apps can run unattended BVTs without a manual
        ``adb shell am start ... --ez SKIP_CONSENT true`` priming step.
        """
        if package:
            _validate_shell_identifier(package, "package")
        if activity:
            _validate_activity(activity)
        extra_flags = self._format_extras(extras)
        if uri:
            validate_launch_uri(uri)
            self._shell(
                serial,
                self._with_extras(
                    f"am start -a android.intent.action.VIEW -d {_q(uri)} {_q_optional(package)}",
                    extra_flags,
                ),
            )
        elif activity:
            self._shell(
                serial,
                self._with_extras(f"am start -n {_component(package, activity)}", extra_flags),
            )
        elif self._is_wear_os(serial):
            self._launch_wear_package(serial, package, extra_flags)
        else:
            self._launch_phone_package(serial, package, extra_flags)

    def _launch_phone_package(self, serial: str, package: str, extra_flags: str = "") -> None:
        """Package-only launch on a phone / tablet / emulator.

        Resolve the main launcher activity and ``am start -n`` it; when
        ``resolve-activity`` does not name ``package`` (e.g. Android's
        ``ResolverActivity`` when two launcher activities exist), query
        ``query-activities`` for MAIN/LAUNCHER: start a single match, raise on
        2+ matches, and fall back to a LAUNCHER monkey only when 0 match.
        """
        resolved = self._resolve_launcher_activity(serial, package)
        if not resolved:
            activities = self._query_launcher_activities(serial, package)
            if len(activities) > 1:
                components = ", ".join(f"{package}/{act}" for act in activities)
                raise RuntimeError(
                    f"{package} has {len(activities)} launcher activities: "
                    f"{components}; pass activity= to choose one"
                )
            if activities:
                resolved = activities[0]
        if resolved:
            self._shell(
                serial,
                self._with_extras(f"am start -n {_component(package, resolved)}", extra_flags),
            )
        elif extra_flags:
            raise RuntimeError(
                f"Cannot pass intent extras to {package} on {serial}: launcher "
                "activity resolution failed and the LAUNCHER-monkey fallback can't "
                "carry extras. Provide an explicit activity= or fix resolution."
            )
        else:
            out = self._shell(
                serial, f"monkey -p {_q(package)} -c android.intent.category.LAUNCHER 1"
            )
            if "No activities found" in out or "monkey aborted" in out:
                first_line = out.strip().splitlines()[0]  # non-empty: matched above
                raise RuntimeError(
                    f"package {package} is not installed (or has no launcher activity) "
                    f"on {serial}: {first_line}"
                )

    def _launch_wear_package(self, serial: str, package: str, extra_flags: str = "") -> None:
        """Package-only launch on Wear OS.

        A bare ``monkey -c LAUNCHER`` aborts on watches (Pixel Watch 2, issue
        #28), so resolve the launcher activity by the explicit LAUNCHER category
        and start it by component — never fall back to monkey (raise instead).

        ``--include-stopped-packages`` is REQUIRED on both the resolve and the
        start: a backgrounded / freshly-installed / just-rebooted Wear app sits
        in FLAG_STOPPED, and Android excludes stopped packages from intent
        resolution. Without it, ``resolve-activity`` returns "No activity found"
        and even an explicit ``am start -n`` reports "Activity class does not
        exist". Device-verified on Pixel Watch 2.
        """
        resolved = self._resolve_launcher_activity(
            serial,
            package,
            category="android.intent.category.LAUNCHER",
            include_stopped=True,
        )
        if not resolved:
            raise RuntimeError(
                f"Could not resolve launcher activity for {package} on Wear OS "
                f"device {serial}; refusing LAUNCHER-monkey fallback (it no-ops "
                "on Wear)"
            )
        self._shell(
            serial,
            self._with_extras(
                f"am start --include-stopped-packages -n {_component(package, resolved)}",
                extra_flags,
            ),
        )

    def _is_wear_os(self, serial: str) -> bool:
        """True if the device reports the ``watch`` token in ro.build.characteristics.

        Uses the device-type system property rather than sniffing the package
        name (issue #28). The property is a comma-separated token list (e.g.
        ``nosdcard,watch``); membership is checked by exact token, so substrings
        like ``watchband`` do not false-positive. Returns False on any probe error.
        """
        try:
            output = self._shell(serial, "getprop ro.build.characteristics")
        except Exception:
            return False
        tokens = {t.strip() for t in output.replace("\n", "").split(",")}
        return "watch" in tokens

    def _resolve_launcher_activity(
        self,
        serial: str,
        package: str,
        category: str | None = None,
        include_stopped: bool = False,
    ) -> str | None:
        """Resolve the main launcher activity for a package.

        Queries ``dumpsys package`` for the LAUNCHER category intent filter
        and returns the activity class name (e.g. ``.MainActivity``).
        Returns None if resolution fails (caller should fall back to monkey).

        When ``category`` is given it is passed as an explicit ``-c <category>``
        to ``resolve-activity`` — required for Wear OS, where implicit resolution
        returns nothing useful (issue #28).

        When ``include_stopped`` is set, ``--include-stopped-packages`` is added
        so a FLAG_STOPPED app (backgrounded / freshly installed / post-reboot)
        still resolves instead of returning "No activity found" (issue #28).
        """
        cmd = "cmd package resolve-activity --brief"
        if include_stopped:
            cmd += " --include-stopped-packages"
        if category:
            cmd += f" -c {_q(category)}"
        cmd += f" {_q(package)}"
        try:
            output = self._shell(serial, cmd)
        except Exception:
            return None
        return self._parse_resolved_activity(output, package)

    def _query_launcher_activities(self, serial: str, package: str) -> list[str]:
        """List all MAIN/LAUNCHER activity names declared by ``package``."""
        cmd = (
            "cmd package query-activities --brief "
            "-a android.intent.action.MAIN "
            f"-c android.intent.category.LAUNCHER {_q(package)}"
        )
        output = self._shell(serial, cmd)
        return self._parse_launcher_activities(output, package)

    @staticmethod
    def _parse_launcher_activities(output: str, package: str) -> list[str]:
        """Extract activity names for ``package`` from ``--brief`` package output."""
        prefix = f"{package}/"
        return [
            s.removeprefix(prefix)
            for line in output.splitlines()
            if (s := line.strip()).startswith(prefix)
        ]

    @staticmethod
    def _parse_resolved_activity(output: str, package: str) -> str | None:
        """Extract the activity component from ``resolve-activity --brief`` output.

        Output format::

            priority=0 preferredOrder=0 match=0x108000 ...
            com.pkg/.Activity

        Returns the activity name (``"com.pkg/.Activity"`` -> ``".Activity"``)
        for the first line naming ``package``, else None.
        """
        activities = _AppsMixin._parse_launcher_activities(output, package)
        return activities[0] if activities else None

    def kill(self, serial: str, package: str) -> None:
        """Force-stop an app."""
        _validate_shell_identifier(package, "package")
        self._shell(serial, f"am force-stop {_q(package)}")

    def list_apps(self, serial: str) -> list[str]:
        """List installed packages."""
        output = self._shell(serial, "pm list packages -3")
        return [
            line.replace("package:", "").strip()
            for line in output.strip().splitlines()
            if line.startswith("package:")
        ]

    def uninstall(self, serial: str, package: str) -> None:
        """Uninstall an app."""
        _validate_shell_identifier(package, "package")
        self._shell(serial, f"pm uninstall {_q(package)}")

    def clear_app_data(self, serial: str, package: str) -> bool:
        """Wipe an app's data + cache (``pm clear``) → first-install state.

        Returns ``False`` (needs_reinstall contract): ``pm clear`` leaves the
        app installed, so no reinstall is needed — unlike the iOS simulator
        path, which is uninstall-based and returns ``True``.

        Forces a true cold start: app-level consent/onboarding stored in
        SharedPreferences/DataStore is gone. Side effects (caller beware):
        ``pm clear`` revokes granted runtime permissions and removes
        AndroidKeystore aliases, so a flow needing either afterward must
        re-establish it (see ``grant_permission``).
        """
        _validate_shell_identifier(package, "package")
        self._shell(serial, f"pm clear {_q(package)}")
        return False

    def grant_permission(self, serial: str, package: str, permission: str) -> None:
        """Grant a runtime permission (``pm grant``) without the system dialog.

        Companion to ``clear_app_data`` — re-grant a permission it revoked
        (e.g. ``android.permission.CAMERA``). ``pm grant`` only works for
        permissions the app declares in its manifest as runtime permissions.
        """
        _validate_shell_identifier(package, "package")
        _validate_shell_identifier(permission, "permission")
        self._shell(serial, f"pm grant {_q(package)} {_q(permission)}")
