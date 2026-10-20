"""GTK3 Dashboard for Linux Data Guardian — with per-network stats, Statistics, and CLI tabs."""

from __future__ import annotations
import subprocess
import threading
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango

from src.data_guardian.ipc import send_ipc_command
from src.data_guardian.ui.async_ipc import ipc_async
from src.data_guardian.database import Database
from src.data_guardian.config import ConfigManager
from src.data_guardian.network.byte_counter import format_bytes
from src.data_guardian.network.nm_monitor import NetworkMonitor

# ──────────────────────────────────────────────────────────────────────────────
# Matplotlib is imported lazily, only when the Statistics tab is built, so the
# dashboard starts fast and does not pay ~1s / ~40 MB for a tab you may not open.
# ──────────────────────────────────────────────────────────────────────────────
_MPL_AVAILABLE = False
_MPL_LOADED = False
Figure = None            # type: ignore[assignment]
FigureCanvas = None      # type: ignore[assignment]
mdates = None            # type: ignore[assignment]


def load_matplotlib() -> bool:
    """Import matplotlib on first use. Returns True when the chart can be drawn."""
    global _MPL_AVAILABLE, _MPL_LOADED, Figure, FigureCanvas, mdates
    if _MPL_LOADED:
        return _MPL_AVAILABLE
    _MPL_LOADED = True
    try:
        import matplotlib

        matplotlib.use("GTK3Agg")
        from matplotlib import dates as _mdates
        from matplotlib.backends.backend_gtk3agg import FigureCanvasGTK3Agg as _FigureCanvas
        from matplotlib.figure import Figure as _Figure

        Figure = _Figure
        FigureCanvas = _FigureCanvas
        mdates = _mdates
        _MPL_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        print(f"[i] matplotlib unavailable, Statistics chart disabled: {exc}")
        _MPL_AVAILABLE = False
    return _MPL_AVAILABLE


