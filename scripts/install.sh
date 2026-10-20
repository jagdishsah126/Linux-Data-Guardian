#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Installer
#
#   ./scripts/install.sh              # user-level (wrapper, shims, autostart, icon)
#   sudo ./scripts/install.sh --system  # system-level (helper, sudoers, service)
#
# Safe to re-run: every step is idempotent. The installer never writes an APT
# hook and never imports user-controlled code as root.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
HELPER_DEST="/usr/local/libexec/data-guardian-helper"
SUDOERS_DEST="/etc/sudoers.d/linux-data-guardian"
UNIT_DEST="/etc/systemd/system/data-guardian.service"
ICON_DEST="/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png"

# Resolve the human user even when the script is run through sudo.
TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [ -z "${TARGET_HOME:-}" ]; then
    echo "[!] Could not resolve home directory for user '${TARGET_USER}'." >&2
    exit 1
fi

SYSTEM_MODE=0
for arg in "$@"; do
    case "$arg" in
        --system) SYSTEM_MODE=1 ;;
        -h|--help)
            echo "Usage: $0 [--system]"
            exit 0
            ;;
        *) echo "[!] Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ] && [ "$SYSTEM_MODE" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
    echo "[!] Refusing to run as root without SUDO_USER: user files would land in /root." >&2
    echo "    Run as your normal user, or use: sudo $0 --system" >&2
    exit 1
fi

echo "=================================================="
echo "  Linux Data Guardian installer (user: ${TARGET_USER})"
echo "=================================================="

render() {
    # render <template> <destination>
    sed -e "s|@USER@|${TARGET_USER}|g" \
        -e "s|@HOME@|${TARGET_HOME}|g" \
        -e "s|@PROJECT_DIR@|${PROJECT_ROOT}|g" \
        "$1" > "$2"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "[!] Missing required command: $1" >&2; exit 1; }
}

preflight() {
    local missing=0
    for cmd in python3 nmcli nft systemctl; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "[!] Missing dependency: $cmd" >&2
            missing=1
        fi
    done
    python3 - <<'PY' || missing=1
import sys
try:
    import yaml
except Exception:
    print("[!] Missing Python module: yaml (install python3-yaml)", file=sys.stderr)
    sys.exit(1)
PY
    if [ "$missing" -ne 0 ]; then
        echo "[!] Install the missing dependencies and re-run." >&2
        exit 1
    fi
}

install_user_files() {
    local bin_dir="${TARGET_HOME}/.local/bin"
    local autostart_dir="${TARGET_HOME}/.config/autostart"
    local apps_dir="${TARGET_HOME}/.local/share/applications"
    local config_dir="${TARGET_HOME}/.config/linux-data-guardian"

    mkdir -p "$bin_dir" "$autostart_dir" "$apps_dir" "$config_dir"

    cat > "${bin_dir}/data-guardian" <<EOF
#!/usr/bin/env bash
PROJECT_DIR="${PROJECT_ROOT}"
PYTHONPATH="\${PROJECT_DIR}:\${PYTHONPATH:-}" exec /usr/bin/python3 -m src.data_guardian.main "\$@"
EOF
    chmod 0755 "${bin_dir}/data-guardian"
    chown "${TARGET_USER}:${TARGET_USER}" "${bin_dir}/data-guardian" 2>/dev/null || true

    # Seed the user config once; never overwrite an existing one.
    if [ ! -f "${config_dir}/config.yaml" ]; then
        install -m 0600 "${PROJECT_ROOT}/config/config.default.yaml" "${config_dir}/config.yaml"
        chown "${TARGET_USER}:${TARGET_USER}" "${config_dir}/config.yaml" 2>/dev/null || true
    fi
    if [ ! -f "${config_dir}/whitelist.yaml" ]; then
        install -m 0600 "${PROJECT_ROOT}/config/whitelist.yaml" "${config_dir}/whitelist.yaml"
        chown "${TARGET_USER}:${TARGET_USER}" "${config_dir}/whitelist.yaml" 2>/dev/null || true
    fi

    render "${PROJECT_ROOT}/systemd/data-guardian-tray.desktop" "${autostart_dir}/data-guardian-tray.desktop"
    render "${PROJECT_ROOT}/systemd/data-guardian.desktop" "${apps_dir}/data-guardian.desktop"
    chown "${TARGET_USER}:${TARGET_USER}" \
        "${autostart_dir}/data-guardian-tray.desktop" "${apps_dir}/data-guardian.desktop" 2>/dev/null || true

    # Shims must be written as the target user (they reference their own home).
    # Only re-drop privileges when the installer itself runs as root; a normal
    # user install must never prompt for a sudo password here.
    if [ "$(id -u)" -eq 0 ]; then
        sudo -u "$TARGET_USER" env HOME="$TARGET_HOME" PYTHONPATH="$PROJECT_ROOT" \
            /usr/bin/python3 -m src.data_guardian.main install-shims
    else
        HOME="$TARGET_HOME" PYTHONPATH="$PROJECT_ROOT" \
            /usr/bin/python3 -m src.data_guardian.main install-shims
    fi
    echo "[+] User files installed for ${TARGET_USER}."
}

install_system_files() {
    [ "$(id -u)" -eq 0 ] || { echo "[!] --system requires root." >&2; exit 1; }

    require_cmd visudo
    install -d -m 0755 /usr/local/libexec
    install -m 0755 -o root -g root "${PROJECT_ROOT}/scripts/data-guardian-helper" "$HELPER_DEST"

    render "${PROJECT_ROOT}/config/sudoers-data-guardian" "${SUDOERS_DEST}.tmp"
    chmod 0440 "${SUDOERS_DEST}.tmp"
    if ! visudo -cf "${SUDOERS_DEST}.tmp" >/dev/null; then
        rm -f "${SUDOERS_DEST}.tmp"
        echo "[!] Generated sudoers file failed validation; aborting." >&2
        exit 1
    fi
    mv "${SUDOERS_DEST}.tmp" "$SUDOERS_DEST"
    chmod 0440 "$SUDOERS_DEST"
    echo "[+] Installed helper and sudoers rule."

    render "${PROJECT_ROOT}/systemd/data-guardian.service" "$UNIT_DEST"
    chmod 0644 "$UNIT_DEST"

    install -D -m 0644 "${PROJECT_ROOT}/assets/icon.png" "$ICON_DEST"
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
    fi

    # Sanity check the helper before enabling the service.
    "$HELPER_DEST" --status >/dev/null 2>&1 || true
    systemctl daemon-reload
    systemctl enable data-guardian.service >/dev/null
    systemctl restart data-guardian.service
    echo "[+] Installed and restarted data-guardian.service."
}

preflight

if [ "$SYSTEM_MODE" -eq 1 ]; then
    install_system_files
fi

install_user_files

echo ""
echo "[✓] Installation complete."
echo "    - data-guardian status      # current state"
echo "    - data-guardian doctor      # health checks"
echo "    - data-guardian dashboard   # GTK dashboard"
echo "    - data-guardian set-hotspot # choose your metered network"
