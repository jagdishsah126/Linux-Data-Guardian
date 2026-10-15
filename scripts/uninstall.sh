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

# 4. Remove CLI shims
echo "[*] Removing CLI shims from ~/.local/bin..."
python3 -m src.data_guardian.main remove-shims 2>/dev/null || true

# 5. Remove command symlink and desktop entry
rm -f "${HOME}/.local/bin/data-guardian"
rm -f "${HOME}/.config/autostart/data-guardian-tray.desktop"
echo "[+] Removed command and autostart entries."

echo ""
echo "[✓] Linux Data Guardian uninstalled cleanly. System network is unaffected."
