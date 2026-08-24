# stability-guard

A macOS background daemon: keeps the system responsive, watches memory and system
errors, and asks Claude to write a short report after every incident.

**No API key.** Reports go through the `claude -p` CLI — i.e. through your subscription.

## What it does

1. **Throttling.** Every 15 seconds it checks the active window. Apps that aren't
   focused and aren't on the whitelist, and that are eating CPU, get demoted via
   `taskpolicy -b -p PID`. Back in focus — priority is restored on the same tick
   (`taskpolicy -B`).
2. **Memory watchdog.** Reads `sysctl kern.memorystatus_vm_pressure_level`
   (1=normal, 2=warning, 4=critical). On critical — a notification with the top 5 by RSS.
3. **System errors.** Watches for new crash reports in `DiagnosticReports`
   (`.ips`, `.crash`, `.panic`, `.hang`). A new file → notification + goes into the report.
4. **Report.** When an incident ends (memory back to normal AND N minutes have passed),
   the daemon sends the facts to `claude -p` along with the last 10 history entries and
   an instruction "don't repeat recommendations already given".

## What it does NOT do

Doesn't kill processes. Doesn't delete files. Doesn't touch system settings, swap, or
Spotlight. The only action is a reversible priority demotion of its own user processes.
Claude is invoked with `--allowed-tools ""` — it can only return text, it has no tools.

## Installation

```bash
cd ~/Desktop/stability-guard
./install.sh
```

The script finds `python3` and `claude` itself, copies the files, builds the plist, and
loads the agent. Safe to run again — it won't overwrite an existing config.

**One manual permission:** on first launch macOS will ask for Accessibility access
(needed to determine the active window). System Settings → Privacy & Security →
Accessibility → enable `python3`. Without it throttling won't work, everything else will.

## Verification

```bash
# is the agent loaded? (second field = exit code, should be 0)
launchctl list | grep stability-guard

# what's happening right now
tail -f ~/.local/share/stability-guard/daemon.log

# one-off self-check of sensors without installing
python3 stability_guard.py --selfcheck
```

Live throttling test: start something heavy (Docker, a build), switch to another
window, wait 15-30 seconds — `throttled ...` will appear in the log. Switch back to
the app — `restored ...` will appear.

The log has `focus: <app>` lines on every active window change. If it shows
`(unknown - no Accessibility access)`, permission wasn't granted, and "by focus"
app protection won't work (whitelist still works, though).

To check priority restoration without touching windows: add an app to `whitelist`
in the config — `restored ...` will appear in the log within one tick.

## What the notifications look like

Two channels, only two:

| What | Where | Why |
|---|---|---|
| All events: lags, priority demotion, reports, crashes | **Top-right panel**, auto-closes after 6s | Doesn't steal focus, clicks pass through |
| Action confirmation (if enabled) | **Center-screen window** with buttons | Needs a click, the panel doesn't accept clicks |

The panel is its own Swift binary, `sgnotify`, compiled at install time. It's a
regular window (`NSPanel` with `.nonactivatingPanel`), not a system notification,
so macOS doesn't hide it during screen recording or an active Focus session — unlike
regular banners, which get fully suppressed in those conditions.

```json
"visible_mode": "panel"    // custom panel (default)
"visible_mode": "banner"   // regular macOS notifications
"visible_mode": "dialog"   // full center-screen window
"dialog_seconds": 6        // how long the panel stays up
```

If `swiftc` is unavailable, the installer will say so, and the daemon will fall
back to regular macOS notifications.

## Notifications

The daemon sends notifications through macOS Notification Center:

| When | Text |
|---|---|
| Memory at warning level | "Memory under pressure" + top 3 by RSS |
| Before priority demotion | "System under load — demoting priority of X" |
| Memory critical | "Critical memory pressure" + top 3 |
| New crash report | "System error" + file names |
| Contacting Claude | "Asking Claude" |
| Report written | "Report from Claude ready" + first line |

One notification per incident, not per app and not per tick
(dedup — `notify_dedupe_seconds`, default 120s).

Check that the channel works:

```bash
python3 stability_guard.py --test-notify   # sends one of each type
```

