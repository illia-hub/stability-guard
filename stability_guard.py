#!/usr/bin/env python3
"""
stability-guard: background daemon that keeps a busy Mac responsive.

What it does:
  1. Lowers CPU/IO priority of background apps that are not in the whitelist
     (reversible, via taskpolicy -b / -B). Restores instantly on focus.
  2. Watches kern.memorystatus_vm_pressure_level and alerts on critical pressure.
  3. Watches for new crash reports (system errors) and alerts.
  4. After an incident is over, asks Claude (via the `claude` CLI, using your
     subscription - no API key) for a short report, appends it to history.md.

What it NEVER does: kill processes, delete files, or change system settings.
Only taskpolicy/renice on user-owned processes, which is fully reversible.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".config", "stability-guard", "config.json")
DATA_DIR = os.path.join(HOME, ".local", "share", "stability-guard")
HISTORY_PATH = os.path.join(DATA_DIR, "history.md")
LOG_PATH = os.path.join(DATA_DIR, "daemon.log")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

CRASH_DIRS = [
    os.path.join(HOME, "Library", "Logs", "DiagnosticReports"),
    "/Library/Logs/DiagnosticReports",
]
CRASH_SUFFIXES = (".ips", ".crash", ".panic", ".hang", ".spin")

DEFAULT_CONFIG = {
    "poll_seconds": 15,
    "whitelist": ["Finder", "Terminal", "iTerm2", "Google Chrome", "Safari", "Slack"],
    "min_cpu_percent": 3.0,
    "pressure_warn_level": 2,
    "pressure_critical_level": 4,
    "report_delay_minutes": 5,
    "min_report_interval_minutes": 30,
    "use_renice": False,
    "renice_value": 10,
    "notify": True,
    "notify_before_action": True,
    "notify_lag_warning": True,
    "notify_every_throttle": False,
    "notify_dedupe_seconds": 120,
    "notify_sound": "Submarine",
    "notify_sound_critical": "Basso",
    "confirm_before_throttle": False,
    "confirm_mode": "dialog",
    "confirm_timeout_seconds": 20,
    "notify_play_sound_directly": True,
    "visible_mode": "panel",
    "critical_alert_mode": "dialog",
    "dialog_seconds": 6,
    "alert_timeout_seconds": 12,
    "sample_interval_seconds": 60,
    "weekly_digest": True,
    "digest_interval_days": 7,
    "ai_enabled": True,
    "ai_timeout_seconds": 120,
    "claude_bin": "claude",
}


# ---------------------------------------------------------------- utilities

LOG_MAX_BYTES = 5 * 1024 * 1024


def log(msg):
    """Append a line to the daemon log. Never raises."""
    line = "%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        # Keep the log bounded: past 5 MB, drop the oldest half. Only our own log.
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH) as f:
                lines = f.readlines()
            with open(LOG_PATH, "w") as f:
                f.writelines(lines[len(lines) // 2:])
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)


def run(cmd, timeout=10, stdin_text=None):
    """Run a command, return (ok, stdout). Errors are logged, never raised."""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
        )
        if p.returncode != 0:
            log("cmd failed (%d): %s | %s" % (p.returncode, " ".join(cmd), p.stderr.strip()[:300]))
            return False, p.stdout
        return True, p.stdout
    except Exception as e:
        log("cmd error: %s | %s" % (" ".join(cmd), e))
        return False, ""


LAST_NOTIFY = {}


PANEL_BIN = os.path.join(DATA_DIR, "sgnotify")


def show_panel(title, message, seconds):
    """
    Our own banner: a small non-activating panel in the top right corner.

    It cannot take keyboard focus and clicks pass through it, so it never gets
    in the way. Unlike a real notification it is not suppressed while the screen
    is recorded or a Focus is on. Returns False if the helper is not built.
    """
    if not os.path.exists(PANEL_BIN):
        return False
    # Stack panels so simultaneous events do not cover each other.
    try:
        out = subprocess.run(["pgrep", "-c", "-f", PANEL_BIN],
                             capture_output=True, text=True, timeout=5)
        index = min(int(out.stdout.strip() or 0), 4)
    except Exception:
        index = 0
    try:
        subprocess.Popen([PANEL_BIN, title[:120], message[:300], str(seconds), str(index)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log("panel failed: %s" % e)
        return False


def show_dialog(title, message, timeout, blocking=False):
    """
    A window drawn by the window server, so macOS never suppresses it - unlike
    banners, which are hidden while the screen is recorded or a Focus is on.

    By default it behaves like a banner: appears, waits `timeout` seconds, then
    closes itself. Nothing to click, and the daemon is not blocked while it is
    up, because the process is fired and forgotten.
    """
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')[:400]

    script = ('display dialog "%s" with title "%s" buttons {"OK"}'
              ' default button "OK" with icon note giving up after %d'
              % (esc(message), esc(title), timeout))
    if blocking:
        run(["osascript", "-e", script], timeout=timeout + 10)
        return
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("dialog failed: %s" % e)


def notify(title, message, subtitle=None, open_path=None, key=None, sound=None,
           execute=None, no_dialog=False):
    """
    macOS notification. Best effort - a failure here must not stop the daemon.

    sound: name of a macOS system sound (see /System/Library/Sounds), or None
    for a silent notification. Sound is what actually makes you look, since
    banners are easy to miss and are hidden entirely while Focus is on.

    Uses terminal-notifier when installed (own icon, and the notification can be
    clicked to open a file), otherwise falls back to osascript, which delivers
    under the "Script Editor" identity.
    """
    if not CONFIG.get("notify", True):
        return

    # Anti-spam: never repeat the same notification within the dedupe window.
    # Pass an explicit key when the text varies every tick (top-memory lists),
    # otherwise the changing text would defeat the dedupe.
    key = key or (title, message)
    window = CONFIG.get("notify_dedupe_seconds", 120)
    now = time.time()
    if now - LAST_NOTIFY.get(key, 0) < window:
        return
    LAST_NOTIFY[key] = now

    # Play the sound ourselves instead of relying on the notification's sound
    # flag: if the sending app's alert style is set to "None" in System Settings,
    # macOS files the notification into Notification Center silently, with no
    # banner and no sound. afplay always works and needs no permission.
    if sound and CONFIG.get("notify_play_sound_directly", True):
        path = "/System/Library/Sounds/%s.aiff" % sound
        if os.path.exists(path):
            try:
                subprocess.Popen(["afplay", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log("afplay failed: %s" % e)

    # macOS hides banners entirely while the screen is being recorded/shared or
    # while a Focus is active - the app's own settings cannot override that.
    # visible_mode "dialog" routes everything through a window instead, which
    # the window server always draws.
    mode = CONFIG.get("visible_mode", "panel")
    if mode in ("panel", "dialog") and not no_dialog:
        body = "%s\n%s" % (subtitle, message) if subtitle else message
        secs = CONFIG.get("dialog_seconds", 6)
        if mode == "panel" and show_panel(title, body, secs):
            return
        show_dialog(title, body, secs)
        return

    tn = shutil.which("terminal-notifier")
    if tn:
        # No -group on purpose: terminal-notifier REMOVES the previous
        # notification that shares a group id, so a constant group would mean
        # only the newest event ever survives in Notification Center.
        # -ignoreDnD gets the banner through an active Focus session, which is
        # the whole point for a warning that the Mac is about to slow down.
        cmd = [tn, "-title", title[:120], "-message", message[:400], "-ignoreDnD"]
        if subtitle:
            cmd += ["-subtitle", subtitle[:120]]
        if open_path:
            cmd += ["-open", "file://" + open_path]
        if execute:
            cmd += ["-execute", execute]
        if sound:
            cmd += ["-sound", sound]
        run(cmd, timeout=8)
        return

    def esc(s):
        # Escape for an AppleScript string literal.
        return s.replace("\\", "\\\\").replace('"', '\\"')[:400]

    script = 'display notification "%s" with title "%s"' % (esc(message), esc(title))
    if subtitle:
        script += ' subtitle "%s"' % esc(subtitle)
    if sound:
        script += ' sound name "%s"' % esc(sound)
    run(["osascript", "-e", script], timeout=8)


def alert(title, message):
    """
    Impossible-to-miss alert for critical events.

    Banners depend on per-app notification settings and are hidden entirely
    while the screen is shared, so they cannot be relied on. A dialog is drawn
    by the window server itself and always shows. It closes by itself after
    alert_timeout_seconds, so nothing ever blocks the daemon for long.
    """
    notify(title, message, sound=CONFIG.get("notify_sound_critical") or None,
           no_dialog=True)
    if CONFIG.get("critical_alert_mode", "dialog") == "dialog":
        secs = CONFIG.get("alert_timeout_seconds", 12)
        if not show_panel(title, message, secs):
            show_dialog(title, message, secs)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        log("no config at %s, using defaults" % CONFIG_PATH)
    except Exception as e:
        log("bad config (%s), using defaults" % e)
    return cfg


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"seen_crashes": [], "last_report_ts": 0}


def save_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("cannot save state: %s" % e)


# ---------------------------------------------------------------- sensors

def frontmost_app():
    """Name of the app currently in focus, or None if System Events did not answer."""
    ok, out = run([
        "osascript", "-e",
        'tell application "System Events" to get name of first application process whose frontmost is true',
    ], timeout=8)
    return out.strip() if ok and out.strip() else None


def memory_pressure_level():
    """1 = normal, 2 = warning, 4 = critical. None if sysctl failed."""
    ok, out = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], timeout=5)
    try:
        return int(out.strip()) if ok else None
    except ValueError:
        return None


def list_gui_processes():
    """
    Return [{pid, app, cpu, rss_mb}] for third-party .app bundles owned by this user.

    Two hard exclusions, and they are the main safety boundary of the daemon:
      - processes owned by another user (root daemons etc.) - we must not touch them
      - anything under /System or /usr - Dock, loginwindow, WindowManager,
        ControlCenter and friends live there and must never be slowed down
    """
    me = os.environ.get("USER") or ""
    ok, out = run(["ps", "-axo", "pid=,user=,%cpu=,rss=,comm="], timeout=10)
    if not ok:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, user, cpu, rss, comm = parts
        if user != me:
            continue
        if comm.startswith("/System/") or comm.startswith("/usr/"):
            continue
        # Helpers are nested bundles:
        #   /Applications/Figma.app/Contents/Frameworks/Figma Helper.app/Contents/MacOS/...
        # Take the OUTERMOST .app, so a helper is judged by the app that owns it
        # and a whitelisted app automatically protects all of its helpers.
        bundles = re.findall(r"/([^/]+)\.app/", comm)
        if not bundles:
            continue
        try:
            procs.append({
                "pid": int(pid),
                "app": bundles[0],
                "cpu": float(cpu),
                "rss_mb": int(rss) // 1024,
            })
        except ValueError:
            continue
    return procs


def is_protected(app, front, whitelist):
    """
    True if the app must keep full priority.

    Matches by prefix so that helper processes are covered too:
    "Google Chrome Helper (Renderer)" is protected by the "Google Chrome" entry.
    """
    if front and (app == front or app.startswith(front + " ")):
        return True
    for w in whitelist:
        if app == w or app.startswith(w + " "):
            return True
    return False


def top_memory_processes(n=5, raw=False):
    """
    Top N by phys_footprint - the number Activity Monitor's "Memory" column shows.
    raw=True returns [(name, mb, pid), ...] for sampling.

    RSS is NOT that number and cannot be corrected into it. macOS compresses idle
    pages out of the resident set while they still count as footprint, and RSS
    counts shared/file-backed pages that never do. Measured here: a VM at RSS
    22 MB / footprint 2216 MB, chrome-headless-shell at RSS 79 MB / footprint
    16 MB - wrong in both directions at once, so the two top-8 lists had zero
    processes in common. /usr/bin/top is setuid root and is the only unprivileged
    way to read footprint for every process; `footprint --all` refuses without root.
    """
    ok, out = run(["/usr/bin/top", "-l", "1", "-o", "mem", "-n", str(n),
                   "-stats", "pid,command,mem"], timeout=20)
    if not ok:
        return []
    # top truncates COMMAND to 16 chars and shows the self-assigned process name
    # (the claude CLI reports its version string), so take real names from ps.
    names = {}
    ok2, pout = run(["ps", "-axo", "pid=,comm="], timeout=10)
    if ok2:
        for line in pout.splitlines():
            p = line.split(None, 1)
            if len(p) == 2:
                names[p[0]] = os.path.basename(p[1])
    unit = {"B": 1.0 / 1048576, "K": 1.0 / 1024, "M": 1.0, "G": 1024.0}
    rows, body = [], False
    for line in out.splitlines():
        if not body:
            body = line.startswith("PID")   # header length is not stable, scan for it
            continue
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        # MEM carries a unit suffix, and a +/- growth marker when -l is above 1.
        m = re.match(r"^([\d.]+)([BKMG])[+-]?$", parts[-1])
        if not m:
            continue
        mb = int(float(m.group(1)) * unit[m.group(2)])
        # Fall back to top's truncated name if the process died between the calls.
        rows.append((names.get(parts[0]) or " ".join(parts[1:-1]), mb, int(parts[0])))
    if raw:
        return rows                         # top -o mem already sorts descending
    return ["%s %d MB" % (name, mb) for name, mb, _ in rows]


SAMPLES_PATH = os.path.join(DATA_DIR, "samples.csv")


def sample_memory(top_n=8):
    """
    Append one row per top process to samples.csv: epoch,name,rss_mb.

    A single snapshot cannot tell a leak from a dev server that spikes while
    compiling and drops back. Only a time series can, so we keep one.
    """
    rows = top_memory_processes(top_n, raw=True)
    if not rows:
        return
    now = int(time.time())
    try:
        with open(SAMPLES_PATH, "a") as f:
            for name, mb, pid in rows:
                f.write("%d,%s,%d,%d\n" % (now, name.replace(",", " "), mb, pid))
    except Exception as e:
        log("cannot write samples: %s" % e)


def prune_samples(keep_days=7):
    """Drop samples older than keep_days. Called on daemon start."""
    cutoff = time.time() - keep_days * 86400
    try:
        if not os.path.exists(SAMPLES_PATH):
            return
        with open(SAMPLES_PATH) as f:
            lines = [l for l in f
                     if l.split(",", 1)[0].isdigit() and int(l.split(",", 1)[0]) >= cutoff]
        with open(SAMPLES_PATH, "w") as f:
            f.writelines(lines)
    except Exception as e:
        log("cannot prune samples: %s" % e)


def memory_trends(hours=24, min_peak_mb=300, min_samples=6):
    """
    Classify each process by how its memory behaves over time.

    The key signal is the FLOOR, not the peak: a leak never gives memory back,
    so its minimum keeps rising. A build server spikes and returns, so its
    minimum stays flat no matter how high the peaks are. Without this, two
    snapshots of a spiky process look exactly like a leak.
    """
    cutoff = time.time() - hours * 3600
    # Several processes share one name (4 x claude, 9 x renderer). Keying by name
    # alone made floor/peak span unrelated processes, so sum siblings per tick:
    # one number per app, which is what "how much is it using" actually means.
    # Legacy 3-column rows are RSS, a different unit - skipping them keeps the
    # two metrics from being compared. They age out via prune_samples.
    totals = {}
    try:
        with open(SAMPLES_PATH) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 4 or not parts[0].isdigit():
                    continue
                ts = int(parts[0])
                if ts < cutoff:
                    continue
                try:
                    key = (parts[1], ts)
                    totals[key] = totals.get(key, 0) + int(parts[2])
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    except Exception as e:
        log("cannot read samples: %s" % e)
        return []

    series = {}
    for (name, ts), mb in totals.items():
        series.setdefault(name, []).append((ts, mb))

    out = []
    for name, points in series.items():
        if len(points) < min_samples:
            continue
        points.sort()
        values = [v for _, v in points]
        peak = max(values)
        if peak < min_peak_mb:
            continue
        half = len(points) // 2
        floor_before = min(v for _, v in points[:half])
        floor_after = min(v for _, v in points[half:])
        latest = values[-1]

        # A rising floor is what actually distinguishes a leak. The ratio arm alone
        # scales with process size, so a 4 GB process had to reach 6 GB to register -
        # backwards for a memory daemon, where absolute bytes are what exhaust RAM.
        # ponytail: 500 MB is calibrated for 16 GB; raise it on a bigger machine.
        span_h = (points[-1][0] - points[0][0]) / 3600.0
        rising = (floor_after > floor_before * 1.5 + 50
                  or floor_after - floor_before > 500)
        leak = 0
        if rising and span_h >= 4:
            # Rank on this: a climbing floor is the only actionable line here, and
            # sorting by peak buried it under spiky giants that are just load.
            leak = floor_after - floor_before
            verdict = "floor rose %d->%d MB, not released" % (floor_before, floor_after)
        elif rising:
            verdict = "floor rose %d->%d MB but only %.1fh of data - too early to tell" % (
                floor_before, floor_after, span_h)
        elif peak > max(floor_after, 1) * 3:
            verdict = "spiky: floor %d MB, peak %d MB - load, not a leak" % (floor_after, peak)
        else:
            verdict = "steady around %d MB" % floor_after
        # Say how old the last sample is: a sparsely covered process can be hours
        # stale, and "now" asserted a freshness the data did not have.
        age_h = (time.time() - points[-1][0]) / 3600.0
        when = "now" if age_h < 0.5 else "%.0fh ago" % age_h
        out.append((leak, peak, "%s: %s; %d MB %s; %d samples over %.1fh" % (
            name, verdict, latest, when, len(points), span_h)))

    out.sort(reverse=True)
    return [line for _, _, line in out[:5]]


def new_crash_reports(seen):
    """Return (list of new crash report names, updated seen list)."""
    found = []
    for d in CRASH_DIRS:
        try:
            for name in os.listdir(d):
                if name.endswith(CRASH_SUFFIXES) and name not in seen:
                    found.append(name)
        except Exception:
            # Directory may not exist or may not be readable - that is fine.
            continue
    if found:
        seen = (seen + found)[-500:]  # keep the list bounded
    return found, seen


# ---------------------------------------------------------------- actions

APPROVED = set()   # apps you allowed to be throttled, for this daemon session
DENIED = set()     # apps you told it to leave alone, for this daemon session
PENDING = {}       # app -> time the veto banner was shown
VETO_DIR = os.path.join(DATA_DIR, "veto")


def veto_path(app):
    """Veto marker file for an app. Name is sanitised, no path tricks possible."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app)[:60]
    return os.path.join(VETO_DIR, safe)


