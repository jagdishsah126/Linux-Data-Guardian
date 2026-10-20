"""Command line and GUI entry point for Linux Data Guardian."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.ipc import send_ipc_command
from src.data_guardian.network.byte_counter import format_bytes
from src.data_guardian.network.nm_monitor import NetworkMonitor
from src.data_guardian.quota import evaluate

VERSION = "2.0.0"
MB = 1024 * 1024


# --------------------------------------------------------------------- status

def gather_status() -> Dict[str, Any]:
    """Combine daemon IPC state (when available) with local database truth."""
    config_mgr = ConfigManager()
    db = Database(config_mgr.db_path, designated_uuid=config_mgr.designated_hotspot_uuid)

    ipc = send_ipc_command({"action": "get_status"}) or {}
    monitor = NetworkMonitor(
        designated_uuid=config_mgr.designated_hotspot_uuid,
        designated_name=config_mgr.designated_hotspot_name,
    )
    active = monitor.get_active_connection()
    is_hotspot = monitor.matches_designated(active)

    try:
        verdict = evaluate(db, config_mgr)
        verdict_data = {
            "daily_used": verdict.daily_used,
            "daily_limit": verdict.daily_limit,
            "daily_effective": verdict.daily_effective,
            "daily_pct": round(verdict.daily_pct, 1),
            "monthly_used": verdict.monthly_used,
            "monthly_limit": verdict.monthly_limit,
            "monthly_pct": round(verdict.monthly_pct, 1),
            "level": verdict.level,
            "reason": verdict.reason,
            "enforced": verdict.enforce,
        }
    except Exception as exc:  # noqa: BLE001
        verdict_data = {"error": str(exc)}

    today = db.get_today_usage()
    return {
        "daemon_online": bool(ipc.get("status") == "ok"),
        "designated_hotspot_uuid": config_mgr.designated_hotspot_uuid,
        "designated_hotspot_name": config_mgr.designated_hotspot_name,
        "connection_name": ipc.get("connection_name") or (active.name if active else "Disconnected"),
        "connection_uuid": ipc.get("connection_uuid") or (active.uuid if active else ""),
        "interface": ipc.get("connection_interface") or (active.interface if active else ""),
        "is_hotspot_active": ipc.get("is_hotspot_active", is_hotspot),
        "active_profile": ipc.get("current_profile") or config_mgr.active_profile,
        "quota_blocked": ipc.get("is_quota_blocked", verdict_data.get("reason") is not None),
        "firewall_error": ipc.get("firewall_error"),
        "apt_window_remaining": ipc.get("apt_window_remaining", 0),
        "active_app": ipc.get("active_app", "unknown"),
        "today_rx": today.get("rx_bytes", 0),
        "today_tx": today.get("tx_bytes", 0),
        "today_total": today.get("total_bytes", 0),
        "quota": verdict_data,
        "warnings_fired": today.get("warnings_fired", []),
    }


def cmd_status(as_json: bool = False) -> int:
    data = gather_status()
    if as_json:
        print(json.dumps(data, indent=2))
        return 0

    quota = data["quota"]
    limit = quota.get("daily_effective", 0)
    print("=" * 52)
    print("            Linux Data Guardian Status")
    print("=" * 52)
    print(f"Daemon                  : {'online' if data['daemon_online'] else 'OFFLINE'}")
    print(f"Designated Hotspot      : {data['designated_hotspot_name'] or 'not configured'}")
    print(f"Active Connection       : {data['connection_name']} ({data['interface'] or 'n/a'})")
    print(f"Metered Hotspot Active  : {'YES' if data['is_hotspot_active'] else 'no'}")
    print(f"Active Profile          : {str(data['active_profile']).upper()}")
    print(f"Focused App (X11)       : {data['active_app']}")
    print("-" * 52)
    print(f"Today RX / TX           : {format_bytes(data['today_rx'])} / {format_bytes(data['today_tx'])}")
    print(f"Today Total (all nets)  : {format_bytes(data['today_total'])}")
    print(f"Metered Today           : {format_bytes(quota.get('daily_used', 0))}")
    print(f"Daily Budget            : {format_bytes(limit) if limit else 'unlimited'}"
          f"  ({quota.get('daily_pct', 0)}%)")
    print(f"Monthly                : {format_bytes(quota.get('monthly_used', 0))}"
          f" / {format_bytes(quota.get('monthly_limit', 0)) if quota.get('monthly_limit') else 'unlimited'}"
          f"  ({quota.get('monthly_pct', 0)}%)")
    print(f"Quota State             : {str(quota.get('level', 'ok')).upper()}"
          + (f" ({quota.get('reason')})" if quota.get("reason") else ""))
    print(f"Traffic Paused          : {'YES' if data['quota_blocked'] else 'no'}")
    if data["apt_window_remaining"]:
        print(f"APT Window              : {data['apt_window_remaining']}s remaining")
    if data["firewall_error"]:
        print(f"FIREWALL ERROR          : {data['firewall_error']}")
    print("=" * 52)
    return 0


# ------------------------------------------------------------------- subcommands

def cmd_set_hotspot(target: str | None) -> int:
    config_mgr = ConfigManager()
    saved = [c for c in NetworkMonitor.list_saved_connections()
             if c["type"] == "802-11-wireless"]
    if not saved:
        print("[!] No saved Wi-Fi connections found in NetworkManager.")
        return 1

    selected = None
    if target:
        wanted = target.lower()
        for conn in saved:
            if conn["uuid"] == target or conn["name"].lower() == wanted:
                selected = conn
                break
        if not selected:
            for conn in saved:
                if wanted in conn["name"].lower():
                    selected = conn
                    break
        if not selected:
            print(f"[!] No saved Wi-Fi connection matching '{target}'.")
            return 1
    else:
        print("\n=== Select designated mobile hotspot ===")
        for idx, conn in enumerate(saved, 1):
            mark = " (current)" if conn["uuid"] == config_mgr.designated_hotspot_uuid else ""
            print(f"  [{idx}] {conn['name']} ({conn['uuid'][:8]}…){mark}")
        try:
            choice = input(f"Number 1-{len(saved)} or q to cancel: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if choice.lower() in ("q", "quit", ""):
            print("Cancelled.")
            return 0
        try:
            selected = saved[int(choice) - 1]
        except (ValueError, IndexError):
            print("[!] Invalid selection.")
            return 1

    config_mgr.set_hotspot(selected["uuid"], selected["name"])
    send_ipc_command({"action": "set_hotspot_uuid",
                      "uuid": selected["uuid"], "name": selected["name"]})
    print(f"\n[+] Designated hotspot: {selected['name']} ({selected['uuid']})")
    return 0


def cmd_bypass() -> int:
    """Emergency bypass. Never unlinks the daemon's socket."""
    resp = send_ipc_command({"action": "emergency_bypass"})
    if resp and resp.get("status") == "ok":
        print("[+] Emergency bypass applied; all restrictions removed.")
        return 0

    print("[i] Daemon offline; flushing the firewall table directly.")
    from src.data_guardian.firewall.nft_manager import NftablesManager

    ok, msg = NftablesManager().flush()
    if ok:
        print(f"[+] {msg}")
        return 0
    print(f"[-] Could not flush rules: {msg}")
    print("    Run manually: sudo /usr/local/libexec/data-guardian-helper --flush")
    return 1


