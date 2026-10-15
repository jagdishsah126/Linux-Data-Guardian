"""APT hook manager for Linux Data Guardian."""

from __future__ import annotations
from pathlib import Path

APT_HOOK_PATH = Path("/etc/apt/apt.conf.d/99data-guardian")

APT_CONF_TEMPLATE = """// ==============================================================================
// Linux Data Guardian — Pre-Download Gating Hook
// Auto-generated. Do not edit manually.
// ==============================================================================

// Gate package downloads before archives are fetched
APT::Acquire::Pre-Invoke {{
    "PYTHONPATH='{project_src}' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt install || exit 1";
}};

// Gate cache refreshes when on metered hotspot
APT::Update::Pre-Invoke {{
    "PYTHONPATH='{project_src}' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt update || exit 1";
}};
"""


def generate_apt_config(project_src: Path | None = None) -> str:
    """Generates the APT configuration content."""
    root = project_src or Path(__file__).resolve().parent.parent.parent.parent
    return APT_CONF_TEMPLATE.format(project_src=str(root))
