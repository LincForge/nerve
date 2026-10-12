# `dumpsys connectivity` fixtures

Trimmed `adb shell dumpsys connectivity` output for `parse_connectivity`
(`src/linc_nerve/core/device_state.py`, linc-nerve-up9). All are real captures
taken on the Mac mini bench on 2026-10-04, under a nerve reservation.

- `tablet_offline.txt`: Galaxy Tab Active3 (SM-T570, Android 12)
  with Wi-Fi switched off, so there is no default network
  (`Active default network: none`, empty `Current Networks:`). This is the
  offline state from bench bug-hunt p0-03. The tablet had since been joined to
  Wi-Fi, so it was switched off for the capture and switched back on afterwards.
- `tablet_wifi_online.txt`: the same tablet on validated Wi-Fi. It shows
  Samsung's Android 12 record layout (`Score(60 ; ...)`, no `created=` stamp).
- `phone_wifi_online.txt`: Galaxy S24 (SM-S921U, Android 16) on
  validated Wi-Fi.
- `watch_wifi_companion_proxy.txt`: Pixel Watch 2 (Android 13). It has two
  validated networks: Wi-Fi (the default) and the phone companion proxy
  (`Transports: BLUETOOTH`). The parser has to pick the record whose id matches
  `Active default network:`.
- `ping_one_received.txt`: `ping -c 1 -w 3 8.8.8.8` on the Galaxy S24.

How they were trimmed: each file keeps the `Active default network:` line and
the `Current Networks:` section, with each network's request list cut to a few
lines.

What was scrubbed: SSIDs became `<redacted-ssid>`. BSSIDs and MACs became
`<redacted-mac>`. IPv4 and IPv6 addresses became `<redacted-ip>`, except
`0.0.0.0` and the public `8.8.8.8`.