### To make sure you notice

Every notification comes with a sound: regular ones — `Submarine`, critical ones
(memory at the limit, crash) — `Basso`. Configurable:

```json
"notify_sound": "Submarine",        // any name from /System/Library/Sounds
"notify_sound_critical": "Basso",   // "" = no sound
```

Available: `Basso, Blow, Bottle, Frog, Funk, Glass, Hero, Morse, Ping, Pop, Purr,
Sosumi, Submarine, Tink`.

Even stronger: System Settings → Notifications → **Script Editor** → **Alerts**
style instead of Banners. Then the notification stays on screen until dismissed.

### Icon and name

By default notifications arrive as "Script Editor" with its icon — that's how
`osascript` works, it has no identity of its own. If you want your own name,
icon, and clicking to open `history.md`:

```bash
brew install terminal-notifier
```

The daemon will pick it up automatically, no config changes needed.

## Action confirmation

By default the daemon acts on its own. To control every demotion:

```json
"confirm_before_throttle": true,
"confirm_mode": "banner",       // or "dialog"
"confirm_timeout_seconds": 20
```

**`banner`** — a notification slides in from the top right: "Will demote Figma's
priority in 20s, click to cancel." Doesn't get in your way. No click = action proceeds.

**`dialog`** — a modal center-screen window with "Demote / Not now / Never" buttons.
Stricter: without an explicit click **nothing happens**, silence = refusal.
The "Never" button adds the app to the whitelist permanently.

In both modes the answer is remembered per app until the daemon restarts — you
won't be asked about the same thing every 15 seconds.

### If you don't see notifications

First check **Focus** — during an active Focus session (including from Motion,
Slack, or Calendar) macOS hides banners but still queues notifications in
Notification Center. Open it by clicking the clock — if they're all there, the
channel works.

To let alerts through Focus: System Settings → Focus → your mode → Allowed
Notifications → add the sending app.

## Report history

```bash
open ~/.local/share/stability-guard/history.md
```

Each entry is the incident facts + Claude's text. The file is append-only.

## Configuration

`~/.config/stability-guard/config.json` — re-read on the fly, no restart needed.

| Key | What it does |
|---|---|
| `whitelist` | Apps that are never demoted |
| `min_cpu_percent` | CPU threshold below which a background app is left alone |
| `report_delay_minutes` | How long after an incident starts to ask for a report |
| `min_report_interval_minutes` | Cooldown — protects subscription limits |
| `notify_every_throttle` | `true` = notify on every demotion (noisy) |
| `ai_enabled` | `false` = write facts to history without calling Claude |
| `use_renice` | See the limitation below. Default `false` |

App names — as in Activity Monitor (e.g. `Google Chrome`, not `Chrome`).

## Disable

```bash
# temporarily
launchctl bootout gui/$UID/com.user.stability-guard

# back on
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.user.stability-guard.plist

# fully
./uninstall.sh
```

## Known limitations (important)

- **`renice` is irreversible without root.** Any user can lower a priority, but
  only root can raise it back. That's why the main mechanism is `taskpolicy -b/-B`,
  which is reversible. `use_renice` is left as an option, but don't enable it
  without sudoers: the app will stay at a lowered nice value until it restarts.
- **Swap isn't touched.** There's no safe way to manage swap on macOS without a
  reboot — the daemon only reports memory pressure.
- **Subscription limits.** `claude -p` consumes the same quota as regular Claude
  Code usage. Hence the 30-minute cooldown and aggregating all incident events
  into one request instead of one request per event.
- **launchd and environment variables.** launchd doesn't inherit the shell
  environment — the agent won't see `export` from `~/.zshrc`. That's why `PATH`
  and `HOME` are set directly in the plist (`EnvironmentVariables` section), and
  the full path to `claude` is substituted into the config at install time. If
  you need to add a variable, either put it in the plist or use
  `launchctl setenv KEY value` (doesn't survive a reboot).
- **ChatGPT instead of Claude.** Technically you could swap `claude_bin` for
  `codex exec`, but the output format is different and the sandbox mode would
  need separate configuration — only `claude` is supported right now.