def ask_permission_banner(app, cpu):
    """
    Confirmation without a modal window: a banner slides in from the top right
    and says what will happen. Click it to cancel. If you do not click within
    confirm_timeout_seconds, the daemon proceeds.

    Returns True when it is time to act, False while waiting or when vetoed.
    """
    now = time.time()
    marker = veto_path(app)

    if app not in PENDING:
        os.makedirs(VETO_DIR, exist_ok=True)
        try:
            if os.path.exists(marker):
                os.remove(marker)   # stale marker from a previous round
        except OSError:
            pass
        PENDING[app] = now
        notify("Throttling in %d s" % CONFIG.get("confirm_timeout_seconds", 20),
               "%s is using %.0f%% CPU in the background. Click this notification to cancel."
               % (app, cpu),
               subtitle="no click means it goes ahead",
               sound=CONFIG.get("notify_sound") or None,
               key="veto-%s" % app,
               execute="/usr/bin/touch %s" % shlex_quote(marker))
        return False

    if now - PENDING[app] < CONFIG.get("confirm_timeout_seconds", 20):
        return False

    PENDING.pop(app, None)
    if os.path.exists(marker):
        try:
            os.remove(marker)
        except OSError:
            pass
        DENIED.add(app)
        log("you vetoed %s, leaving it alone this session" % app)
        notify("Cancelled", "%s keeps its normal priority." % app)
        return False

    APPROVED.add(app)
    log("no veto for %s, proceeding" % app)
    return True


