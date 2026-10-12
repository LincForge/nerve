from linc_nerve.core.device_state import (
    Keyguard,
    KeyguardInfo,
    Wakefulness,
    compute_readiness,
    parse_battery,
    parse_keyguard,
    parse_power,
    parse_screen_size,
)


def test_parse_power_awake():
    assert parse_power("  mWakefulness=Awake\n  Display Power: state=ON") is Wakefulness.AWAKE


def test_parse_power_asleep():
    assert parse_power("  mWakefulness=Asleep\n  Display Power: state=OFF") is Wakefulness.ASLEEP


def test_parse_power_dozing_is_not_awake():
    assert parse_power("  mWakefulness=Dozing\n  Display Power: state=DOZE") is Wakefulness.DOZING


def test_parse_power_dreaming_is_dozing():
    assert parse_power("mWakefulness=Dreaming") is Wakefulness.DOZING


def test_parse_power_wear_napping_is_awake():
    assert parse_power("mWakefulness=Napping") is Wakefulness.AWAKE


def test_parse_power_fallback_display_state():
    assert parse_power("Display Power: state=ON") is Wakefulness.AWAKE
    assert parse_power("Display Power: state=DOZE_SUSPEND") is Wakefulness.DOZING


def test_parse_power_unknown_on_empty():
    assert parse_power("") is Wakefulness.UNKNOWN


def test_parse_keyguard_unlocked():
    info = parse_keyguard(
        "mDreamingLockscreen=false\n  KeyguardController:\n    mKeyguardShowing=false"
    )
    assert info.state is Keyguard.UNLOCKED
    assert info.secure is False


def test_parse_keyguard_locked():
    info = parse_keyguard(
        "mDreamingLockscreen=true\n  KeyguardController:\n    mKeyguardShowing=true"
    )
    assert info.state is Keyguard.LOCKED


def test_parse_keyguard_secure_flag():
    info = parse_keyguard("mKeyguardShowing=true mKeyguardSecure=true")
    assert info.state is Keyguard.LOCKED
    assert info.secure is True


def test_parse_keyguard_samsung_secure_pin():
    # Live Galaxy S10 (A12, test bench phone) with a PIN: secure is reported as a bare
    # `secure=true` line under KeyguardServiceDelegate — NOT `mKeyguardSecure=true`.
    # Missing this read a PIN'd device as non-secure → ensure_ready swiped a real lock.
    out = (
        "    KeyguardServiceDelegate\n"
        "      secure=true\n"
        "      deviceHasKeyguard=true\n"
        "      KeyguardStateMonitor\n"
        "        mSimSecure=false\n"
        "    isKeyguardShowing=true"
    )
    info = parse_keyguard(out)
    assert info.state is Keyguard.LOCKED
    assert info.secure is True


def test_parse_keyguard_samsung_secure_false_when_no_pin():
    # Same Samsung layout but lock-free: secure=false line, not showing.
    out = (
        "    KeyguardServiceDelegate\n"
        "      secure=false\n"
        "      deviceHasKeyguard=true\n"
        "    isKeyguardShowing=false"
    )
    info = parse_keyguard(out)
    assert info.state is Keyguard.UNLOCKED
    assert info.secure is False


def test_parse_keyguard_unknown_on_empty():
    assert parse_keyguard("").state is Keyguard.UNKNOWN


def test_parse_keyguard_samsung_iskeyguardshowing_unlocked():
    # Samsung Android 12 uses `isKeyguardShowing=`, not `mKeyguardShowing=` (live: tab-active3)
    info = parse_keyguard(
        "mShowingDream=false mDreamingLockscreen=false\n    isKeyguardShowing=false"
    )
    assert info.state is Keyguard.UNLOCKED


def test_parse_keyguard_samsung_iskeyguardshowing_locked():
    assert parse_keyguard("isKeyguardShowing=true").state is Keyguard.LOCKED


def test_parse_battery_charging():
    info = parse_battery("  level: 87\n  scale: 100\n  status: 2\n  AC powered: true")
    assert info.battery_pct == 87
    assert info.charging is True


def test_parse_battery_discharging():
    info = parse_battery(
        "  level: 8\n  scale: 100\n  status: 3\n  AC powered: false\n"
        "  USB powered: false\n  Wireless powered: false"
    )
    assert info.battery_pct == 8
    assert info.charging is False


def test_parse_battery_full_counts_as_charging():
    info = parse_battery("level: 100\n scale: 100\n status: 5")
    assert info.charging is True


def test_parse_battery_missing_level_is_none():
    assert parse_battery("status: 2").battery_pct is None


def test_readiness_ready():
    ready, reason = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.UNLOCKED), 87, True)
    assert ready is True and reason == "ready"


