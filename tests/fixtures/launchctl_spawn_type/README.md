# `launchctl print` spawn-type fixtures

The `spawn type = <word> (<n>)` line from `launchctl print gui/$UID/<label>`,
for `parse_loaded_job` / `reload_reasons` in `scripts/post_merge_kickstart.py`
(linc-nerve-o4w7). All are real captures from the Mac mini (macOS 26.6.2) on
2026-10-05, taken from the scratch job `com.linc.nerve-ptest` while it was
loaded with each plist `ProcessType`:

| File | plist `ProcessType` | captured line |
|------|---------------------|---------------|
| `background.txt` | `Background` | `spawn type = background (5)` |
| `standard.txt` | `Standard` | `spawn type = daemon (3)` |
| `absent.txt` | (key absent) | `spawn type = daemon (3)` |
| `interactive.txt` | `Interactive` | `spawn type = interactive (4)` |
| `adaptive.txt` | `Adaptive` | `spawn type = adaptive (6)` |

launchd reports `Standard` (and a plist with no `ProcessType`) as `daemon`,
not `standard`, so the script maps that word back before comparing.