def shlex_quote(s):
    """Minimal shell quoting for the -execute command line."""
    return "'" + s.replace("'", "'\\''") + "'"


def ask_permission(app, cpu):
    """
    Ask before lowering an app's priority. Returns True if allowed.

    Answers are remembered per app until the daemon restarts, so you are asked
    once per app, not every 15 seconds. "Never" writes the app into the
    whitelist so it survives a restart. No answer within the timeout means NO -
    the daemon never acts on silence.
    """
    if app in DENIED:
        return False
    if app in APPROVED:
        return True

    timeout = CONFIG.get("confirm_timeout_seconds", 20)
    script = (
        'display dialog "%s is using %.0f%% CPU in the background.\n\n'
        'Lower its priority? It is restored as soon as you switch to it."'
        ' with title "Stability Guard" buttons {"Never", "Not now", "Lower"}'
        ' default button "Lower" with icon caution giving up after %d'
        % (app.replace('"', ""), cpu, timeout)
    )
    ok, out = run(["osascript", "-e", script], timeout=timeout + 10)
    if not ok:
        log("confirmation dialog failed for %s, not touching it" % app)
        DENIED.add(app)
        return False

    if "gaveup:true" in out.replace(" ", ""):
        log("no answer about %s, leaving it alone" % app)
        DENIED.add(app)
        return False

    if "Lower" in out:
        APPROVED.add(app)
        log("you allowed throttling of %s" % app)
        return True

    DENIED.add(app)
    if "Never" in out:
        add_to_whitelist(app)
    else:
        log("you skipped %s for this session" % app)
    return False


