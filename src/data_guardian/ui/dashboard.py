"""Lightweight GTK3 Dashboard for Linux Data Guardian."""

from __future__ import annotations
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from src.data_guardian.ipc import send_ipc_command
from src.data_guardian.database import Database
from src.data_guardian.config import ConfigManager
from src.data_guardian.network.byte_counter import format_bytes
from src.data_guardian.network.nm_monitor import NetworkMonitor


class DashboardWindow(Gtk.Window):
    """Native GTK3 status and control dashboard."""

    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        super().__init__(title="Linux Data Guardian")
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(self.config_mgr.db_path)

        self.set_default_size(480, 520)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(12)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(main_box)

        # 1. Header Banner
        self.status_banner = Gtk.Label()
        self.status_banner.set_markup("<b><big>Linux Data Guardian</big></b>\nConnecting to daemon...")
        self.status_banner.set_justify(Gtk.Justification.CENTER)
        main_box.pack_start(self.status_banner, False, False, 5)

        # 2. Connection Info Frame
        conn_frame = Gtk.Frame(label="Active Network Connection")
        conn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        conn_box.set_border_width(8)
        conn_frame.add(conn_box)

        self.conn_label = Gtk.Label(label="Network: Detecting...", xalign=0)
        conn_box.pack_start(self.conn_label, False, False, 0)

        # Hotspot Selector
        hs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hs_label = Gtk.Label(label="Designated Hotspot:", xalign=0)
        hs_box.pack_start(hs_label, False, False, 0)

        self.combo_hotspots = Gtk.ComboBoxText()
        self._populate_hotspots()
        self.combo_hotspots.connect("changed", self._on_hotspot_selected)
        hs_box.pack_start(self.combo_hotspots, True, True, 0)
        conn_box.pack_start(hs_box, False, False, 0)

        main_box.pack_start(conn_frame, False, False, 0)

        # 3. Usage & Quota Frame
        usage_frame = Gtk.Frame(label="Today's Mobile Data Usage")
        usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        usage_box.set_border_width(8)
        usage_frame.add(usage_box)

        self.usage_label = Gtk.Label(label="Used: 0 MB / 1000 MB", xalign=0)
        usage_box.pack_start(self.usage_label, False, False, 0)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_fraction(0.0)
        usage_box.pack_start(self.progress_bar, False, False, 0)

        # Temporary Allowance Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_100 = Gtk.Button(label="+100 MB")
        btn_100.connect("clicked", lambda b: self._grant_allowance(100))
        btn_500 = Gtk.Button(label="+500 MB")
        btn_500.connect("clicked", lambda b: self._grant_allowance(500))
        btn_bypass = Gtk.Button(label="Emergency Bypass")
        btn_bypass.connect("clicked", self._emergency_bypass)

        btn_box.pack_start(btn_100, True, True, 0)
        btn_box.pack_start(btn_500, True, True, 0)
        btn_box.pack_start(btn_bypass, True, True, 0)
        usage_box.pack_start(btn_box, False, False, 4)

        main_box.pack_start(usage_frame, False, False, 0)

        # 4. Profile Mode Frame
        profile_frame = Gtk.Frame(label="Data Saver Profile")
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        profile_box.set_border_width(8)
        profile_frame.add(profile_box)

        self.radio_balanced = Gtk.RadioButton.new_with_label_from_widget(None, "Balanced")
        self.radio_strict = Gtk.RadioButton.new_with_label_from_widget(self.radio_balanced, "Strict (Foreground)")
        self.radio_off = Gtk.RadioButton.new_with_label_from_widget(self.radio_balanced, "Off (Monitoring Only)")

        self.radio_balanced.connect("toggled", lambda b: self._on_profile_toggled("balanced", b.get_active()))
        self.radio_strict.connect("toggled", lambda b: self._on_profile_toggled("strict", b.get_active()))
        self.radio_off.connect("toggled", lambda b: self._on_profile_toggled("off", b.get_active()))

        profile_box.pack_start(self.radio_balanced, True, True, 0)
        profile_box.pack_start(self.radio_strict, True, True, 0)
        profile_box.pack_start(self.radio_off, True, True, 0)
        main_box.pack_start(profile_frame, False, False, 0)

        # 5. Recent Download Approvals Log
        log_frame = Gtk.Frame(label="Recent Download Approvals & Interceptions")
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_border_width(8)
        log_frame.add(log_box)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(110)
        self.log_list = Gtk.ListBox()
        scrolled.add(self.log_list)
        log_box.pack_start(scrolled, True, True, 0)

        main_box.pack_start(log_frame, True, True, 0)

        # Periodic refresh
        GLib.timeout_add_seconds(2, self._refresh_status)
        self._refresh_status()

    def _populate_hotspots(self) -> None:
        """Fills the hotspot selection dropdown with saved wireless networks."""
        self.combo_hotspots.remove_all()
        saved = NetworkMonitor.list_saved_connections()
        desig_uuid = self.config_mgr.designated_hotspot_uuid

        selected_idx = 0
        idx = 0
        for conn in saved:
            if conn["type"] == "802-11-wireless":
                self.combo_hotspots.append(conn["uuid"], f"{conn['name']} ({conn['uuid'][:8]}...)")
                if conn["uuid"] == desig_uuid:
                    selected_idx = idx
                idx += 1

        if idx > 0:
            self.combo_hotspots.set_active(selected_idx)

    def _on_hotspot_selected(self, combo: Gtk.ComboBoxText) -> None:
        selected_uuid = combo.get_active_id()
        if selected_uuid and selected_uuid != self.config_mgr.designated_hotspot_uuid:
            self.config_mgr.designated_hotspot_uuid = selected_uuid
            send_ipc_command({"action": "set_hotspot_uuid", "uuid": selected_uuid})

    def _on_profile_toggled(self, profile_name: str, is_active: bool) -> None:
        if is_active:
            send_ipc_command({"action": "set_profile", "profile": profile_name})

    def _grant_allowance(self, mb: int) -> None:
        send_ipc_command({"action": "grant_allowance", "mb": mb})
        self._refresh_status()

    def _emergency_bypass(self, widget: Gtk.Widget) -> None:
        send_ipc_command({"action": "emergency_bypass"})
        self.radio_off.set_active(True)
        self._refresh_status()

    def _refresh_status(self) -> bool:
        """Polls daemon for live status and updates widgets."""
        status = send_ipc_command({"action": "get_status"})
        if not status:
            self.status_banner.set_markup(
                "<b><big>Linux Data Guardian</big></b>\n<span foreground='gray'>Daemon is not running</span>"
            )
            # Read directly from DB fallback
            today = self.db.get_today_usage()
            used = today.get("total_bytes", 0)
            limit = self.config_mgr.daily_limit_mb * 1024 * 1024
            self.usage_label.set_text(f"Used: {format_bytes(used)} / {format_bytes(limit)}")
            frac = min(used / limit, 1.0) if limit > 0 else 0.0
            self.progress_bar.set_fraction(frac)
            self._update_log_list()
            return True

        is_hotspot = status.get("is_hotspot_active", False)
        conn_name = status.get("connection_name", "Disconnected")
        profile = status.get("current_profile", "balanced")
        is_blocked = status.get("is_quota_blocked", False)
        today_total = status.get("today_total", 0)
        daily_limit = status.get("daily_limit_bytes", self.config_mgr.daily_limit_mb * 1024 * 1024)

        if is_hotspot:
            color = "orange" if profile == "balanced" else ("blue" if profile == "strict" else "gray")
            blocked_text = " — <span foreground='red'><b>QUOTA EXCEEDED</b></span>" if is_blocked else ""
            self.status_banner.set_markup(
                f"<b><big>Linux Data Guardian</big></b>\n"
                f"<span foreground='{color}'><b>● Metered Hotspot: {conn_name} ({profile.upper()} ACTIVE)</b></span>{blocked_text}"
            )
        else:
            self.status_banner.set_markup(
                f"<b><big>Linux Data Guardian</big></b>\n"
                f"<span foreground='green'><b>● Unrestricted Network: {conn_name}</b></span>"
            )

        self.conn_label.set_text(f"Connected: {conn_name} ({status.get('interface', '')}) | Focus: {status.get('active_app', 'none')}")

        frac = min(today_total / daily_limit, 1.0) if daily_limit > 0 else 0.0
        self.progress_bar.set_fraction(frac)
        pct = int(frac * 100)
        self.progress_bar.set_text(f"{pct}%")
        self.usage_label.set_text(f"Used: {format_bytes(today_total)} / {format_bytes(daily_limit)}")

        # Sync radio button if changed externally
        if profile == "balanced" and not self.radio_balanced.get_active():
            self.radio_balanced.set_active(True)
        elif profile == "strict" and not self.radio_strict.get_active():
            self.radio_strict.set_active(True)
        elif profile == "off" and not self.radio_off.get_active():
            self.radio_off.set_active(True)

        self._update_log_list()
        return True

    def _update_log_list(self) -> None:
        for child in self.log_list.get_children():
            self.log_list.remove(child)

        approvals = self.db.get_recent_approvals(limit=5)
        if not approvals:
            lbl = Gtk.Label(label="No recent package approvals recorded.", xalign=0)
            self.log_list.add(lbl)
        else:
            for ev in approvals:
                dec = ev["decision"]
                dec_color = "green" if dec == "approved" else "red"
                txt = f"[{ev['timestamp'].split('T')[-1][:8]}] <b>{ev['tool']}</b>: {ev['packages'][:35]} → <span foreground='{dec_color}'>{dec.upper()}</span>"
                row_lbl = Gtk.Label(xalign=0)
                row_lbl.set_markup(txt)
                self.log_list.add(row_lbl)
        self.log_list.show_all()


def show_dashboard() -> None:
    win = DashboardWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    show_dashboard()
