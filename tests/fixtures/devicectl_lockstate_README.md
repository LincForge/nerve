# `xcrun devicectl device info lockState` fixtures

Captured read-only on a physical bench iPhone 13 Pro (`iPhone14,2`) under a `nerve` device reservation (linc-nerve-pt4w).

## Capture Environment & Provenance

- **Command**: `xcrun devicectl device info lockState --device <redacted-udid> --json-output -`
- **Xcode version** (`xcodebuild -version`): `Xcode 27.2` (`Build version 27B52`), `devicectl` `version` `651.32` (`jsonVersion` `5`)
- **iOS version**: `iOS 26.6.1`
- **Observed state**: `unlocked` (bench device configured with Auto-Lock Never)
- **Exit code**: `0`
- **Stdout prefix note**: When `--json-output -` writes to stdout without `--quiet`, `devicectl` prints a 4-line human-readable header (`Current device lock state:` followed by bullet lines) immediately before the top-level `{` of the JSON document. Parsers reading stdout directly should slice from the first `{` (or pass `--quiet`).

## Key Paths Carrying Lock State Booleans

Inside the top-level JSON object:

- `result.passcodeRequired` (`bool`): `false` when the device is currently **unlocked** (no passcode entry required to interact); `true` when the device is **locked** (passcode required).
- `result.unlockedSinceBoot` (`bool`): `true` when the device has been unlocked at least once since boot (After First Unlock); `false` before first unlock after boot.

## Fixture Files

- `devicectl_lockstate_observed.json` (**observed**): Real JSON output captured from the unlocked bench iPhone 13 Pro (`result.passcodeRequired: false`, `result.unlockedSinceBoot: true`), with the hardware UDID (`--device` argument) replaced by `<redacted-udid>` and the CoreDevice UUID (`result.deviceIdentifier`) replaced by `<redacted-device-identifier>`.
- `devicectl_lockstate_derived.json` (**derived, not observed**): Derived opposite-state (`locked`) fixture constructed from `devicectl_lockstate_observed.json` by flipping only the lock-state booleans (`result.passcodeRequired` flipped from `false` to `true`, and `result.unlockedSinceBoot` flipped from `true` to `false`). The bench iPhone's lock state was not modified during capture.