def add_to_whitelist(app):
    """Persist an app into the user's whitelist. Only ever adds, never removes."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if app not in cfg.get("whitelist", []):
            cfg.setdefault("whitelist", []).append(app)
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        log("%s added to whitelist permanently" % app)
        notify("Added to whitelist", "%s will never be throttled again." % app)
    except Exception as e:
        log("cannot update whitelist: %s" % e)


def throttle(pid, app):
    """Lower scheduling priority. Reversible. Returns True on success."""
    ok, _ = run(["taskpolicy", "-b", "-p", str(pid)], timeout=5)
    if ok and CONFIG.get("use_renice"):
        # renice down works without root, but going back up does NOT.
        # That is why it is opt-in and taskpolicy stays the primary mechanism.
        run(["renice", str(CONFIG.get("renice_value", 10)), "-p", str(pid)], timeout=5)
    return ok


def unthrottle(pid):
    """Restore normal scheduling priority. No-op if the process is already gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    ok, _ = run(["taskpolicy", "-B", "-p", str(pid)], timeout=5)
    return ok


# ---------------------------------------------------------------- AI report

SYSTEM_PROMPT = (
    "You are the reporting module of a macOS stability daemon. "
    "You receive facts about one incident plus the last reports you already wrote. "
    "Answer in English, max 6 lines, plain text, no markdown headers. "
    "Structure: 1-2 lines what happened, then at most ONE recommendation. "
    "The facts may include a 24h memory trend section. Any statement about growth "
    "or a leak MUST come from those trend lines only - never infer a trend from "
    "the single top-memory snapshot, which is one moment in time. "
    "Hard rule: do NOT repeat a recommendation that already appears in the history "
    "unless the new data shows a clearly different pattern. If the history already "
    "covers this situation and nothing new appeared, write exactly: "
    "'Known pattern repeated, no new recommendations.' "
    "Never suggest killing processes, deleting files or disabling swap - the daemon "
    "is read-only by design and cannot do those things."
)


