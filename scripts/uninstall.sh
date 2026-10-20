#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Uninstaller
#
#   ./scripts/uninstall.sh            # remove app + system integration, keep data
#   ./scripts/uninstall.sh --purge    # also delete config and usage history
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[ -n "${TARGET_HOME:-}" ] || { echo "[!] Cannot resolve home for '${TARGET_USER}'." >&2; exit 1; }

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help) echo "Usage: $0 [--purge]"; exit 0 ;;
        *) echo "[!] Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

FAILURES=0
step() {
    if ! "$@"; then
        echo "[!] Step failed: $*" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

echo "=================================================="
echo "  Uninstalling Linux Data Guardian (${TARGET_USER})"
echo "=================================================="

# 1. Stop the daemon first so it cannot re-apply rules.
if [ -f /etc/systemd/system/data-guardian.service ]; then
    step sudo systemctl stop data-guardian.service
    step sudo systemctl disable data-guardian.service
    step sudo rm -f /etc/systemd/system/data-guardian.service
    step sudo systemctl daemon-reload
fi

# 2. Flush the firewall while the helper still exists.
if [ -x /usr/local/libexec/data-guardian-helper ]; then
    step sudo -n /usr/local/libexec/data-guardian-helper --flush || \
        echo "[i] Firewall flush skipped (helper not reachable without a password)."
fi

# 3. Remove privileged components.
step sudo rm -f /usr/local/libexec/data-guardian-helper
step sudo rm -f /etc/sudoers.d/linux-data-guardian
step sudo rm -f /etc/apt/apt.conf.d/99data-guardian
step sudo rm -f /usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png

# 4. Remove shims as the target user, with a correct working directory.
if [ -d "${PROJECT_ROOT}/src/data_guardian" ]; then
    if [ "$(id -u)" -eq 0 ]; then
        step sudo -u "$TARGET_USER" env HOME="$TARGET_HOME" PYTHONPATH="$PROJECT_ROOT" \
            /usr/bin/python3 -m src.data_guardian.main remove-shims
    else
        step env HOME="$TARGET_HOME" PYTHONPATH="$PROJECT_ROOT" \
            /usr/bin/python3 -m src.data_guardian.main remove-shims
    fi
fi

# 5. Remove user integration.
rm -f "${TARGET_HOME}/.local/bin/data-guardian"
rm -f "${TARGET_HOME}/.config/autostart/data-guardian-tray.desktop"
rm -f "${TARGET_HOME}/.local/share/applications/data-guardian.desktop"

# 6. Optional data purge.
if [ "$PURGE" -eq 1 ]; then
    rm -rf "${TARGET_HOME}/.config/linux-data-guardian"
    rm -rf "${TARGET_HOME}/.local/share/linux-data-guardian"
    echo "[+] Removed configuration and usage history."
else
    echo "[i] Kept ${TARGET_HOME}/.config/linux-data-guardian and usage history."
    echo "    Re-run with --purge to delete them."
fi

if [ "$FAILURES" -ne 0 ]; then
    echo ""
    echo "[!] Completed with ${FAILURES} failed step(s); review the output above." >&2
    exit 1
fi

echo ""
echo "[✓] Uninstalled. System networking is unaffected."
