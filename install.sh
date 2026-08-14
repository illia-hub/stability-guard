#!/bin/bash
# Installer for stability-guard. Idempotent: safe to run again.
set -euo pipefail

LABEL="com.user.stability-guard"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.local/share/stability-guard"
CONFIG_DIR="$HOME/.config/stability-guard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> stability-guard installer"

# 1. python3. Prefer the system one: a daemon must not depend on a conda or
# homebrew install that the user may upgrade or remove later.
if [ -x /usr/bin/python3 ]; then
    PYTHON=/usr/bin/python3
else
    PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
    echo "ERROR: python3 not found. Install Xcode Command Line Tools: xcode-select --install"
    exit 1
fi
echo "    python3: $PYTHON"

# 2. claude CLI. Skip temporary shim paths - launchd will not see them.
CLAUDE=""
for candidate in "$HOME/.local/bin/claude" "/opt/homebrew/bin/claude" "/usr/local/bin/claude" "$HOME/.claude/local/claude"; do
    if [ -x "$candidate" ]; then CLAUDE="$candidate"; break; fi
done
if [ -z "$CLAUDE" ]; then
    FOUND="$(command -v claude || true)"
    case "$FOUND" in
        /var/folders/*|/tmp/*) echo "    WARNING: only a temporary shim found at $FOUND - ignoring." ;;
        "") : ;;
        *) CLAUDE="$FOUND" ;;
    esac
fi
if [ -z "$CLAUDE" ]; then
    echo "    WARNING: claude CLI not found. Daemon will run, but AI reports will be skipped."
    echo "             Install Claude Code, then set \"claude_bin\" in $CONFIG_DIR/config.json"
    CLAUDE="claude"
else
    echo "    claude:  $CLAUDE"
fi

# 3. directories
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$HOME/Library/LaunchAgents"

# 4. script
cp "$SRC_DIR/stability_guard.py" "$INSTALL_DIR/stability_guard.py"
chmod +x "$INSTALL_DIR/stability_guard.py"
echo "    script:  $INSTALL_DIR/stability_guard.py"

# 5. config - never overwrite an existing one
if [ -f "$CONFIG_DIR/config.json" ]; then
    # Keep every value the user set, only add keys introduced by a newer version.
    "$PYTHON" - "$SRC_DIR/config.json" "$CONFIG_DIR/config.json" <<'PY'
import json, sys
template, current = sys.argv[1], sys.argv[2]
try:
    tpl = json.load(open(template))
    cur = json.load(open(current))
except Exception as e:
    print("    config:  kept as is (%s)" % e)
    sys.exit(0)
added = [k for k in tpl if k not in cur]
if added:
    cur.update({k: tpl[k] for k in added})
    json.dump(cur, open(current, "w"), ensure_ascii=False, indent=2)
    print("    config:  kept existing, added new keys: %s" % ", ".join(added))
else:
    print("    config:  kept existing %s" % current)
PY
else
    sed "s|\"claude_bin\": \"claude\"|\"claude_bin\": \"$CLAUDE\"|" \
        "$SRC_DIR/config.json" > "$CONFIG_DIR/config.json"
    echo "    config:  created $CONFIG_DIR/config.json"
fi

# 6. notification panel helper. Optional: without it the daemon falls back to
# system notifications. Needs swiftc from the Command Line Tools.
if command -v swiftc >/dev/null 2>&1; then
    if swiftc -O -o "$INSTALL_DIR/sgnotify" "$SRC_DIR/sgnotify.swift" 2>/dev/null; then
        echo "    panel:   $INSTALL_DIR/sgnotify"
    else
        echo "    WARNING: sgnotify did not compile, falling back to system notifications."
    fi
else
    echo "    NOTE: swiftc not found, using system notifications."
fi

# 7. plist. launchd does not inherit the shell environment, so PATH is written in.
LAUNCHD_PATH="$(dirname "$CLAUDE"):$(dirname "$PYTHON"):/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__SCRIPT__|$INSTALL_DIR/stability_guard.py|g" \
    -e "s|__PATH__|$LAUNCHD_PATH|g" \
    -e "s|__HOME__|$HOME|g" \
    "$SRC_DIR/$LABEL.plist" > "$PLIST"
echo "    plist:   $PLIST"

# 8. (re)load
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "    loaded."

echo
echo "Готово. Проверка:  launchctl list | grep stability-guard"
echo "Лог:               tail -f $INSTALL_DIR/daemon.log"
echo "История отчётов:   open $INSTALL_DIR/history.md"
echo
echo "ВАЖНО: при первом запуске macOS спросит разрешение на Accessibility"
echo "(System Settings > Privacy & Security > Accessibility) - без него osascript"
echo "не сможет узнать активное окно. Разреши для python3."
