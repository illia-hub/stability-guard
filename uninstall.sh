#!/bin/bash
# Uninstaller. Removes the launch agent and the daemon script.
# Your logs, history and config are kept unless you explicitly confirm.
set -euo pipefail

LABEL="com.user.stability-guard"
INSTALL_DIR="$HOME/.local/share/stability-guard"
CONFIG_DIR="$HOME/.config/stability-guard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> stopping $LABEL"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || echo "    (was not running)"

# Restore priority of every GUI process we may have lowered.
# Harmless if nothing was throttled.
echo "==> restoring process priorities"
for pid in $(ps -axo pid=,comm= | awk '/\.app\/Contents\/MacOS\//{print $1}'); do
    taskpolicy -B -p "$pid" 2>/dev/null || true
done

/bin/rm -f "$PLIST" "$INSTALL_DIR/stability_guard.py"
echo "==> agent removed"

echo
echo "История и логи НЕ удалены: $INSTALL_DIR"
echo "Конфиг НЕ удалён:          $CONFIG_DIR/config.json"
read -r -p "Удалить их тоже? [y/N] " answer
if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    /bin/rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
    echo "    удалено."
else
    echo "    оставлено."
fi