# ──────────────────────────────────────────────────────────────────────────────
# CLI command definitions shown in CLI tab
# ──────────────────────────────────────────────────────────────────────────────
CLI_COMMANDS = [
    {
        "group": "📊 Status & Monitoring",
        "commands": [
            {
                "label": "Show Status",
                "cmd": "data-guardian status",
                "desc": "Shows current network, hotspot detection, profile, daily usage, and quota status.",
            },
            {
                "label": "Daemon Service Status",
                "cmd": "systemctl status data-guardian.service --no-pager",
                "desc": "Check if the background daemon is running via systemd (auto-starts at boot).",
            },
            {
                "label": "View Daemon Logs",
                "cmd": "journalctl -u data-guardian.service -n 50 --no-pager",
                "desc": "Last 50 log lines from the systemd daemon (errors, connection events, etc.).",
            },
            {
                "label": "List Known Networks",
                "cmd": "nmcli connection show --active",
                "desc": "Show currently active network connections via NetworkManager.",
            },
            {
                "label": "List Saved Wi-Fi",
                "cmd": "nmcli connection show | grep wifi",
                "desc": "All saved Wi-Fi networks stored in NetworkManager.",
            },
        ],
    },
    {
        "group": "🛡️ Profile & Hotspot Control",
        "commands": [
            {
                "label": "Set Profile: Balanced",
                "cmd": "data-guardian profile balanced",
                "desc": "BALANCED: Browsing works normally; new APT (UID 42) connections are "
                        "dropped while metered. Snap/Flatpak run as your user and are not blocked.",
            },
            {
                "label": "Set Profile: Strict",
                "cmd": "data-guardian profile strict",
                "desc": "STRICT: Only the focused X11 app and the allowlisted tools "
                        "(opencode, antigravity) may open new connections. Without cgroup v2 "
                        "it falls back to ports 22/80/443.",
            },
            {
                "label": "Set Profile: Off",
                "cmd": "data-guardian profile off",
                "desc": "OFF: Monitoring only — no firewall rules applied, all apps have full access. "
                        "Usage is still tracked and logged for your statistics.",
            },
            {
                "label": "Set Hotspot (Interactive)",
                "cmd": "data-guardian set-hotspot",
                "desc": "Interactively choose which saved Wi-Fi network is your metered mobile hotspot.",
            },
            {
                "label": "Set Hotspot: Moto Mobile",
                "cmd": "data-guardian set-hotspot 'Moto Mobile'",
                "desc": "Directly set 'Moto Mobile' as the designated metered hotspot.",
            },
        ],
    },
    {
        "group": "📦 Download Approvals",
        "commands": [
            {
                "label": "Grant +100 MB Allowance",
                "cmd": "data-guardian allow 100",
                "desc": "Temporarily grant 100 MB extra on top of today's daily quota limit.",
            },
            {
                "label": "Grant +500 MB Allowance",
                "cmd": "data-guardian allow 500",
                "desc": "Temporarily grant 500 MB extra on top of today's daily quota limit.",
            },
            {
                "label": "Allow APT Updates (15 min)",
                "cmd": "data-guardian allow-apt",
                "desc": "Opens a temporary 15-minute window for APT package updates (sudo apt update / install) on mobile data without disabling other protections.",
            },
            {
                "label": "Emergency Bypass (All Off)",
                "cmd": "data-guardian bypass",
                "desc": "EMERGENCY: Immediately disables all firewall rules and removes nft data_guardian table. "
                        "Use if you are accidentally locked out of a network.",
            },
        ],
    },
    {
        "group": "🔧 Installation & Maintenance",
        "commands": [
            {
                "label": "Check Privileged Helper",
                "cmd": "sudo -n /usr/local/libexec/data-guardian-helper --status 2>&1 || echo 'Helper inactive or not yet installed with sudo'",
                "desc": "Tests if the root-owned privileged helper is installed and accessible via passwordless sudo.",
            },
            {
                "label": "Install Shims (pip/npm/cargo)",
                "cmd": "data-guardian install-shims",
                "desc": "Installs wrapper shims in ~/.local/bin for pip, pip3, npm, cargo. "
                        "These intercept package installations and ask for approval when on hotspot.",
            },
            {
                "label": "Remove Shims",
                "cmd": "data-guardian remove-shims",
                "desc": "Removes the pip/npm/cargo shims from ~/.local/bin (restores normal behavior).",
            },
            {
                "label": "Restart Daemon",
                "cmd": "systemctl restart data-guardian.service",
                "desc": "Restart the background daemon (use if status shows errors or it stopped).",
            },
            {
                "label": "Restart Tray",
                "cmd": "pkill -f 'data-guardian tray' 2>/dev/null; sleep 1; data-guardian tray &",
                "desc": "Kill and restart the system tray icon applet. Use if the tray disappears from the panel.",
            },
            {
                "label": "View Config File",
                "cmd": "cat ~/.config/linux-data-guardian/config.yaml",
                "desc": "Show current configuration including hotspot UUID, quotas, and per-network profiles.",
            },
            {
                "label": "Check nftables Rules",
                "cmd": "sudo nft list table inet data_guardian 2>/dev/null || echo 'No data_guardian nft table active (firewall rules not applied or Off mode)'",
                "desc": "Show active firewall rules in the isolated data_guardian nftables table.",
            },
            {
                "label": "Emergency Disable Script",
                "cmd": "bash \"$(cat \"${XDG_CONFIG_HOME:-$HOME/.config}/linux-data-guardian/project_dir\")/scripts/emergency-disable.sh\"",
                "desc": "Directly run the emergency disable script to flush all nft data_guardian rules immediately.",
            },
        ],
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Main Dashboard Window
# ──────────────────────────────────────────────────────────────────────────────

class DashboardWindow(Gtk.Window):
    """Linux Data Guardian — GTK3 Dashboard with Overview, Statistics, and CLI tabs."""

    def __init__(self, config_manager: Optional[ConfigManager] = None) -> None:
        super().__init__(title="Linux Data Guardian")
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(self.config_mgr.db_path)

        # Currently selected network UUID in the network dropdown
        self._selected_uuid: str = ""
        # Known saved wireless connections: list of {uuid, name, type}
        self._saved_connections: List[Dict[str, Any]] = []
        # Track whether profile radio change is from code (suppress IPC ping)
        self._loading_profile = False
        # Last daemon snapshot, refreshed once per timer tick
        self._status: Dict[str, Any] = {}
        self._refresh_inflight = False

        self.set_default_size(640, 620)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)
        self.set_icon_name("linux-data-guardian")

        # ── Notebook (tabs) ────────────────────────────────────────────────
        notebook = Gtk.Notebook()
        notebook.set_border_width(0)
        self.add(notebook)

        # Tab 1: Overview
        overview_tab = self._build_overview_tab()
        tab1_label = Gtk.Label(label="📡  Overview")
        notebook.append_page(overview_tab, tab1_label)

        # Tab 2: Statistics
        stats_tab = self._build_statistics_tab()
        tab2_label = Gtk.Label(label="📈  Statistics")
        notebook.append_page(stats_tab, tab2_label)

        # Tab 3: CLI Commands
        cli_tab = self._build_cli_tab()
        tab3_label = Gtk.Label(label="⌨️  CLI")
        notebook.append_page(cli_tab, tab3_label)

        # Start periodic refresh
        GLib.timeout_add_seconds(3, self._refresh_overview)
        self._refresh_overview()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1: OVERVIEW
    # ══════════════════════════════════════════════════════════════════════════

    def _build_overview_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_border_width(12)

        # ── Status Banner ──────────────────────────────────────────────────
        self.status_banner = Gtk.Label()
        self.status_banner.set_markup(
            "<b><big>Linux Data Guardian</big></b>\n"
            "<span foreground='gray'>Connecting to daemon…</span>"
        )
        self.status_banner.set_justify(Gtk.Justification.CENTER)
        outer.pack_start(self.status_banner, False, False, 6)

        # ── Live connection info ───────────────────────────────────────────
        self.live_conn_label = Gtk.Label(label="Active: detecting…", xalign=0)
        self.live_conn_label.set_ellipsize(Pango.EllipsizeMode.END)
        outer.pack_start(self.live_conn_label, False, False, 2)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)

        # ── Network Selector Frame ─────────────────────────────────────────
        net_frame = Gtk.Frame()
        net_frame.set_label("  Network — Select to view stats & set profile  ")
        net_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        net_box.set_border_width(10)
        net_frame.add(net_box)

        # Dropdown row
        dd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dd_label = Gtk.Label(label="Network:")
        dd_row.pack_start(dd_label, False, False, 0)

        self.combo_networks = Gtk.ComboBoxText()
        dd_row.pack_start(self.combo_networks, True, True, 0)

        # Hotspot star button
        self.btn_set_hotspot = Gtk.Button(label="⭐ Set as Hotspot")
        self.btn_set_hotspot.set_tooltip_text("Mark selected network as your metered mobile hotspot")
        self.btn_set_hotspot.connect("clicked", self._on_set_as_hotspot)
        dd_row.pack_start(self.btn_set_hotspot, False, False, 0)
        net_box.pack_start(dd_row, False, False, 0)

        # Hotspot indicator
        self.hotspot_indicator = Gtk.Label(label="", xalign=0)
        net_box.pack_start(self.hotspot_indicator, False, False, 0)

        outer.pack_start(net_frame, False, False, 0)

        # ── Usage Frame (per-selected-network) ────────────────────────────
        usage_frame = Gtk.Frame()
        usage_frame.set_label("  Today's Usage — Selected Network  ")
        usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        usage_box.set_border_width(10)
        usage_frame.add(usage_box)

        rx_tx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.rx_label = Gtk.Label(label="↓ Download: 0 B", xalign=0)
        self.tx_label = Gtk.Label(label="↑ Upload: 0 B", xalign=0)
        rx_tx_row.pack_start(self.rx_label, True, True, 0)
        rx_tx_row.pack_start(self.tx_label, True, True, 0)
        usage_box.pack_start(rx_tx_row, False, False, 0)

        self.usage_label = Gtk.Label(label="Total: 0 B  /  Daily Limit: 1000 MB", xalign=0)
        usage_box.pack_start(self.usage_label, False, False, 0)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_fraction(0.0)
        usage_box.pack_start(self.progress_bar, False, False, 0)

        # Allowance buttons (apply globally to quota)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_100 = Gtk.Button(label="+100 MB")
        btn_100.connect("clicked", lambda b: self._grant_allowance(100))
        btn_500 = Gtk.Button(label="+500 MB")
        btn_500.connect("clicked", lambda b: self._grant_allowance(500))
        self.btn_apt = Gtk.Button(label="📦 Allow APT (15m)")
        self.btn_apt.set_tooltip_text("Open a timed window for apt update / apt install on mobile data")
        self.btn_apt.connect("clicked", lambda b: self._allow_apt_window())
        btn_bypass = Gtk.Button(label="🚨 Bypass")
        btn_bypass.connect("clicked", self._emergency_bypass)
        btn_box.pack_start(btn_100, True, True, 0)
        btn_box.pack_start(btn_500, True, True, 0)
        btn_box.pack_start(self.btn_apt, True, True, 0)
        btn_box.pack_start(btn_bypass, True, True, 0)
        usage_box.pack_start(btn_box, False, False, 4)

        outer.pack_start(usage_frame, False, False, 0)

        # ── Profile Frame (per-network) ────────────────────────────────────
        profile_frame = Gtk.Frame()
        profile_frame.set_label("  Data Saver Profile — This Network Only  ")
        profile_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        profile_outer.set_border_width(10)
        profile_frame.add(profile_outer)

        profile_radio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.radio_balanced = Gtk.RadioButton.new_with_label_from_widget(None, "⚖️  Balanced")
        self.radio_strict = Gtk.RadioButton.new_with_label_from_widget(self.radio_balanced, "🔒  Strict (Exp.)")
        self.radio_off = Gtk.RadioButton.new_with_label_from_widget(self.radio_balanced, "👁  Off (Monitor Only)")
        self.radio_balanced.connect("toggled", lambda b: self._on_profile_toggled("balanced", b.get_active()))
        self.radio_strict.connect("toggled", lambda b: self._on_profile_toggled("strict", b.get_active()))
        self.radio_off.connect("toggled", lambda b: self._on_profile_toggled("off", b.get_active()))
        profile_radio_row.pack_start(self.radio_balanced, True, True, 0)
        profile_radio_row.pack_start(self.radio_strict, True, True, 0)
        profile_radio_row.pack_start(self.radio_off, True, True, 0)
        profile_outer.pack_start(profile_radio_row, False, False, 0)

        # Profile description label
        self.profile_desc_label = Gtk.Label(xalign=0)
        self.profile_desc_label.set_line_wrap(True)
        self.profile_desc_label.set_markup(self._profile_description("balanced"))
        profile_outer.pack_start(self.profile_desc_label, False, False, 0)

        outer.pack_start(profile_frame, False, False, 0)

        # ── Recent Approvals Log ───────────────────────────────────────────
        log_frame = Gtk.Frame()
        log_frame.set_label("  Recent Download Approvals  ")
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_border_width(8)
        log_frame.add(log_box)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(100)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.log_list = Gtk.ListBox()
        scrolled.add(self.log_list)
        log_box.pack_start(scrolled, True, True, 0)
        outer.pack_start(log_frame, False, False, 0)

        # Now that all UI frames (usage, profile, network) are instantiated,
        # connect signals and populate the combo box with active network stats:
        self.combo_networks.connect("changed", self._on_network_selected)
        self._populate_network_combo()

        return outer

    def _profile_description(self, profile: str) -> str:
        if profile == "balanced":
            return (
                "<small><span foreground='#b8860b'>⚖️  <b>Balanced</b>: Browsing works normally. "
                "New APT (UID 42) connections are dropped while metered; other apps are not filtered. "
                "Best for everyday use on hotspot.</span></small>"
            )
        elif profile == "strict":
            return (
                "<small><span foreground='#1565c0'>🔒  <b>Strict</b>: Only the focused X11 app and "
                "the allowlisted tools (opencode, antigravity) may open new connections. Without "
                "cgroup v2, a TCP port allowlist (22/80/443) is used instead.</span></small>"
            )
        else:
            return (
                "<small><span foreground='gray'>👁  <b>Off (Monitor Only)</b>: No firewall rules active. "
                "All apps have unrestricted access. "
                "Usage is still tracked in Statistics.</span></small>"
            )

    def _populate_network_combo(self, preferred_uuid: str = "") -> None:
        """Fill the network dropdown with all saved wireless connections."""
        self.combo_networks.remove_all()
        self._saved_connections = [
            c for c in NetworkMonitor.list_saved_connections()
            if c["type"] == "802-11-wireless"
        ]
        # Also add any networks we've tracked data for but aren't in saved list
        known = self.db.get_known_networks()
        saved_uuids = {c["uuid"] for c in self._saved_connections}
        for n in known:
            if n["connection_uuid"] not in saved_uuids:
                self._saved_connections.append({
                    "uuid": n["connection_uuid"],
                    "name": n["connection_name"],
                    "type": "802-11-wireless",
                })

        desig_uuid = self.config_mgr.designated_hotspot_uuid

        # Target selection: 1. preferred_uuid -> 2. currently active network -> 3. designated hotspot -> 4. first network
        target_uuid = preferred_uuid or self._selected_uuid
        if not target_uuid:
            status = self._status or {}
            if status.get("connection_uuid"):
                target_uuid = status["connection_uuid"]
            elif desig_uuid:
                target_uuid = desig_uuid
            elif self._saved_connections:
                target_uuid = self._saved_connections[0]["uuid"]

        selected_idx = 0
        for idx, conn in enumerate(self._saved_connections):
            star = " ⭐" if conn["uuid"] == desig_uuid else ""
            self.combo_networks.append(conn["uuid"], f"{conn['name']}{star}")
            if target_uuid and conn["uuid"] == target_uuid:
                selected_idx = idx

        if self._saved_connections:
            chosen_conn = self._saved_connections[selected_idx]
            self._selected_uuid = chosen_conn["uuid"]
            self.combo_networks.set_active(selected_idx)
            self._refresh_network_panel(self._selected_uuid)

    def _on_network_selected(self, combo: Gtk.ComboBoxText) -> None:
        """When user selects a network in dropdown — update stats and profile UI."""
        uuid = combo.get_active_id()
        if not uuid:
            return
        self._selected_uuid = uuid
        self._refresh_network_panel(uuid)

    def _on_set_as_hotspot(self, _btn: Gtk.Widget) -> None:
        """Set the currently selected network as the designated hotspot."""
        if not self._selected_uuid:
            return
        name = next(
            (c["name"] for c in self._saved_connections if c["uuid"] == self._selected_uuid),
            self._selected_uuid[:8],
        )
        self.config_mgr.set_hotspot(self._selected_uuid, name)
        ipc_async(
            {"action": "set_hotspot_uuid", "uuid": self._selected_uuid, "name": name},
            lambda _r: self._populate_network_combo(preferred_uuid=self._selected_uuid),
        )

    def _refresh_network_panel(self, uuid: str) -> None:
        """Refresh the selected network's stats and profile widgets (never writes config)."""
        usage = self.db.get_network_today_usage(uuid)
        rx = usage.get("rx_bytes", 0)
        tx = usage.get("tx_bytes", 0)
        total = usage.get("total_bytes", 0)
        limit_bytes = self.config_mgr.daily_limit_mb * 1024 * 1024
        is_metered = bool(usage.get("metered"))

        self.rx_label.set_text(f"↓ Download: {format_bytes(rx)}")
        self.tx_label.set_text(f"↑ Upload:   {format_bytes(tx)}")

        if is_metered and limit_bytes > 0:
            self.usage_label.set_text(
                f"Total: {format_bytes(total)}  /  Daily Limit: {format_bytes(limit_bytes)}"
            )
            frac = min(total / limit_bytes, 1.0)
            self.progress_bar.set_text(f"{int(frac * 100)}%")
        else:
            self.usage_label.set_text(f"Total: {format_bytes(total)}  /  unmetered — no quota")
            frac = 0.0
            self.progress_bar.set_text("n/a")
        self.progress_bar.set_fraction(frac)

        desig = self.config_mgr.designated_hotspot_uuid
        if uuid == desig:
            self.hotspot_indicator.set_markup(
                "<span foreground='orange'><b>⭐ Designated metered hotspot — "
                "data saving rules apply here.</b></span>"
            )
        else:
            self.hotspot_indicator.set_markup(
                "<span foreground='green'>Regular (unmetered) network — "
                "no restrictions are applied here.</span>"
            )

        # Read the profile from config; the daemon snapshot is the authority for
        # the *active* network only, and we never persist from a refresh.
        status = self._status or {}
        if uuid and uuid == status.get("connection_uuid"):
            saved_profile = status.get("current_profile", self.config_mgr.get_network_profile(uuid))
        else:
            saved_profile = self.config_mgr.get_network_profile(uuid)

        self._loading_profile = True
        try:
            if saved_profile == "strict":
                self.radio_strict.set_active(True)
            elif saved_profile == "off":
                self.radio_off.set_active(True)
            else:
                self.radio_balanced.set_active(True)
            self.profile_desc_label.set_markup(self._profile_description(saved_profile))
        finally:
            self._loading_profile = False

    def _on_profile_toggled(self, profile_name: str, is_active: bool) -> None:
        if not is_active or self._loading_profile:
            return
        if not self._selected_uuid:
            return
        # Per-network: never change the global default or another network's profile.
        self.config_mgr.set_network_profile(self._selected_uuid, profile_name)
        self.profile_desc_label.set_markup(self._profile_description(profile_name))
        ipc_async({"action": "set_network_profile",
                   "uuid": self._selected_uuid,
                   "profile": profile_name},
                  lambda _r: GLib.timeout_add(400, self._refresh_overview))

    def _grant_allowance(self, mb: int) -> None:
        ipc_async({"action": "grant_allowance", "mb": mb},
                  lambda _r: GLib.timeout_add(400, self._refresh_overview))

    def _allow_apt_window(self) -> None:
        ipc_async({"action": "allow_apt_window"},
                  lambda _r: GLib.timeout_add(400, self._refresh_overview))

    def _emergency_bypass(self, _widget: Gtk.Widget) -> None:
        ipc_async({"action": "emergency_bypass"},
                  lambda _r: GLib.timeout_add(400, self._refresh_overview))

    def _refresh_overview(self) -> bool:
        """Start a daemon poll in a worker thread; never block the GTK thread."""
        if self._refresh_inflight:
            return True
        self._refresh_inflight = True

        def worker() -> None:
            status = send_ipc_command({"action": "get_status"}, timeout=2.0)
            GLib.idle_add(self._apply_overview, status)

        threading.Thread(target=worker, daemon=True, name="dg-dashboard-poll").start()
        return True

    def _apply_overview(self, status: Optional[Dict[str, Any]]) -> bool:
        """Apply a poll result on the GTK main thread."""
        self._refresh_inflight = False
        self._status = status or {}

        if not status:
            self.status_banner.set_markup(
                "<b><big>Linux Data Guardian</big></b>\n"
                "<span foreground='gray'>Daemon not running</span>"
            )
            self.live_conn_label.set_text("Active: (daemon offline — showing cached data)")
        else:
            is_hotspot = status.get("is_hotspot_active", False)
            conn_name = status.get("connection_name", "Disconnected")
            profile = status.get("current_profile", "balanced")
            is_blocked = status.get("is_quota_blocked", False)
            firewall_error = status.get("firewall_error")
            iface = status.get("connection_interface", "")

            if firewall_error:
                self.status_banner.set_markup(
                    f"<b><big>Linux Data Guardian</big></b>\n"
                    f"<span foreground='red'><b>⚠ PROTECTION FAILED: {firewall_error[:80]}</b></span>"
                )
            elif is_hotspot:
                color = {"balanced": "orange", "strict": "#1565c0"}.get(profile, "gray")
                blocked_txt = "  <span foreground='red'><b>— QUOTA EXCEEDED</b></span>" if is_blocked else ""
                self.status_banner.set_markup(
                    f"<b><big>Linux Data Guardian</big></b>\n"
                    f"<span foreground='{color}'><b>🔴 Metered Hotspot: {conn_name} "
                    f"({profile.upper()} MODE){blocked_txt}</b></span>"
                )
            else:
                self.status_banner.set_markup(
                    f"<b><big>Linux Data Guardian</big></b>\n"
                    f"<span foreground='green'><b>🟢 Unrestricted: {conn_name}</b></span>"
                )

            active_app = status.get("active_app", "")
            apt_left = int(status.get("apt_window_remaining", 0) or 0)
            extra = f" | APT window {apt_left // 60 + 1} min" if apt_left else ""
            self.live_conn_label.set_text(
                f"Active: {conn_name}  ({iface})"
                + (f" | Focused: {active_app}" if active_app and active_app != "unknown" else "")
                + extra
            )

            self.btn_apt.set_label(
                f"📦 Close APT ({apt_left // 60 + 1}m)" if apt_left else "📦 Allow APT (15m)"
            )

        # Auto-select active network if _selected_uuid is somehow not initialized
        if not self._selected_uuid and self._saved_connections:
            active_uuid = status.get("connection_uuid", "") if status else ""
            target = active_uuid or self.config_mgr.designated_hotspot_uuid or self._saved_connections[0]["uuid"]
            self._selected_uuid = target
            self.combo_networks.set_active_id(target)

        # Refresh stats for currently selected network
        if self._selected_uuid:
            self._refresh_network_panel(self._selected_uuid)

        self._update_log_list()
        return False  # one-shot GLib.idle_add callback

    def _update_log_list(self) -> None:
        for child in self.log_list.get_children():
            self.log_list.remove(child)
        approvals = self.db.get_recent_approvals(limit=6)
        if not approvals:
            lbl = Gtk.Label(label="No recent package approvals recorded.", xalign=0)
            self.log_list.add(lbl)
        else:
            for ev in approvals:
                dec = ev["decision"]
                color = "green" if dec == "approved" else "red"
                time_part = ev["timestamp"].split("T")[-1][:8] if "T" in ev["timestamp"] else ev["timestamp"]
                txt = (
                    f"[{time_part}] <b>{ev['tool']}</b>: "
                    f"{str(ev.get('packages', ''))[:40]} → "
                    f"<span foreground='{color}'>{dec.upper()}</span>"
                )
                lbl = Gtk.Label(xalign=0)
                lbl.set_markup(txt)
                self.log_list.add(lbl)
        self.log_list.show_all()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2: STATISTICS
    # ══════════════════════════════════════════════════════════════════════════

    def _build_statistics_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(10)
        load_matplotlib()

        # Controls row
        ctrl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Period selector
        period_label = Gtk.Label(label="Period:")
        ctrl_row.pack_start(period_label, False, False, 0)

        self.stats_period_combo = Gtk.ComboBoxText()
        for label, val in [("1 day", 1), ("7 days", 7), ("15 days", 15),
                           ("30 days", 30), ("45 days", 45), ("60 days", 60)]:
            self.stats_period_combo.append(str(val), label)
        self.stats_period_combo.set_active_id("30")
        ctrl_row.pack_start(self.stats_period_combo, False, False, 0)

        # Metric selector (RX / TX / Total)
        metric_label = Gtk.Label(label="  Show:")
        ctrl_row.pack_start(metric_label, False, False, 0)
        self.stats_metric_combo = Gtk.ComboBoxText()
        for label, val in [("Total (RX+TX)", "total"), ("Download only (RX)", "rx"), ("Upload only (TX)", "tx")]:
            self.stats_metric_combo.append(val, label)
        self.stats_metric_combo.set_active_id("total")
        ctrl_row.pack_start(self.stats_metric_combo, False, False, 0)

        refresh_btn = Gtk.Button(label="🔄 Refresh Chart")
        refresh_btn.connect("clicked", lambda b: self._refresh_stats_chart())
        ctrl_row.pack_end(refresh_btn, False, False, 0)

        outer.pack_start(ctrl_row, False, False, 0)

        if _MPL_AVAILABLE:
            # Matplotlib canvas
            self._stats_fig = Figure(figsize=(7, 3.5), dpi=96, facecolor="#1e1e2e")
            self._stats_ax = self._stats_fig.add_subplot(111)
            self._stats_canvas = FigureCanvas(self._stats_fig)
            self._stats_canvas.set_size_request(600, 320)
            outer.pack_start(self._stats_canvas, True, True, 0)
        else:
            no_mpl = Gtk.Label(
                label="ℹ️  matplotlib is not installed.\n"
                      "Install it with:  pip install matplotlib\n"
                      "Then restart the dashboard."
            )
            outer.pack_start(no_mpl, True, True, 0)

        # Per-network summary table below chart
        self._stats_summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.pack_start(self._stats_summary_box, False, False, 0)

        self.stats_period_combo.connect("changed", lambda c: self._refresh_stats_chart())
        self.stats_metric_combo.connect("changed", lambda c: self._refresh_stats_chart())

        # Draw chart after window is shown
        GLib.idle_add(self._refresh_stats_chart)
        return outer

    def _refresh_stats_chart(self) -> bool:
        days = int(self.stats_period_combo.get_active_id() or "30")
        metric = self.stats_metric_combo.get_active_id() or "total"

        history = self.db.get_all_networks_history(days=days)
        known_nets = self.db.get_known_networks()

        if _MPL_AVAILABLE:
            ax = self._stats_ax
            ax.clear()
            ax.set_facecolor("#1e1e2e")
            self._stats_fig.patch.set_facecolor("#1e1e2e")

            # Build date range for X axis
            end = date.today()
            start = end - timedelta(days=days - 1)
            date_range = [start + timedelta(days=i) for i in range(days)]
            date_strs = [d.isoformat() for d in date_range]

            colors = ["#89b4fa", "#a6e3a1", "#fab387", "#f38ba8",
                      "#cba6f7", "#94e2d5", "#f9e2af", "#89dceb"]
            color_idx = 0

            # Draw one line per network
            for net in known_nets:
                uuid = net["connection_uuid"]
                name = net["connection_name"]
                net_data = {r["date"]: r for r in history.get(uuid, [])}
                key = {"total": "total_bytes", "rx": "rx_bytes", "tx": "tx_bytes"}[metric]
                y_vals = [net_data.get(d, {}).get(key, 0) / (1024 * 1024) for d in date_strs]
                ax.plot(
                    date_range, y_vals,
                    label=name,
                    color=colors[color_idx % len(colors)],
                    linewidth=1.8,
                    marker="o", markersize=3,
                )
                color_idx += 1

            # Draw bold total line
            all_data = {r["date"]: r for r in history.get("__all__", [])}
            key = {"total": "total_bytes", "rx": "rx_bytes", "tx": "tx_bytes"}[metric]
            y_all = [all_data.get(d, {}).get(key, 0) / (1024 * 1024) for d in date_strs]
            ax.plot(
                date_range, y_all,
                label="All Networks (Total)",
                color="#ffffff",
                linewidth=2.5,
                linestyle="--",
                alpha=0.8,
            )

            # Style
            ax.set_xlabel("Date", color="#cdd6f4", fontsize=9)
            ax.set_ylabel("MB", color="#cdd6f4", fontsize=9)
            ax.tick_params(colors="#cdd6f4", labelsize=8)
            for spine in ax.spines.values():
                spine.set_edgecolor("#45475a")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            self._stats_fig.autofmt_xdate(rotation=35, ha="right")
            ax.grid(True, color="#45475a", linewidth=0.5, alpha=0.5)
            legend = ax.legend(
                loc="upper left", fontsize=8,
                facecolor="#313244", edgecolor="#45475a",
                labelcolor="#cdd6f4",
            )
            title_map = {"total": "Total (RX+TX)", "rx": "Download (RX)", "tx": "Upload (TX)"}
            ax.set_title(
                f"Usage History — Last {days} days — {title_map[metric]}",
                color="#cdd6f4", fontsize=10, pad=8
            )
            self._stats_fig.tight_layout()
            self._stats_canvas.draw()

        # ── Summary table below chart ───────────────────────────────────
        for child in self._stats_summary_box.get_children():
            self._stats_summary_box.remove(child)

        if known_nets:
            header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            for text, expand in [("Network", True), ("↓ Download", False),
                                  ("↑ Upload", False), ("Total", False), ("Last Seen", False)]:
                lbl = Gtk.Label(xalign=0)
                lbl.set_markup(f"<b>{text}</b>")
                lbl.set_size_request(130 if expand else 110, -1)
                lbl.set_margin_end(8)
                header_row.pack_start(lbl, expand, expand, 0)
            self._stats_summary_box.pack_start(header_row, False, False, 2)
            self._stats_summary_box.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2
            )

            desig_uuid = self.config_mgr.designated_hotspot_uuid
            for net in known_nets:
                net_history = history.get(net["connection_uuid"], [])
                total_rx = sum(r["rx_bytes"] for r in net_history)
                total_tx = sum(r["tx_bytes"] for r in net_history)
                total_all = total_rx + total_tx

                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                star = " ⭐" if net["connection_uuid"] == desig_uuid else ""
                name_lbl = Gtk.Label(
                    label=f"{net['connection_name']}{star}", xalign=0
                )
                name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                name_lbl.set_size_request(130, -1)
                name_lbl.set_margin_end(8)
                row.pack_start(name_lbl, True, True, 0)

                for val, size in [
                    (format_bytes(total_rx), 110),
                    (format_bytes(total_tx), 110),
                    (format_bytes(total_all), 110),
                    (net.get("last_seen", "N/A"), 100),
                ]:
                    l = Gtk.Label(label=str(val), xalign=0)
                    l.set_size_request(size, -1)
                    l.set_margin_end(8)
                    row.pack_start(l, False, False, 0)

                self._stats_summary_box.pack_start(row, False, False, 1)

        self._stats_summary_box.show_all()
        return False  # GLib.idle_add one-shot

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3: CLI COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    def _build_cli_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Split pane: left = command buttons, right = output
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_border_width(8)

        # ── Left: scrollable command list ──────────────────────────────────
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_min_content_width(280)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_box.set_border_width(4)

        for group in CLI_COMMANDS:
            # Group header
            grp_lbl = Gtk.Label(xalign=0)
            grp_lbl.set_markup(f"<b>{GLib.markup_escape_text(group['group'])}</b>")
            grp_lbl.set_margin_top(8)
            left_box.pack_start(grp_lbl, False, False, 0)
            left_box.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2
            )

            for item in group["commands"]:
                btn_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                btn_row.set_border_width(4)

                btn = Gtk.Button(label=f"▶  {item['label']}")
                btn.set_tooltip_text(item["cmd"])
                btn.set_relief(Gtk.ReliefStyle.HALF)
                btn.connect("clicked", self._on_cli_run, item)
                btn_row.pack_start(btn, False, False, 0)

                desc_lbl = Gtk.Label(label=item["desc"], xalign=0)
                desc_lbl.set_line_wrap(True)
                desc_lbl.set_max_width_chars(34)
                desc_lbl.get_style_context().add_class("dim-label")
                desc_lbl.set_margin_start(4)
                btn_row.pack_start(desc_lbl, False, False, 0)

                left_box.pack_start(btn_row, False, False, 0)

        left_scroll.add(left_box)
        paned.pack1(left_scroll, resize=False, shrink=False)

        # ── Right: output panel ────────────────────────────────────────────
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right_box.set_border_width(4)

        out_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cli_cmd_label = Gtk.Label(label="Click a command to run it →", xalign=0)
        self.cli_cmd_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.cli_cmd_label.set_markup("<b>Click a command to run it →</b>")
        out_header.pack_start(self.cli_cmd_label, True, True, 0)

        clear_btn = Gtk.Button(label="🗑 Clear")
        clear_btn.connect("clicked", lambda b: self._cli_clear_output())
        out_header.pack_end(clear_btn, False, False, 0)
        right_box.pack_start(out_header, False, False, 4)

        out_scroll = Gtk.ScrolledWindow()
        out_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.cli_output = Gtk.TextView()
        self.cli_output.set_editable(False)
        self.cli_output.set_cursor_visible(False)
        self.cli_output.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.cli_output.set_monospace(True)
        self.cli_output.set_left_margin(6)
        self.cli_output.set_right_margin(6)
        self.cli_output.set_top_margin(4)
        self.cli_output_buf = self.cli_output.get_buffer()

        # Tags for colored output
        self.cli_output_buf.create_tag("cmd_tag", foreground="#89b4fa", weight=Pango.Weight.BOLD)
        self.cli_output_buf.create_tag("out_tag", foreground="#cdd6f4")
        self.cli_output_buf.create_tag("err_tag", foreground="#f38ba8")
        self.cli_output_buf.create_tag("ok_tag", foreground="#a6e3a1")

        out_scroll.add(self.cli_output)
        right_box.pack_start(out_scroll, True, True, 0)

        # Spinner row
        self.cli_spinner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.cli_spinner = Gtk.Spinner()
        self.cli_running_label = Gtk.Label(label="Running…")
        self.cli_spinner_box.pack_start(self.cli_spinner, False, False, 0)
        self.cli_spinner_box.pack_start(self.cli_running_label, False, False, 0)
        self.cli_spinner_box.set_no_show_all(True)
        right_box.pack_start(self.cli_spinner_box, False, False, 0)

        paned.pack2(right_box, resize=True, shrink=False)
        paned.set_position(290)

        outer.pack_start(paned, True, True, 0)
        return outer

    def _on_cli_run(self, _btn: Gtk.Widget, item: Dict[str, Any]) -> None:
        """Run a CLI command in a background thread and stream output to TextView."""
        cmd = item["cmd"]
        label = item["label"]

        self.cli_cmd_label.set_markup(f"<b>$ {cmd}</b>")
        buf = self.cli_output_buf

        # Append separator + command header
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(
            end,
            f"\n{'─' * 60}\n▶  {label}\n$ {cmd}\n{'─' * 60}\n",
            "cmd_tag"
        )
        # Auto-scroll
        self._cli_scroll_to_end()

        # Show spinner
        self.cli_spinner_box.set_visible(True)
        self.cli_spinner.start()

        def run_in_thread() -> None:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                rc = result.returncode

                def update_ui() -> None:
                    end_iter = buf.get_end_iter()
                    if stdout:
                        buf.insert_with_tags_by_name(end_iter, stdout, "out_tag")
                    if stderr:
                        buf.insert_with_tags_by_name(buf.get_end_iter(), stderr, "err_tag")
                    status_line = f"\n[Exit code: {rc}]\n"
                    tag = "ok_tag" if rc == 0 else "err_tag"
                    buf.insert_with_tags_by_name(buf.get_end_iter(), status_line, tag)
                    self.cli_spinner.stop()
                    self.cli_spinner_box.set_visible(False)
                    self._cli_scroll_to_end()

                GLib.idle_add(update_ui)

            except subprocess.TimeoutExpired:
                def timeout_ui() -> None:
                    end_iter = buf.get_end_iter()
                    buf.insert_with_tags_by_name(end_iter, "\n[Timed out after 30s]\n", "err_tag")
                    self.cli_spinner.stop()
                    self.cli_spinner_box.set_visible(False)
                    self._cli_scroll_to_end()
                GLib.idle_add(timeout_ui)
            except Exception as e:
                def err_ui() -> None:
                    end_iter = buf.get_end_iter()
                    buf.insert_with_tags_by_name(end_iter, f"\n[Error: {e}]\n", "err_tag")
                    self.cli_spinner.stop()
                    self.cli_spinner_box.set_visible(False)
                    self._cli_scroll_to_end()
                GLib.idle_add(err_ui)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()

    def _cli_scroll_to_end(self) -> None:
        """Scroll the CLI output TextView to the bottom."""
        buf = self.cli_output_buf
        end_iter = buf.get_end_iter()
        end_mark = buf.create_mark(None, end_iter, False)
        self.cli_output.scroll_to_mark(end_mark, 0.0, True, 0.0, 1.0)

    def _cli_clear_output(self) -> None:
        self.cli_output_buf.set_text("")


# ──────────────────────────────────────────────────────────────────────────────

def show_dashboard() -> None:
    win = DashboardWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    show_dashboard()
