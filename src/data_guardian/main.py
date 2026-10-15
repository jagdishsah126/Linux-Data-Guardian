"""Main CLI and GUI entry point for Linux Data Guardian."""

from __future__ import annotations
import sys
import argparse
from pathlib import Path

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.network.byte_counter import format_bytes
from src.data_guardian.network.nm_monitor import NetworkMonitor
from src.data_guardian.ipc import send_ipc_command
from src.data_guardian.approvals.shims import install_shims, remove_shims


def cmd_status() -> None:
    """Prints current status to terminal."""
    config_mgr = ConfigManager()
    db = Database(config_mgr.db_path)
    monitor = NetworkMonitor(
        designated_uuid=config_mgr.designated_hotspot_uuid,
        designated_name=config_mgr.config.get("hotspot", {}).get("designated_name", "")
    )

    active = monitor.get_active_connection()
    is_hotspot = monitor.is_designated_hotspot_active()
    today = db.get_today_usage()
    effective_limit = today.get('effective_limit_bytes', 0)
    if effective_limit == 0:
        effective_limit = config_mgr.daily_limit_mb * 1024 * 1024

    print("==================================================")
    print("           Linux Data Guardian Status             ")
    print("==================================================")
    print(f"Designated Hotspot UUID : {config_mgr.designated_hotspot_uuid or 'Not Configured'}")
    print(f"Active Connection       : {active.name if active else 'None'} ({active.uuid if active else 'N/A'})")
    print(f"Metered Status          : {'YES (Hotspot Active)' if is_hotspot else 'NO (Unmetered)'}")
    print(f"Active Profile          : {config_mgr.active_profile.upper()}")
    print("--------------------------------------------------")
    print(f"Today's Download (RX)   : {format_bytes(today.get('rx_bytes', 0))}")
    print(f"Today's Upload (TX)     : {format_bytes(today.get('tx_bytes', 0))}")
    print(f"Today's Total Usage     : {format_bytes(today.get('total_bytes', 0))}")
    print(f"Daily Limit             : {format_bytes(effective_limit)}")
    print(f"Quota Exceeded Block    : {'YES (TRAFFIC PAUSED)' if today.get('quota_exceeded') else 'NO'}")
    print("==================================================")


def cmd_set_hotspot(target: str | None = None) -> None:
    """Interactively or directly sets the designated metered Wi-Fi hotspot."""
    config_mgr = ConfigManager()
    saved = [c for c in NetworkMonitor.list_saved_connections() if c["type"] == "802-11-wireless"]

    if not saved:
        print("[!] No saved Wi-Fi connections found in NetworkManager.")
        return

    selected = None
    if target:
        target_lower = target.lower()
        for c in saved:
            if c["uuid"] == target or c["name"].lower() == target_lower:
                selected = c
                break
        if not selected:
            # Partial match
            for c in saved:
                if target_lower in c["name"].lower():
                    selected = c
                    break

        if not selected:
            print(f"[!] No saved Wi-Fi connection matching '{target}' was found.")
            return
    else:
        print("\n=== Select Designated Mobile Hotspot ===")
        current_uuid = config_mgr.designated_hotspot_uuid
        for idx, c in enumerate(saved, 1):
            mark = " (CURRENT)" if c["uuid"] == current_uuid else ""
            print(f"  [{idx}] {c['name']} (UUID: {c['uuid'][:8]}...){mark}")

        print("")
        try:
            choice = input(f"Enter number (1-{len(saved)}) or 'q' to cancel: ").strip()
            if choice.lower() in ("q", "quit", ""):
                print("Cancelled.")
                return
            idx = int(choice) - 1
            if 0 <= idx < len(saved):
                selected = saved[idx]
            else:
                print("[!] Invalid selection.")
                return
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return

    # Update config
    config_mgr.designated_hotspot_uuid = selected["uuid"]
    if "hotspot" not in config_mgr.config:
        config_mgr.config["hotspot"] = {}
    config_mgr.config["hotspot"]["designated_name"] = selected["name"]
    config_mgr.save_user_config()

    # Inform running daemon if active
    send_ipc_command({"action": "set_hotspot_uuid", "uuid": selected["uuid"]})

    print(f"\n[✓] Successfully set designated hotspot to: '{selected['name']}' ({selected['uuid']})")
    print("    Linux Data Guardian will automatically protect data whenever connected to this Wi-Fi.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="data-guardian",
        description="Linux Data Guardian — Mobile Data Saver and Accounting"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("status", help="Show current connection, profile, and usage")
    subparsers.add_parser("daemon", help="Run background daemon service")
    subparsers.add_parser("tray", help="Launch XFCE system tray applet")
    subparsers.add_parser("dashboard", help="Launch GTK3 status dashboard")

    # Set hotspot command
    p_hs = subparsers.add_parser("set-hotspot", help="Select or set the designated mobile Wi-Fi hotspot")
    p_hs.add_argument("target", nargs="?", default=None, help="Wi-Fi name or UUID to designate (interactive if omitted)")

    # Profile command
    p_prof = subparsers.add_parser("profile", help="Set active Data Saver profile")
    p_prof.add_argument("mode", choices=["balanced", "strict", "off"], help="Profile mode")

    # Allowance command
    p_allow = subparsers.add_parser("allow", help="Grant temporary data allowance")
    p_allow.add_argument("mb", type=int, help="Megabytes to add")

    subparsers.add_parser("bypass", help="Emergency disable all restrictions")
    subparsers.add_parser("install-shims", help="Install CLI shims for pip, npm, cargo in ~/.local/bin")
    subparsers.add_parser("remove-shims", help="Remove CLI shims")

    args = parser.parse_args()

    if not args.command or args.command == "status":
        cmd_status()
    elif args.command == "set-hotspot":
        cmd_set_hotspot(args.target)
    elif args.command == "daemon":
        from src.data_guardian.daemon import run_daemon
        run_daemon()
    elif args.command == "tray":
        from src.data_guardian.ui.tray import run_tray
        run_tray()
    elif args.command == "dashboard":
        from src.data_guardian.ui.dashboard import show_dashboard
        show_dashboard()
    elif args.command == "profile":
        resp = send_ipc_command({"action": "set_profile", "profile": args.mode})
        if resp and resp.get("status") == "ok":
            print(f"[+] Profile updated to '{args.mode}'.")
        else:
            # Fallback to local config update
            cfg = ConfigManager()
            cfg.active_profile = args.mode
            print(f"[+] Profile updated to '{args.mode}' in configuration.")
    elif args.command == "allow":
        resp = send_ipc_command({"action": "grant_allowance", "mb": args.mb})
        if resp and resp.get("status") == "ok":
            print(f"[+] Granted +{args.mb} MB temporary allowance.")
        else:
            db = Database(ConfigManager().db_path)
            db.grant_temporary_allowance(args.mb * 1024 * 1024)
            print(f"[+] Granted +{args.mb} MB in database.")
    elif args.command == "bypass":
        send_ipc_command({"action": "emergency_bypass"})
        # Also run emergency recovery script
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "emergency-disable.sh"
        if script.exists():
            import subprocess
            subprocess.run([str(script)])
    elif args.command == "install-shims":
        paths = install_shims()
        print(f"[+] Installed shims in ~/.local/bin: {[p.name for p in paths]}")
    elif args.command == "remove-shims":
        remove_shims()
        print("[+] Removed shims from ~/.local/bin.")


if __name__ == "__main__":
    main()
