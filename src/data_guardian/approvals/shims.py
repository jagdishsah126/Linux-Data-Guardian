"""Generates and manages transparent CLI shims for pip, npm, cargo in ~/.local/bin/."""

from __future__ import annotations
import stat
from pathlib import Path
from typing import List

SHIM_TEMPLATE = """#!/usr/bin/env bash
# ==============================================================================
# Linux Data Guardian — CLI Shim for {tool}
# Auto-generated. Do not edit manually.
# ==============================================================================

# Invoke Data Guardian approval gate
PYTHONPATH="{project_src}:${{PYTHONPATH:-}}" /usr/bin/python3 -m src.data_guardian.approvals.gate --tool "{tool}" "$@"
GATE_EXIT=$?

if [ $GATE_EXIT -ne 0 ]; then
    exit $GATE_EXIT
fi

# Locate the actual binary, skipping this shim
THIS_SCRIPT=$(realpath "$0")
REAL_BIN=""

# Read all instances from PATH
while IFS= read -r candidate; do
    REAL_CANDIDATE=$(realpath "$candidate" 2>/dev/null || true)
    if [ "$REAL_CANDIDATE" != "$THIS_SCRIPT" ] && [ -x "$candidate" ]; then
        REAL_BIN="$candidate"
        break
    fi
done < <(which -a "{tool}" 2>/dev/null || true)

if [ -n "$REAL_BIN" ]; then
    exec "$REAL_BIN" "$@"
else
    echo "[!] Error: Underlying {tool} binary not found in PATH." >&2
    exit 127
fi
"""

SUPPORTED_TOOLS = ["pip", "pip3", "npm", "cargo"]


def install_shims(target_dir: Path | None = None) -> List[Path]:
    """Installs shims in target directory (defaults to ~/.local/bin)."""
    dest = target_dir or (Path.home() / ".local" / "bin")
    dest.mkdir(parents=True, exist_ok=True)
    project_src = Path(__file__).resolve().parent.parent.parent.parent

    installed: List[Path] = []
    for tool in SUPPORTED_TOOLS:
        shim_path = dest / tool
        content = SHIM_TEMPLATE.format(tool=tool, project_src=str(project_src))
        with open(shim_path, "w", encoding="utf-8") as f:
            f.write(content)
        # Make executable (rwxr-xr-x)
        shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(shim_path)

    return installed


def remove_shims(target_dir: Path | None = None) -> None:
    """Removes shims from target directory."""
    dest = target_dir or (Path.home() / ".local" / "bin")
    for tool in SUPPORTED_TOOLS:
        shim_path = dest / tool
        if shim_path.exists():
            try:
                # Only remove if it contains our identifier
                content = shim_path.read_text(encoding="utf-8", errors="ignore")
                if "Linux Data Guardian" in content:
                    shim_path.unlink()
            except Exception as e:
                print(f"[!] Warning removing shim {shim_path}: {e}")
