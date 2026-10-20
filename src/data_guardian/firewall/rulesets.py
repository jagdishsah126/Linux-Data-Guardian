"""Single source of truth for nftables ruleset rendering.

The privileged helper only *validates and applies* what this module renders
(``helper --apply-stdin``), so there is exactly one implementation of the rules
and it is unit-testable without root.

Every rendered ruleset begins with ``destroy table inet data_guardian`` inside the
same transaction, which makes application idempotent: re-applying replaces the
table instead of appending duplicate rules.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

TABLE = "data_guardian"
FOREGROUND_CGROUP = "data_guardian/foreground"
WHITELIST_CGROUP = "data_guardian/whitelist"

# Interface names accepted by the kernel: keep the same shape as the helper.
IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}\Z")

DNS_DHCP_UDP = (53, 67, 68)
BOOTP_TCP = (53,)
NTP_UDP = (123,)


def is_valid_interface(name: str) -> bool:
    return bool(name) and bool(IFACE_RE.match(name))


def _ports(values: Iterable[int]) -> str:
    return "{" + ", ".join(str(int(p)) for p in values) + "}"


def build_ruleset(
    *,
    profile: str,
    interface: str = "",
    quota_exceeded: bool = False,
    apt_window: bool = False,
    strict_use_cgroups: bool = True,
    extra_tcp_ports: Optional[List[int]] = None,
    allow_ntp: bool = True,
    cgroup_base: str = "data_guardian",
    cgroup_foreground: Optional[str] = None,
    cgroup_whitelist: Optional[str] = None,
) -> str:
    """Render the nftables ruleset for a profile.

    ``profile`` is one of ``balanced``, ``strict``, ``off``.
    ``quota_exceeded`` overrides the profile with the hard-cutoff ruleset.

    ``cgroup_foreground`` / ``cgroup_whitelist`` override the default
    ``<cgroup_base>/foreground`` paths (relative to the cgroup2 mount). nft
    validates that these paths exist, so tests running without a delegated
    subtree can point at real cgroups.
    """
    if profile not in ("balanced", "strict", "off"):
        raise ValueError(f"Invalid profile: {profile!r}")

    # "off" (and no quota block) means: make sure the table is gone.
    if profile == "off" and not quota_exceeded:
        return f"destroy table inet {TABLE}\n"

    if not is_valid_interface(interface):
        raise ValueError(f"Invalid or missing interface: {interface!r}")

    lines: List[str] = [
        f"destroy table inet {TABLE}",
        f"table inet {TABLE} {{",
        "    chain output {",
        "        type filter hook output priority 0; policy accept;",
        "",
        "        oif \"lo\" accept",
    ]

    if quota_exceeded:
        # Hard cutoff: allow only DHCP/DNS so the link stays usable, drop the rest.
        lines += [
            "",
            "        # Quota exhausted: only local network services survive",
            f"        udp dport {_ports(DNS_DHCP_UDP)} accept",
            f"        tcp dport {_ports(BOOTP_TCP)} accept",
        ]
        if apt_window:
            lines += [
                "",
                "        # Temporary APT window on the metered interface",
                f"        oif \"{interface}\" skuid 42 ct state new accept",
            ]
        lines += [
            "",
            "        # Drop everything else, including in-flight transfers",
            f"        oif \"{interface}\" drop",
        ]
    else:
        udp = list(DNS_DHCP_UDP) + (list(NTP_UDP) if allow_ntp else [])
        lines += [
            "",
            "        ct state established,related accept",
            "        ct state invalid drop",
            "",
            f"        udp dport {_ports(udp)} accept",
            f"        tcp dport {_ports(BOOTP_TCP)} accept",
        ]

        if profile == "strict" and strict_use_cgroups:
            fg_path = cgroup_foreground or f"{cgroup_base}/foreground"
            wl_path = cgroup_whitelist or f"{cgroup_base}/whitelist"
            # `level` is the number of path components from the cgroup2 root.
            fg_level = len([p for p in fg_path.split("/") if p])
            wl_level = len([p for p in wl_path.split("/") if p])
            lines += [
                "",
                "        # Strict: only sockets owned by the foreground app or a whitelisted tool",
                f'        socket cgroupv2 level {fg_level} "{fg_path}" ct state new accept',
                f'        socket cgroupv2 level {wl_level} "{wl_path}" ct state new accept',
            ]

        if apt_window:
            lines += [
                "",
                "        # Temporary APT window on the metered interface",
                f"        oif \"{interface}\" skuid 42 ct state new accept",
            ]

        if profile == "strict" and not strict_use_cgroups:
            # Fallback when cgroup v2 delegation is unavailable: port allowlist.
            ports = extra_tcp_ports or [80, 443, 22]
            lines += [
                "",
                "        # Strict fallback: web/SSH only (cgroup matching unavailable)",
                f"        oif \"{interface}\" tcp dport {_ports(ports)} ct state new accept",
            ]

        lines += [
            "",
            "        # Background package-manager downloads are never welcome on mobile data",
            f"        oif \"{interface}\" skuid 42 ct state new drop",
        ]

        if profile == "strict":
            lines += [
                "",
                "        # Anything else that tries to open a new connection on the metered link",
                f"        oif \"{interface}\" ct state new drop",
            ]

    lines += ["    }", "}", ""]
    return "\n".join(lines)
