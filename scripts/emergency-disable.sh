#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Emergency bypass
#
# Removes every firewall restriction immediately. Prefers asking the running
# daemon (which keeps its state consistent) and falls back to a direct flush.
# Never unlinks the daemon's socket.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
HELPER="/usr/local/libexec/data-guardian-helper"

echo "[*] Linux Data Guardian — emergency bypass"

# 1. Ask the daemon first: it resets profile state, quota flags and rules together.
if PYTHONPATH="$PROJECT_ROOT" /usr/bin/python3 -m src.data_guardian.main bypass; then
    echo "[+] Daemon handled the bypass."
    exit 0
fi

# 2. Fall back to a direct flush (daemon offline or unresponsive).
if [ -x "$HELPER" ]; then
    if sudo -n "$HELPER" --flush; then
        echo "[+] Firewall table flushed."
        exit 0
    fi
    if sudo "$HELPER" --flush; then
        echo "[+] Firewall table flushed."
        exit 0
    fi
fi

echo "[!] Could not reach the daemon or the helper." >&2
echo "    Try: sudo /usr/local/libexec/data-guardian-helper --flush" >&2
exit 1
