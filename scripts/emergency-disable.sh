#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Emergency Disable & Recovery Script
#
# Purpose: Instantly remove all data guardian firewall rules, policy restrictions,
#          and temporary blocks. Restores 100% normal unrestricted networking.
#
# Safety:  Operates ONLY on the isolated 'data_guardian' nftables table.
#          Does NOT modify, flush, or interfere with system UFW or other chains.
# ==============================================================================

set -euo pipefail

echo "[*] Linux Data Guardian: Initiating emergency disable..."

# 1. Flush and delete the dedicated nftables table
if command -v nft >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        sudo nft delete table inet data_guardian 2>/dev/null && echo "[+] Removed nftables table 'data_guardian'." || echo "[i] No active nftables table 'data_guardian' found."
    else
        echo "[!] Sudo required to remove nftables rules. Attempting with sudo:"
        sudo nft delete table inet data_guardian 2>/dev/null && echo "[+] Removed nftables table 'data_guardian'." || echo "[i] No active nftables table 'data_guardian' found."
    fi
else
    echo "[i] nftables not found on this system."
fi

# 2. Release any local socket or lock files
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
LOCK_FILE="${RUNTIME_DIR}/data-guardian.lock"
SOCK_FILE="${RUNTIME_DIR}/data-guardian.sock"

if [ -f "${LOCK_FILE}" ]; then
    rm -f "${LOCK_FILE}"
    echo "[+] Removed runtime lock file."
fi

if [ -S "${SOCK_FILE}" ]; then
    rm -f "${SOCK_FILE}"
    echo "[+] Removed runtime socket."
fi

# 3. Log recovery event if SQLite database exists
DB_PATH="${HOME}/.local/share/linux-data-guardian/guardian.db"
if [ -f "${DB_PATH}" ] && command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${DB_PATH}" "INSERT INTO approval_events (timestamp, tool, packages, estimated_bytes, decision, resolved_at) VALUES (datetime('now'), 'emergency-disable', 'recovery_script', 0, 'emergency_bypass', datetime('now'));" 2>/dev/null || true
    echo "[+] Recorded emergency bypass event in database."
fi

# 4. Notify desktop user if desktop session is active
if command -v notify-send >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
    notify-send -u critical "Linux Data Guardian" "Emergency Disable executed. All network restrictions have been removed." || true
fi

echo "[✓] Emergency recovery complete. Network access is fully restored."