# ------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-guardian",
        description="Linux Data Guardian — mobile data saver and accounting",
    )
    parser.add_argument("--version", action="version", version=f"data-guardian {VERSION}")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="Show connection, profile, quota and usage")
    p_status.add_argument("--json", action="store_true", help="Machine-readable output")

    sub.add_parser("daemon", help="Run the background daemon")
    sub.add_parser("tray", help="Launch the system tray applet")
    sub.add_parser("dashboard", help="Launch the GTK3 dashboard")

    p_hs = sub.add_parser("set-hotspot", help="Select the designated metered hotspot")
    p_hs.add_argument("target", nargs="?", default=None)

    p_prof = sub.add_parser("profile", help="Set the global default profile")
    p_prof.add_argument("mode", choices=["balanced", "strict", "off"])

    p_allow = sub.add_parser("allow", help="Grant temporary data allowance")
    p_allow.add_argument("mb", type=int)

    sub.add_parser("allow-apt", help="Open the timed APT download window")
    sub.add_parser("close-apt", help="Close the APT download window")
    sub.add_parser("bypass", help="Emergency bypass (removes all restrictions)")

    p_doctor = sub.add_parser("doctor", help="Run health diagnostics")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.add_argument("--no-color", action="store_true")

    sub.add_parser("install-shims", help="Install pip/pip3/npm/cargo shims")
    sub.add_parser("remove-shims", help="Remove the shims")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "status"

    if command == "status":
        return cmd_status(as_json=getattr(args, "json", False))

    if command == "set-hotspot":
        return cmd_set_hotspot(args.target)

    if command == "daemon":
        from src.data_guardian.daemon import run_daemon

        run_daemon()
        return 0

    if command == "tray":
        from src.data_guardian.ui.tray import run_tray

        run_tray()
        return 0

    if command == "dashboard":
        from src.data_guardian.ui.dashboard import show_dashboard

        show_dashboard()
        return 0

    if command == "profile":
        resp = send_ipc_command({"action": "set_profile", "profile": args.mode})
        if resp and resp.get("status") == "ok":
            print(f"[+] Profile set to '{args.mode}'.")
            return 0
        try:
            ConfigManager().active_profile = args.mode
        except ValueError as exc:
            print(f"[-] {exc}")
            return 1
        print(f"[+] Profile '{args.mode}' saved (daemon offline; applies on next start).")
        return 0

    if command == "allow":
        resp = send_ipc_command({"action": "grant_allowance", "mb": args.mb})
        if resp and resp.get("status") == "ok":
            print(f"[+] Granted +{args.mb} MB.")
            return 0
        if resp and resp.get("status") == "error":
            print(f"[-] {resp.get('message')}")
            return 1
        try:
            Database(ConfigManager().db_path).grant_temporary_allowance(args.mb * MB)
        except ValueError as exc:
            print(f"[-] {exc}")
            return 1
        print(f"[+] Granted +{args.mb} MB (stored; daemon offline).")
        return 0

    if command == "allow-apt":
        resp = send_ipc_command({"action": "allow_apt_window"})
        if resp and resp.get("status") == "ok":
            print(f"[+] APT window open for {resp.get('minutes', 15)} minute(s).")
            return 0
        print(f"[-] Could not open APT window: {(resp or {}).get('message', 'daemon offline')}")
        return 1

    if command == "close-apt":
        send_ipc_command({"action": "close_apt_window"})
        print("[+] APT window requested to close.")
        return 0

    if command == "bypass":
        return cmd_bypass()

    if command == "doctor":
        from src.data_guardian.diagnostics import run_doctor

        return run_doctor(as_json=getattr(args, "json", False),
                          color=not getattr(args, "no_color", False))

    if command == "install-shims":
        from src.data_guardian.approvals.shims import install_shims

        paths = install_shims()
        print(f"[+] Installed shims: {', '.join(p.name for p in paths)}")
        return 0

    if command == "remove-shims":
        from src.data_guardian.approvals.shims import remove_shims

        removed = remove_shims()
        print(f"[+] Removed {len(removed)} shim(s).")
        return 0

    print(f"[!] Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
