#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Installation Script
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

echo "=================================================="
echo "    Installing Linux Data Guardian for XFCE       "
echo "=================================================="

# 1. Install user command symlink into ~/.local/bin
mkdir -p "${HOME}/.local/bin"
cat << EOF > "${HOME}/.local/bin/data-guardian"
#!/usr/bin/env bash
PROJECT_DIR="${PROJECT_ROOT}"
PYTHONPATH="\${PROJECT_DIR}:\${PYTHONPATH:-}" /usr/bin/python3 -m src.data_guardian.main "\$@"
EOF
chmod +x "${HOME}/.local/bin/data-guardian"
echo "[+] Installed 'data-guardian' command in ~/.local/bin/data-guardian (pointing to ${PROJECT_ROOT})"

# 2. Install CLI shims (pip, npm, cargo)
echo "[*] Installing developer tool shims in ~/.local/bin..."
python3 -m src.data_guardian.main install-shims

# 3. Install XFCE Autostart desktop entry
AUTOSTART_DIR="${HOME}/.config/autostart"
mkdir -p "${AUTOSTART_DIR}"
cp "${PROJECT_ROOT}/systemd/data-guardian-tray.desktop" "${AUTOSTART_DIR}/"
echo "[+] Registered XFCE system tray autostart entry."

# 4. Check for system-level installation request
if [ "${1:-}" = "--system" ] || [ "$(id -u)" -eq 0 ]; then
    echo "[*] Installing systemd service and APT download gate (requires root)..."
    
    # Install APT hook
    mkdir -p /etc/apt/apt.conf.d
    cat << EOF > /etc/apt/apt.conf.d/99data-guardian
// Linux Data Guardian Pre-Download Gating Hook
APT::Acquire::Pre-Invoke {
    "PYTHONPATH='${PROJECT_ROOT}' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt install || exit 1";
};
APT::Update::Pre-Invoke {
    "PYTHONPATH='${PROJECT_ROOT}' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt update || exit 1";
};
EOF
    echo "[+] Installed /etc/apt/apt.conf.d/99data-guardian"

    # Install Systemd Service
    cp "${PROJECT_ROOT}/systemd/data-guardian.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable data-guardian.service
    systemctl restart data-guardian.service
    echo "[+] Enabled and started data-guardian.service"
else
    echo ""
    echo "[i] User-level installation complete!"
    echo "    To enable kernel-level nftables enforcement and APT gating, run:"
    echo "    sudo ./scripts/install.sh --system"
fi

echo ""
echo "[✓] Installation finished successfully!"
echo "    - Run 'data-guardian status' to see current state."
echo "    - Run 'data-guardian dashboard' to open the GTK3 control window."
echo "    - Run 'data-guardian tray' to start the system tray icon."