def test_readiness_asleep_blocks():
    ready, reason = compute_readiness(
        Wakefulness.ASLEEP, KeyguardInfo(Keyguard.UNLOCKED), 87, True
    )
    assert ready is False and "asleep" in reason


def test_readiness_dozing_blocks():
    ready, reason = compute_readiness(
        Wakefulness.DOZING, KeyguardInfo(Keyguard.UNLOCKED), 87, True
    )
    assert ready is False and "dozing" in reason


def test_readiness_locked_blocks():
    ready, reason = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.LOCKED), 87, True)
    assert ready is False and reason == "locked (keyguard up)"


def test_readiness_secure_high_battery_actionable():
    # A charging bench device sits at ~100%; secure-locked there must still give the
    # actionable secure_keyguard_set verdict (not the old battery-gated BFU reason).
    ready, reason = compute_readiness(
        Wakefulness.AWAKE, KeyguardInfo(Keyguard.LOCKED, secure=True), 100, True
    )
    assert ready is False and reason.startswith("secure_keyguard_set")


def test_readiness_keyguard_unknown_blocks():
    ready, reason = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.UNKNOWN), 87, True)
    assert ready is False and reason == "keyguard_state_indeterminate"


def test_readiness_low_battery_not_charging_blocks():
    ready, reason = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.UNLOCKED), 8, False)
    assert ready is False and reason == "battery 8% and not charging"


def test_readiness_low_battery_but_charging_ok():
    ready, _ = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.UNLOCKED), 8, True)
    assert ready is True


def test_readiness_none_battery_does_not_block():
    ready, _ = compute_readiness(Wakefulness.AWAKE, KeyguardInfo(Keyguard.UNLOCKED), None, False)
    assert ready is True


def test_parse_screen_size_physical_only():
    assert parse_screen_size("Physical size: 1080x2280") == (1080, 2280)


def test_parse_screen_size_override_wins():
    out = "Physical size: 1080x2280\nOverride size: 720x1520"
    assert parse_screen_size(out) == (720, 1520)


def test_parse_screen_size_multiline_noise():
    out = "rotation 0\nPhysical size: 1440x3040\nstable insets ...\n"
    assert parse_screen_size(out) == (1440, 3040)


def test_parse_screen_size_garbage_is_none():
    assert parse_screen_size("wm: command not found") is None
    assert parse_screen_size("") is None


def test_parse_screen_size_zero_override_is_none():
    # A confused `wm size reset` can emit 0x0 — must not yield a (0,0) swipe target.
    out = "Physical size: 1080x2280\nOverride size: 0x0"
    assert parse_screen_size(out) is None


def test_readiness_secure_low_battery_is_actionable():
    # Behavioral change: old code returned generic "locked (keyguard up)" here.
    ok, reason = compute_readiness(
        Wakefulness.AWAKE, KeyguardInfo(Keyguard.LOCKED, secure=True), 50, False
    )
    assert ok is False
    assert reason.startswith("secure_keyguard_set")


def test_readiness_secure_mid_battery_actionable():
    ok, reason = compute_readiness(
        Wakefulness.AWAKE, KeyguardInfo(Keyguard.LOCKED, secure=True), 97, True
    )
    assert ok is False and reason.startswith("secure_keyguard_set")


def test_readiness_nonsecure_locked_unchanged():
    ok, reason = compute_readiness(
        Wakefulness.AWAKE, KeyguardInfo(Keyguard.LOCKED, secure=False), 90, True
    )
    assert ok is False and reason == "locked (keyguard up)"


# --- UiAutomation leak detection (44d68b62) ---
# Real line captured live on the bench Pixel 8 Pro (2026-07-11) while a
# UiAutomation client was registered: `uiautomator events` running, exact
# same registration surface a killed `am instrument` leaks.
_UIA_LINE = "Ui Automation[eventTypes=TYPES_ALL_MASK, notificationTimeout=0]"


def test_parse_uiautomation_registered_true_on_live_line():
    from linc_nerve.core.device_state import parse_uiautomation_registered

    dump = "Registered clients:{\n[com.example.app]}\n" + _UIA_LINE + "\nGlobal Info [\n"
    assert parse_uiautomation_registered(dump) is True


def test_parse_uiautomation_registered_false_on_clean_dump():
    from linc_nerve.core.device_state import parse_uiautomation_registered

    assert parse_uiautomation_registered("Registered clients:{}\nGlobal Info [\n") is False


def test_parse_uiautomation_registered_false_on_empty():
    from linc_nerve.core.device_state import parse_uiautomation_registered

    assert parse_uiautomation_registered("") is False


def test_parse_uiautomation_registered_ignores_midline_mention():
    from linc_nerve.core.device_state import parse_uiautomation_registered

    # Prose/package mentions must not trip the detector — only the
    # accessibility manager's own registration line (line-anchored) counts.
    assert parse_uiautomation_registered("label=Ui Automation[x] service crashed") is False
