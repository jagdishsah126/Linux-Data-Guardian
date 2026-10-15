"""Generates scoped nftables rulesets for Linux Data Guardian."""

from __future__ import annotations
from typing import List, Optional

TABLE_NAME = "data_guardian"


def build_nft_ruleset(
    profile: str = "balanced",
    quota_exceeded: bool = False,
    allowed_ports_udp: Optional[List[int]] = None,
    allowed_ports_tcp: Optional[List[int]] = None,
    drop_system_update_uids: bool = True
) -> str:
    """Builds the atomic nftables configuration string for 'table inet data_guardian'.

    Profiles:
      - 'balanced': allows web surfing, DNS, DHCP, loopback; blocks automated update UIDs; gates package managers.
      - 'strict': foreground-only + whitelisted tools.
      - 'off': flushes the table.
    """
    if profile == "off" and not quota_exceeded:
        return f"delete table inet {TABLE_NAME}\n"

    udp_ports = allowed_ports_udp or [53, 67, 68, 123]
    tcp_ports = allowed_ports_tcp or [53]
    udp_ports_str = "{" + ", ".join(str(p) for p in udp_ports) + "}"
    tcp_ports_str = "{" + ", ".join(str(p) for p in tcp_ports) + "}"

    lines = [
        f"table inet {TABLE_NAME} {{",
        "    chain output {",
        "        type filter hook output priority 0; policy accept;",
        "",
        "        # 1. Always accept loopback traffic",
        "        oif \"lo\" accept",
        "",
        "        # 2. Fast-path: accept established and related connections",
        "        ct state established,related accept",
        "",
        "        # 3. Drop invalid connection states",
        "        ct state invalid drop",
        "",
        "        # 4. Essential network services: DNS, DHCP, NTP",
        f"        udp dport {udp_ports_str} accept",
        f"        tcp dport {tcp_ports_str} accept",
    ]

    # Quota exhaustion: Hard stop on new outbound internet connections
    if quota_exceeded:
        lines.extend([
            "",
            "        # Quota Exceeded: Drop all new outbound connections",
            "        ct state new drop",
        ])
    elif profile == "strict":
        lines.extend([
            "",
            "        # Strict Mode: Only allow foreground cgroup or whitelisted sockets",
            "        # Sockets belonging to data_guardian/foreground or data_guardian/whitelist are accepted",
            "        socket cgroupv2 level 2 \"data_guardian/foreground\" accept",
            "        socket cgroupv2 level 2 \"data_guardian/whitelist\" accept",
            "        # Block other new outbound connections",
            "        ct state new drop",
        ])
    elif profile == "balanced":
        if drop_system_update_uids:
            lines.extend([
                "",
                "        # Balanced Mode: Silence background APT sandbox downloads",
                "        # UID 42 (_apt) is dropped unless explicitly permitted",
                "        skuid 42 ct state new drop",
            ])

    lines.extend([
        "    }",
        "}",
    ])

    return "\n".join(lines) + "\n"