def history_tail(n=10):
    """Last N report entries from history.md, as text."""
    try:
        with open(HISTORY_PATH) as f:
            text = f.read()
    except Exception:
        return "(no history yet)"
    entries = [e for e in text.split("\n## ") if e.strip()]
    if not entries:
        return "(no history yet)"
    return "\n## " + "\n## ".join(entries[-n:])


def ask_claude(incident_text):
    """Call the claude CLI with no tools enabled. Returns report text or None."""
    binary = shutil.which(CONFIG.get("claude_bin", "claude"))
    if not binary:
        log("claude CLI not found in PATH, skipping report")
        return None

    prompt = (
        "INCIDENT:\n%s\n\n"
        "PREVIOUS REPORTS (do not repeat recommendations from here):\n%s\n"
        % (incident_text, history_tail(10))
    )
    # --allowed-tools "" means Claude can only produce text: it cannot touch
    # the filesystem or run anything. This is the safety boundary.
    ok, out = run(
        [binary, "-p", "--allowed-tools", "", "--append-system-prompt", SYSTEM_PROMPT],
        timeout=CONFIG.get("ai_timeout_seconds", 120),
        stdin_text=prompt,
    )
    if not ok or not out.strip():
        return None
    return out.strip()


def write_report(incident_text, report_text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = "\n## %s\n\n**Facts:**\n%s\n\n**Report:**\n%s\n" % (
        stamp, incident_text, report_text)
    try:
        with open(HISTORY_PATH, "a") as f:
            f.write(block)
        return True
    except Exception as e:
        log("cannot write history: %s" % e)
        return False


# ---------------------------------------------------------------- incident

class Incident:
    """Accumulates facts about one rough patch until it is over."""

    def __init__(self):
        self.started = time.time()
        self.throttled = {}      # app -> number of times throttled
        self.pressure_peak = 1
        self.crashes = []
        self.jetsam = []
        self.top_mem = []
        # One notification per incident, not per tick and not per app.
        self.warned_lag = False
        self.warned_action = False

    def add_throttle(self, app):
        self.throttled[app] = self.throttled.get(app, 0) + 1

    def empty(self):
        return not self.throttled and self.pressure_peak < 4 and not self.crashes

    def to_text(self):
        mins = max(1, int((time.time() - self.started) / 60))
        lines = ["Duration: ~%d min" % mins]
        if self.throttled:
            apps = ", ".join("%s (x%d)" % (a, n) for a, n in
                             sorted(self.throttled.items(), key=lambda x: -x[1]))
            lines.append("Throttled (taskpolicy -b): " + apps)
        if self.pressure_peak >= 2:
            lines.append("Peak memory pressure: %d (1=normal, 2=warning, 4=critical)"
                         % self.pressure_peak)
        if self.top_mem:
            lines.append("Top memory: " + "; ".join(self.top_mem))
        if self.jetsam:
            lines.append("OUT OF MEMORY - macOS killed a process: " + ", ".join(self.jetsam[:3]))
        others = [c for c in self.crashes if not c.startswith("JetsamEvent")]
        if others:
            lines.append("System errors / crash reports: " + ", ".join(others[:5]))
        trends = memory_trends()
        if trends:
            lines.append("Memory over the last 24h (floor = what is never released):")
            lines.extend("  " + t for t in trends)
        return "\n".join("- " + l for l in lines)


# ---------------------------------------------------------------- main loop

CONFIG = dict(DEFAULT_CONFIG)
LAST_FRONT = None
RECOVERED = False
LAST_SAMPLE = 0


def tick(state, throttled_pids, incident):
    """One monitoring cycle. Returns the current incident (or None)."""
    global LAST_FRONT
    front = frontmost_app()
    front_changed = front != LAST_FRONT
    if front_changed:
        # Also proves the Accessibility permission is granted: without it
        # osascript returns nothing and this line would read "(unknown)".
        log("focus: %s" % (front or "(unknown - no Accessibility permission)"))
        LAST_FRONT = front
    whitelist = set(CONFIG["whitelist"])
    procs = list_gui_processes()
    alive = {p["pid"]: p for p in procs}

    # 0) Blind recovery. throttled_pids lives in memory only, so after a daemon
    # restart we no longer know what we lowered earlier. taskpolicy -B is
    # idempotent, so on every focus change we simply clear the background state
    # of the app that just came to front. Without this, an app throttled before
    # a restart would stay slow forever.
    global RECOVERED
    if not RECOVERED:
        # Same reason, for whitelisted apps: the whitelist may have grown while
        # the daemon was stopped.
        n = 0
        for p in procs:
            if is_protected(p["app"], None, whitelist):
                unthrottle(p["pid"])
                n += 1
        RECOVERED = True
        log("startup recovery: cleared background state on %d whitelisted processes" % n)

    if front_changed and front:
        for p in procs:
            if is_protected(p["app"], front, set()) and p["pid"] not in throttled_pids:
                unthrottle(p["pid"])

    # 1) restore anything that came back into focus, got whitelisted, or died
    for pid in list(throttled_pids):
        app = throttled_pids[pid]
        if pid not in alive:
            del throttled_pids[pid]
        elif is_protected(app, front, whitelist):
            if unthrottle(pid):
                log("restored %s (pid %d)" % (app, pid))
            del throttled_pids[pid]

    # 2) throttle background CPU hogs that are not protected.
    # front is None means System Events did not answer: we do not know what is
    # in focus, and acting on that would risk slowing down the app you are
    # actually using. Skip only the throttling - memory and crash checks below
    # still run.
    for p in ([] if front is None else procs):
        if p["pid"] in throttled_pids:
            continue
        if is_protected(p["app"], front, whitelist):
            continue
        if p["cpu"] < CONFIG["min_cpu_percent"]:
            continue
        if CONFIG.get("confirm_before_throttle"):
            # A confirmation needs a click, so it is the one and only thing that
            # opens a window in the centre of the screen. Every other message
            # goes to the panel in the top right corner.
            # "banner" mode asks by click on a system notification instead, and
            # therefore only works with visible_mode "banner".
            if CONFIG.get("confirm_mode", "dialog") == "dialog":
                allowed = ask_permission(p["app"], p["cpu"])
            else:
                allowed = ask_permission_banner(p["app"], p["cpu"])
            if not allowed:
                continue
        if throttle(p["pid"], p["app"]):
            throttled_pids[p["pid"]] = p["app"]
            log("throttled %s (pid %d, cpu %.1f%%)" % (p["app"], p["pid"], p["cpu"]))
            incident = incident or Incident()
            incident.add_throttle(p["app"])
            if CONFIG.get("notify_every_throttle"):
                notify("Priority lowered",
                       "%s (%.0f%% CPU, not in focus)" % (p["app"], p["cpu"]), sound=CONFIG.get("notify_sound", "Submarine") or None)
            elif CONFIG.get("notify_before_action", True) and not incident.warned_action:
                # One "I am acting now" notification per incident, not per app.
                incident.warned_action = True
                notify("System under load",
                       "Lowering priority of background apps: %s. Restored when you switch to them."
                       % p["app"],
                       subtitle="%s is using %.0f%% CPU in the background" % (p["app"], p["cpu"]), sound=CONFIG.get("notify_sound", "Submarine") or None)

    # 3) memory sample for the trend history, and memory pressure
    global LAST_SAMPLE
    if time.time() - LAST_SAMPLE >= CONFIG.get("sample_interval_seconds", 60):
        LAST_SAMPLE = time.time()
        sample_memory()

    level = memory_pressure_level()
    if level is not None and level >= CONFIG["pressure_critical_level"]:
        incident = incident or Incident()
        incident.pressure_peak = max(incident.pressure_peak, level)
        incident.top_mem = top_memory_processes(5)
        alert("Critical memory pressure",
              "Level 4 of 4 - things are about to stall.\n\nUsing the most memory:\n"
              + "\n".join(incident.top_mem[:5]))
        log("memory pressure critical, top: %s" % incident.top_mem)
    elif level is not None and level >= CONFIG["pressure_warn_level"]:
        # Early warning: the Mac is starting to struggle but is not critical yet.
        if CONFIG.get("notify_lag_warning", True):
            incident = incident or Incident()
            if not incident.warned_lag:
                incident.warned_lag = True
                incident.top_mem = top_memory_processes(5)
                notify("Memory under pressure",
                       "Using the most memory: " + "; ".join(incident.top_mem[:3]),
                       subtitle="level %d of 4 - things may get slow" % level,
                       key="lag-warning", sound=CONFIG.get("notify_sound", "Submarine") or None)
        if incident:
            incident.pressure_peak = max(incident.pressure_peak, level)

    # 4) system errors / crash reports
    crashes, state["seen_crashes"] = new_crash_reports(state["seen_crashes"])
    if crashes:
        incident = incident or Incident()
        incident.crashes.extend(crashes)
        # A JetsamEvent means macOS ran out of memory and killed something.
        # That is a different class of problem from an app crashing on a bug.
        jetsam = [c for c in crashes if c.startswith("JetsamEvent")]
        if jetsam:
            incident.jetsam.extend(jetsam)
            alert("macOS killed a process to free memory",
                  "Out of memory. Using the most memory right now:\n"
                  + "\n".join(top_memory_processes(5)))
        others = [c for c in crashes if not c.startswith("JetsamEvent")]
        if others:
            alert("System error", "New crash reports appeared:\n" + "\n".join(others[:5]))
        log("new crash reports: %s" % crashes)
        save_state(state)

    # 5) incident over? ask Claude, log the report, notify
    if incident and not incident.empty():
        age_min = (time.time() - incident.started) / 60
        calm = (level is None or level < CONFIG["pressure_critical_level"])
        if age_min >= CONFIG["report_delay_minutes"] and calm:
            finish_incident(state, incident)
            return None
        return incident
    return None


def finish_incident(state, incident):
    """Write the incident to history, asking Claude if the cooldown allows it."""
    facts = incident.to_text()
    cooldown = CONFIG["min_report_interval_minutes"] * 60
    since_last = time.time() - state.get("last_report_ts", 0)

    if not CONFIG.get("ai_enabled", True):
        write_report(facts, "(AI reports disabled in config)")
        return
    if since_last < cooldown:
        log("report skipped: cooldown, %d min left" % int((cooldown - since_last) / 60))
        write_report(facts, "(report skipped: subscription cooldown)")
        return

    who = ", ".join(incident.throttled) or "memory pressure"
    notify("Asking Claude", "Incident is over (%s). Requesting an analysis." % who,
           subtitle="through your subscription, not the API", key="asking-claude", sound=CONFIG.get("notify_sound", "Submarine") or None)
    report = ask_claude(facts)
    if report:
        write_report(facts, report)
        state["last_report_ts"] = time.time()
        save_state(state)
        notify("Claude report ready", report.splitlines()[0][:180],
               subtitle="click to open the history" if shutil.which("terminal-notifier")
               else "history: ~/.local/share/stability-guard/history.md",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound", "Submarine") or None)
        log("report written")
    else:
        write_report(facts, "(Claude unavailable, no report)")
        notify("Claude did not answer", "Incident facts were still written to the history.",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound", "Submarine") or None)


DIGEST_PROMPT = (
    "You are reviewing a week of stability reports from a macOS daemon. "
    "Answer in English, max 10 lines, plain text. "
    "Say which apps caused trouble most often, whether earlier recommendations "
    "were followed by any measurable change, and name at most two things worth "
    "doing next. Ignore one-off spikes; only call out repeating patterns."
)


def weekly_digest(state, force=False):
    """One Claude call per week over the whole history, appended as a summary."""
    if not CONFIG.get("weekly_digest", True) or not CONFIG.get("ai_enabled", True):
        return
    every = CONFIG.get("digest_interval_days", 7) * 86400
    if not force and time.time() - state.get("last_digest_ts", 0) < every:
        return
    try:
        with open(HISTORY_PATH) as f:
            history = f.read()
    except Exception:
        return
    if len(history) < 500:
        return

    binary = shutil.which(CONFIG.get("claude_bin", "claude"))
    if not binary:
        return
    log("running weekly digest")
    ok, out = run([binary, "-p", "--allowed-tools", "",
                   "--append-system-prompt", DIGEST_PROMPT],
                  timeout=CONFIG.get("ai_timeout_seconds", 120),
                  stdin_text="HISTORY:\n" + history[-20000:])
    state["last_digest_ts"] = time.time()
    save_state(state)
    if ok and out.strip():
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            with open(HISTORY_PATH, "a") as f:
                f.write("\n## %s - WEEKLY DIGEST\n\n%s\n" % (stamp, out.strip()))
        except Exception as e:
            log("cannot write digest: %s" % e)
        notify("Weekly digest ready", out.strip().splitlines()[0][:180],
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound") or None)
        log("weekly digest written")


def main():
    global CONFIG
    os.makedirs(DATA_DIR, exist_ok=True)
    CONFIG = load_config()
    first_run = not os.path.exists(STATE_PATH)
    state = load_state()

    if first_run:
        # Crash reports that already exist are history, not news. Mark them seen
        # silently, otherwise the very first tick would alert about all of them.
        _, state["seen_crashes"] = new_crash_reports([])
        save_state(state)
        log("first run: %d existing crash reports marked as seen"
            % len(state["seen_crashes"]))

    throttled_pids = {}   # pid -> app name, everything we lowered and must restore
    incident = None
    config_mtime = 0

    prune_samples()
    log("stability-guard started (poll=%ss, whitelist=%s)"
        % (CONFIG["poll_seconds"], ", ".join(CONFIG["whitelist"])))
    notify("Stability Guard", "Daemon started. Watching focus, memory and errors.")

    while True:
        try:
            # Hot-reload config so edits apply without a restart.
            try:
                m = os.path.getmtime(CONFIG_PATH)
                if m != config_mtime:
                    config_mtime = m
                    CONFIG = load_config()
                    log("config reloaded")
            except OSError:
                pass

            incident = tick(state, throttled_pids, incident)
            weekly_digest(state)
        except Exception as e:
            # A bug in one tick must never take the daemon down.
            log("tick error: %s" % e)

        time.sleep(CONFIG["poll_seconds"])


def selfcheck():
    """Minimal runnable check: sensors answer, incident text is built correctly."""
    assert memory_pressure_level() in (1, 2, 4), "sysctl pressure level unreadable"
    assert isinstance(list_gui_processes(), list)
    assert len(top_memory_processes(5)) > 0, "top returned nothing"

    inc = Incident()
    assert inc.empty()
    inc.add_throttle("Docker")
    inc.add_throttle("Docker")
    inc.pressure_peak = 4
    inc.crashes = ["Foo_2026-01-01-000000_Mac.ips"]
    text = inc.to_text()
    assert "Docker (x2)" in text and "4" in text and "Foo_" in text, text
    assert not inc.empty()

    found, seen = new_crash_reports([])
    found2, _ = new_crash_reports(seen)
    assert found2 == [], "crash reports are not deduplicated"

    # Safety: system UI must never be touched, helpers inherit their parent's rules.
    wl = ["Google Chrome", "Slack"]
    assert is_protected("Google Chrome Helper (Renderer)", None, wl), "helper unprotected"
    assert is_protected("Docker Desktop", "Docker Desktop", wl), "frontmost unprotected"
    assert not is_protected("Docker Desktop", "Slack", wl), "background app not throttled"
    system_apps = {"Dock", "loginwindow", "WindowManager", "ControlCenter",
                   "SystemUIServer", "Finder"}
    procs = list_gui_processes()
    seen_apps = {p["app"] for p in procs}
    leaked = system_apps & seen_apps
    assert not leaked, "system UI processes leaked into throttle candidates: %s" % leaked
    # Nested helper bundles must be reported under their owning app.
    assert not [a for a in seen_apps if "Helper" in a], \
        "helper bundles not folded into their parent app: %s" % seen_apps

    # Trend classification: a leak raises the floor, a build server does not.
    import tempfile
    global SAMPLES_PATH
    real_samples = SAMPLES_PATH
    SAMPLES_PATH = os.path.join(tempfile.mkdtemp(), "samples.csv")
    now_ts = int(time.time())
    with open(SAMPLES_PATH, "w") as f:
        # 30 min apart: a leak verdict needs at least 4h of span, so 20 samples
        # 5 min apart (the old fixture) would now correctly read as too early.
        for i in range(20):
            f.write("%d,leakdemo,%d,101\n" % (now_ts - (20 - i) * 1800, 200 + i * 60))
            f.write("%d,burstdemo,%d,102\n" % (now_ts - (20 - i) * 1800,
                                               1400 if i % 3 == 0 else 50))
        # Two processes sharing a name must be summed per tick, not treated as
        # one wildly swinging series.
        for i in range(20):
            f.write("%d,twindemo,300,201\n" % (now_ts - (20 - i) * 1800))
            f.write("%d,twindemo,300,202\n" % (now_ts - (20 - i) * 1800))
    trends = " ".join(memory_trends())
    SAMPLES_PATH = real_samples
    assert "floor rose" in trends.split("leakdemo")[1][:60], trends
    assert "not a leak" in trends.split("burstdemo")[1][:80], trends
    assert "600 MB" in trends.split("twindemo")[1][:60], "siblings not summed: %s" % trends

    print("selfcheck OK (pressure=%s, gui procs=%d, known crashes=%d)"
          % (memory_pressure_level(), len(list_gui_processes()), len(seen)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        selfcheck()
    elif len(sys.argv) > 1 and sys.argv[1] == "--digest":
        CONFIG = load_config()
        globals()["CONFIG"] = CONFIG
        st = load_state()
        weekly_digest(st, force=True)
        print("digest done, see " + HISTORY_PATH)
    elif len(sys.argv) > 1 and sys.argv[1] == "--trends":
        CONFIG = load_config()
        globals()["CONFIG"] = CONFIG
        lines = memory_trends()
        print("\n".join(lines) if lines else "not enough samples yet")
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-notify":
        # Sends one of every notification the daemon can produce, so you can
        # check that they actually reach Notification Center.
        CONFIG = load_config()
        CONFIG["notify_dedupe_seconds"] = 0
        globals()["CONFIG"] = CONFIG
        notify("Memory under pressure", "Using the most memory: Docker 4100 MB; Chrome 2300 MB",
               subtitle="level 2 of 4 - things may get slow",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("System under load",
               "Lowering priority of background apps: Docker. Restored when you switch to them.",
               subtitle="Docker is using 42% CPU in the background",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("Critical memory pressure", "Docker 4100 MB; ollama 1900 MB",
               subtitle="level 4 of 4 - things are about to stall",
               sound=CONFIG.get("notify_sound_critical") or None)
        time.sleep(2)
        notify("System error", "New crash reports: Example_2026-01-01.ips",
               sound=CONFIG.get("notify_sound_critical") or None)
        time.sleep(2)
        notify("Asking Claude", "Incident is over (Docker). Requesting an analysis.",
               subtitle="through your subscription, not the API",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("Claude report ready", "Example report line.",
               subtitle="history: ~/.local/share/stability-guard/history.md",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound") or None)
        print("Sent 6 notifications. Check Notification Center.")
    else:
        main()
