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
    "confirm_mode": "banner",
    "confirm_timeout_seconds": 20,
    "notify_play_sound_directly": True,
    "critical_alert_mode": "dialog",
    "alert_timeout_seconds": 30,
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


def notify(title, message, subtitle=None, open_path=None, key=None, sound=None,
           execute=None):
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

    tn = shutil.which("terminal-notifier")
    if tn:
        # -ignoreDnD gets the banner through an active Focus session, which is
        # the whole point for a warning that the Mac is about to slow down.
        cmd = [tn, "-title", title[:120], "-message", message[:400],
               "-group", "stability-guard", "-ignoreDnD"]
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
    notify(title, message, sound=CONFIG.get("notify_sound_critical") or None)
    if CONFIG.get("critical_alert_mode", "dialog") != "dialog":
        return

    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')[:400]

    timeout = CONFIG.get("alert_timeout_seconds", 30)
    script = ('display dialog "%s" with title "Stability Guard" buttons {"Понятно"}'
              ' default button "Понятно" with icon caution giving up after %d'
              % (esc(message), timeout))
    run(["osascript", "-e", script], timeout=timeout + 10)


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


def top_memory_processes(n=5):
    """Top N processes by RSS, as ['Name 1234 MB', ...]."""
    ok, out = run(["ps", "-axo", "rss=,comm="], timeout=10)
    if not ok:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            rows.append((int(parts[0]), os.path.basename(parts[1])))
        except ValueError:
            continue
    rows.sort(reverse=True)
    return ["%s %d MB" % (name, rss // 1024) for rss, name in rows[:n]]


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
        notify("Понижу приоритет через %d с" % CONFIG.get("confirm_timeout_seconds", 20),
               "%s грузит %.0f%% CPU в фоне. Нажми это уведомление, чтобы отменить."
               % (app, cpu),
               subtitle="без нажатия действие выполнится",
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
        notify("Отменено", "%s останется с обычным приоритетом." % app)
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
    once per app, not every 15 seconds. "Никогда" writes the app into the
    whitelist so it survives a restart. No answer within the timeout means NO -
    the daemon never acts on silence.
    """
    if app in DENIED:
        return False
    if app in APPROVED:
        return True

    timeout = CONFIG.get("confirm_timeout_seconds", 20)
    script = (
        'display dialog "Приложение %s грузит %.0f%% CPU в фоне.\n\n'
        'Понизить его приоритет? Вернётся, как только переключишься в него."'
        ' with title "Stability Guard" buttons {"Никогда", "Не сейчас", "Понизить"}'
        ' default button "Понизить" with icon caution giving up after %d'
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

    if "Понизить" in out:
        APPROVED.add(app)
        log("you allowed throttling of %s" % app)
        return True

    DENIED.add(app)
    if "Никогда" in out:
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
        notify("Добавлено в whitelist", "%s больше не будет понижаться." % app)
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
    """Restore normal scheduling priority."""
    ok, _ = run(["taskpolicy", "-B", "-p", str(pid)], timeout=5)
    return ok


# ---------------------------------------------------------------- AI report

SYSTEM_PROMPT = (
    "You are the reporting module of a macOS stability daemon. "
    "You receive facts about one incident plus the last reports you already wrote. "
    "Answer in Russian, max 6 lines, plain text, no markdown headers. "
    "Structure: 1-2 lines what happened, then at most ONE recommendation. "
    "Hard rule: do NOT repeat a recommendation that already appears in the history "
    "unless the new data shows a clearly different pattern. If the history already "
    "covers this situation and nothing new appeared, write exactly: "
    "'Povtor izvestnogo patterna, novyh rekomendacij net.' in Russian. "
    "Never suggest killing processes, deleting files or disabling swap - the daemon "
    "is read-only by design and cannot do those things."
)


def history_tail(n=10):
    """Last N report entries from history.md, as text."""
    try:
        with open(HISTORY_PATH) as f:
            text = f.read()
    except Exception:
        return "(история пуста)"
    entries = [e for e in text.split("\n## ") if e.strip()]
    if not entries:
        return "(история пуста)"
    return "\n## " + "\n## ".join(entries[-n:])


def ask_claude(incident_text):
    """Call the claude CLI with no tools enabled. Returns report text or None."""
    binary = shutil.which(CONFIG.get("claude_bin", "claude"))
    if not binary:
        log("claude CLI not found in PATH, skipping report")
        return None

    prompt = (
        "ИНЦИДЕНТ:\n%s\n\n"
        "ПРЕДЫДУЩИЕ ОТЧЁТЫ (не повторяй рекомендации отсюда):\n%s\n"
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
    block = "\n## %s\n\n**Факты:**\n%s\n\n**Отчёт:**\n%s\n" % (
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
        lines = ["Длительность: ~%d мин" % mins]
        if self.throttled:
            apps = ", ".join("%s (x%d)" % (a, n) for a, n in
                             sorted(self.throttled.items(), key=lambda x: -x[1]))
            lines.append("Понижен приоритет (taskpolicy -b): " + apps)
        if self.pressure_peak >= 2:
            lines.append("Пик memory pressure: %d (1=норма, 2=warning, 4=critical)"
                         % self.pressure_peak)
        if self.top_mem:
            lines.append("Топ по памяти: " + "; ".join(self.top_mem))
        if self.crashes:
            lines.append("Системные ошибки / крэш-репорты: " + ", ".join(self.crashes[:5]))
        return "\n".join("- " + l for l in lines)


# ---------------------------------------------------------------- main loop

CONFIG = dict(DEFAULT_CONFIG)
LAST_FRONT = None
RECOVERED = False


def tick(state, throttled_pids, incident):
    """One monitoring cycle. Returns the current incident (or None)."""
    global LAST_FRONT
    front = frontmost_app()
    front_changed = front != LAST_FRONT
    if front_changed:
        # Also proves the Accessibility permission is granted: without it
        # osascript returns nothing and this line would read "(неизвестно)".
        log("focus: %s" % (front or "(неизвестно - нет доступа к Accessibility)"))
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
            # "banner": slides in from the top right, click to cancel (default).
            # "dialog": modal window in the centre, nothing happens without a
            # explicit click. Stricter, but it interrupts you.
            if CONFIG.get("confirm_mode", "banner") == "dialog":
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
                notify("Понижен приоритет",
                       "%s (%.0f%% CPU, не в фокусе)" % (p["app"], p["cpu"]), sound=CONFIG.get("notify_sound", "Submarine") or None)
            elif CONFIG.get("notify_before_action", True) and not incident.warned_action:
                # One "I am acting now" notification per incident, not per app.
                incident.warned_action = True
                notify("Система под нагрузкой",
                       "Понижаю приоритет фоновых приложений: %s. Вернётся при переключении в них."
                       % p["app"],
                       subtitle="%s грузит %.0f%% CPU в фоне" % (p["app"], p["cpu"]), sound=CONFIG.get("notify_sound", "Submarine") or None)

    # 3) memory pressure
    level = memory_pressure_level()
    if level is not None and level >= CONFIG["pressure_critical_level"]:
        incident = incident or Incident()
        incident.pressure_peak = max(incident.pressure_peak, level)
        incident.top_mem = top_memory_processes(5)
        alert("Критическое давление на память",
              "Уровень 4 из 4 - сейчас будет тормозить.\n\nБольше всего занимают:\n"
              + "\n".join(incident.top_mem[:5]))
        log("memory pressure critical, top: %s" % incident.top_mem)
    elif level is not None and level >= CONFIG["pressure_warn_level"]:
        # Early warning: the Mac is starting to struggle but is not critical yet.
        if CONFIG.get("notify_lag_warning", True):
            incident = incident or Incident()
            if not incident.warned_lag:
                incident.warned_lag = True
                incident.top_mem = top_memory_processes(5)
                notify("Память под давлением",
                       "Больше всего занимают: " + "; ".join(incident.top_mem[:3]),
                       subtitle="уровень %d из 4 - возможны подтормаживания" % level,
                       key="lag-warning", sound=CONFIG.get("notify_sound", "Submarine") or None)
        if incident:
            incident.pressure_peak = max(incident.pressure_peak, level)

    # 4) system errors / crash reports
    crashes, state["seen_crashes"] = new_crash_reports(state["seen_crashes"])
    if crashes:
        incident = incident or Incident()
        incident.crashes.extend(crashes)
        alert("Системная ошибка",
              "Появились новые крэш-репорты:\n" + "\n".join(crashes[:5]))
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
        write_report(facts, "(ИИ-отчёты отключены в конфиге)")
        return
    if since_last < cooldown:
        log("report skipped: cooldown, %d min left" % int((cooldown - since_last) / 60))
        write_report(facts, "(отчёт пропущен: cooldown подписки)")
        return

    who = ", ".join(incident.throttled) or "давление на память"
    notify("Спрашиваю Claude", "Инцидент закончился (%s). Запрашиваю разбор." % who,
           subtitle="через подписку, не через API", key="asking-claude", sound=CONFIG.get("notify_sound", "Submarine") or None)
    report = ask_claude(facts)
    if report:
        write_report(facts, report)
        state["last_report_ts"] = time.time()
        save_state(state)
        notify("Отчёт от Claude готов", report.splitlines()[0][:180],
               subtitle="нажми, чтобы открыть историю" if shutil.which("terminal-notifier")
               else "история: ~/.local/share/stability-guard/history.md",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound", "Submarine") or None)
        log("report written")
    else:
        write_report(facts, "(Claude недоступен, отчёт не получен)")
        notify("Claude не ответил", "Факты инцидента всё равно записаны в историю.",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound", "Submarine") or None)


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

    log("stability-guard started (poll=%ss, whitelist=%s)"
        % (CONFIG["poll_seconds"], ", ".join(CONFIG["whitelist"])))
    notify("Stability Guard", "Демон запущен. Слежу за фокусом, памятью и ошибками.")

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
        except Exception as e:
            # A bug in one tick must never take the daemon down.
            log("tick error: %s" % e)

        time.sleep(CONFIG["poll_seconds"])


def selfcheck():
    """Minimal runnable check: sensors answer, incident text is built correctly."""
    assert memory_pressure_level() in (1, 2, 4), "sysctl pressure level unreadable"
    assert isinstance(list_gui_processes(), list)
    assert len(top_memory_processes(5)) > 0, "ps returned nothing"

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

    print("selfcheck OK (pressure=%s, gui procs=%d, known crashes=%d)"
          % (memory_pressure_level(), len(list_gui_processes()), len(seen)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        selfcheck()
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-notify":
        # Sends one of every notification the daemon can produce, so you can
        # check that they actually reach Notification Center.
        CONFIG = load_config()
        CONFIG["notify_dedupe_seconds"] = 0
        globals()["CONFIG"] = CONFIG
        notify("Память под давлением", "Больше всего занимают: Docker 4100 MB; Chrome 2300 MB",
               subtitle="уровень 2 из 4 - возможны подтормаживания",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("Система под нагрузкой",
               "Понижаю приоритет фоновых приложений: Docker. Вернётся при переключении в них.",
               subtitle="Docker грузит 42% CPU в фоне",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("Критическое давление на память", "Docker 4100 MB; ollama 1900 MB",
               subtitle="уровень 4 из 4 - сейчас будет тормозить",
               sound=CONFIG.get("notify_sound_critical") or None)
        time.sleep(2)
        notify("Системная ошибка", "Новые крэш-репорты: Example_2026-01-01.ips",
               sound=CONFIG.get("notify_sound_critical") or None)
        time.sleep(2)
        notify("Спрашиваю Claude", "Инцидент закончился (Docker). Запрашиваю разбор.",
               subtitle="через подписку, не через API",
               sound=CONFIG.get("notify_sound") or None)
        time.sleep(2)
        notify("Отчёт от Claude готов", "Пример строки отчёта.",
               subtitle="история: ~/.local/share/stability-guard/history.md",
               open_path=HISTORY_PATH, sound=CONFIG.get("notify_sound") or None)
        print("Отправлено 6 уведомлений. Проверь Центр уведомлений.")
    else:
        main()
