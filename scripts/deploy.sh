#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — Deploy runtime artifact
#
# Copies only the files needed at runtime into the implementation directory.
# The implementation directory is a plain artifact (no nested git repository),
# so it can never diverge from the build tree.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUILD_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
DEPLOY_DIR="${1:-/home/jagdish/Desktop/System Files/Linux Data Guardian}"

if [ ! -d "$DEPLOY_DIR" ]; then
    echo "[!] Destination does not exist: $DEPLOY_DIR" >&2
    exit 1
fi

if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "[!] $DEPLOY_DIR is still a git repository." >&2
    echo "    Commit and push first, back it up, then remove its .git directory." >&2
    echo "    This script refuses to sync into a second clone to avoid divergence." >&2
    exit 1
fi

command -v rsync >/dev/null 2>&1 || { echo "[!] rsync is required." >&2; exit 1; }

echo "[*] Deploying ${BUILD_ROOT} -> ${DEPLOY_DIR}"

rsync -a --delete \
    --include '/src/***' \
    --include '/scripts/***' \
    --include '/config/***' \
    --include '/systemd/***' \
    --include '/assets/***' \
    --exclude '*' \
    "${BUILD_ROOT}/" "${DEPLOY_DIR}/"

# Keep the runtime tree free of byte-code and test caches.
find "$DEPLOY_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEPLOY_DIR" -name '*.pyc' -delete 2>/dev/null || true

{
    echo "source=${BUILD_ROOT}"
    echo "commit=$(git -C "$BUILD_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "deployed_at=$(date -Iseconds)"
} > "${DEPLOY_DIR}/DEPLOYED"

echo "[+] Deployed runtime files."
echo "    Next: sudo ./scripts/install.sh --system  (from the deployed tree)"
