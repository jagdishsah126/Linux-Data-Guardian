#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Clean Uninstallation Script
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

echo "=================================================="
echo "      Uninstalling Linux Data Guardian            "
echo "=================================================="

# 1. Flush any active firewall rules
echo "[*] Flushing firewall rules..."
"${PROJECT_ROOT}/scripts/emergency-disable.sh" || true

# 2. Stop and remove systemd service if installed
if [ -f "/etc/systemd/system/data-guardian.service" ]; then
    echo "[*] Stopping and disabling systemd service..."
    sudo systemctl stop data-guardian.service 2>/dev/null || true
    sudo systemctl disable data-guardian.service 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/data-guardian.service"
    sudo systemctl daemon-reload
    echo "[+] Removed data-guardian.service"
fi

# 3. Remove APT hook if installed
if [ -f "/etc/apt/apt.conf.d/99data-guardian" ]; then
    echo "[*] Removing APT pre-invoke hook..."
    sudo rm -f "/etc/apt/apt.conf.d/99data-guardian"
    echo "[+] Removed /etc/apt/apt.conf.d/99data-guardian"
fi

# 4. Remove Privileged Helper and Sudoers file if installed
if [ -f "/usr/local/libexec/data-guardian-helper" ] || [ -f "/etc/sudoers.d/linux-data-guardian" ]; then
    echo "[*] Removing privileged helper and sudoers configuration..."
    sudo rm -f "/usr/local/libexec/data-guardian-helper"
    sudo rm -f "/etc/sudoers.d/linux-data-guardian"
    echo "[+] Removed data-guardian-helper and sudoers rule."
fi

# 5. Remove CLI shims
echo "[*] Removing CLI shims from ~/.local/bin..."
python3 -m src.data_guardian.main remove-shims 2>/dev/null || true

# 6. Remove command symlink, desktop entry, and icon
rm -f "${HOME}/.local/bin/data-guardian"
rm -f "${HOME}/.config/autostart/data-guardian-tray.desktop"
rm -f "${HOME}/.local/share/applications/data-guardian.desktop"
if [ -f "/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png" ]; then
    sudo rm -f "/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png" 2>/dev/null || true
    sudo gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
fi
echo "[+] Removed command, autostart entries, and icons."

echo ""
echo "[✓] Linux Data Guardian uninstalled cleanly. System network is unaffected."
