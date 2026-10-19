"""System tray applet for Linux Data Guardian supporting XApp.StatusIcon (Linux Mint) and AppIndicator."""

from __future__ import annotations
import subprocess
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

# Try importing XApp first (Native to Linux Mint XFCE panel: xapp-status-plugin)
HAS_XAPP = False
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
    HAS_XAPP = True
except Exception:
    HAS_XAPP = False

# Fallback: AppIndicator3
HAS_APPINDICATOR = False
if not HAS_XAPP:
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3
        HAS_APPINDICATOR = True
    except Exception:
        HAS_APPINDICATOR = False

from src.data_guardian.ipc import send_ipc_command
from src.data_guardian.network.byte_counter import format_bytes
from src.data_guardian.ui.dashboard import DashboardWindow


class DataGuardianTray:
    """System tray applet for quick toggles and status display in Linux Mint panel."""

    def __init__(self) -> None:
        self.dashboard_window: DashboardWindow | None = None
        self._loading_profile: bool = False
        self._build_menu()

        self.backend = "none"
        if HAS_XAPP:
            self._init_xapp()
        elif HAS_APPINDICATOR:
            self._init_appindicator()
        else:
            self._init_gtk_status_icon()

        # Periodic status refresh every 3 seconds
        GLib.timeout_add_seconds(3, self._refresh_tray)
        self._refresh_tray()

    def _init_xapp(self) -> None:
        """Native Linux Mint XApp.StatusIcon for xapp-status-plugin."""
        self.backend = "xapp"
        self.status_icon = XApp.StatusIcon()
        self.status_icon.set_name("linux-data-guardian")
        self.status_icon.set_icon_name("linux-data-guardian")
        self.status_icon.set_tooltip_text("Linux Data Guardian (Click to open dashboard)")
        self.status_icon.set_primary_menu(self.menu)
        self.status_icon.set_secondary_menu(self.menu)
        self.status_icon.connect("activate", self._on_icon_clicked)
        self.status_icon.set_visible(True)

        # Notify user that tray icon is running
        try:
            import subprocess
            subprocess.run([
                "notify-send",
                "-u", "low",
                "-i", "security-medium-symbolic",
                "Linux Data Guardian",
                "Tray applet is active in your bottom status bar."
            ], check=False)
        except Exception:
            pass

    def _init_appindicator(self) -> None:
        """AppIndicator3 fallback."""
        self.backend = "appindicator"
        self.indicator = AppIndicator3.Indicator.new(
            "linux-data-guardian-tray",
            "network-wireless-symbolic",
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.menu)

    def _init_gtk_status_icon(self) -> None:
        """Gtk.StatusIcon fallback."""
        self.backend = "gtk"
        self.gtk_icon = Gtk.StatusIcon()
        self.gtk_icon.set_from_icon_name("network-wireless-symbolic")
        self.gtk_icon.set_tooltip_text("Linux Data Guardian")
        self.gtk_icon.connect("activate", self._on_icon_clicked)
        self.gtk_icon.connect("popup-menu", self._on_gtk_popup_menu)
        self.gtk_icon.set_visible(True)

    def _on_icon_clicked(self, widget: object = None) -> None:
        """Left-click on icon opens dashboard."""
        self._open_dashboard(None)

    def _on_gtk_popup_menu(self, icon: Gtk.StatusIcon, button: int, activate_time: int) -> None:
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def _build_menu(self) -> None:
        self.menu = Gtk.Menu()

        # 1. Status Header
        self.menu_status = Gtk.MenuItem(label="Data Guardian: Checking...")
        self.menu_status.set_sensitive(False)
        self.menu.append(self.menu_status)

        # 2. Usage Summary
        self.menu_usage = Gtk.MenuItem(label="Usage: 0 MB / 1000 MB")
        self.menu_usage.set_sensitive(False)
        self.menu.append(self.menu_usage)

        self.menu.append(Gtk.SeparatorMenuItem())

        # 3. Profile Items
        self.item_balanced = Gtk.RadioMenuItem.new_with_label(None, "● Balanced Data Saver")
        self.item_strict = Gtk.RadioMenuItem.new_with_label_from_widget(self.item_balanced, "● Strict (Foreground Only)")
        self.item_off = Gtk.RadioMenuItem.new_with_label_from_widget(self.item_balanced, "○ Data Saver Off")

        self.item_balanced.connect("toggled", lambda w: self._on_profile_selected("balanced", w.get_active()))
        self.item_strict.connect("toggled", lambda w: self._on_profile_selected("strict", w.get_active()))
        self.item_off.connect("toggled", lambda w: self._on_profile_selected("off", w.get_active()))

        self.menu.append(self.item_balanced)
        self.menu.append(self.item_strict)
        self.menu.append(self.item_off)

        self.menu.append(Gtk.SeparatorMenuItem())

        # 4. Quick Actions
        item_add_100 = Gtk.MenuItem(label="+100 MB Temporary Allowance")
        item_add_100.connect("activate", lambda w: send_ipc_command({"action": "grant_allowance", "mb": 100}))
        self.menu.append(item_add_100)

        item_dashboard = Gtk.MenuItem(label="Open Dashboard...")
        item_dashboard.connect("activate", self._open_dashboard)
        self.menu.append(item_dashboard)

        item_bypass = Gtk.MenuItem(label="Emergency Bypass (Unrestrict)")
        item_bypass.connect("activate", self._emergency_bypass)
        self.menu.append(item_bypass)

        self.menu.append(Gtk.SeparatorMenuItem())

        # 5. Quit
        item_quit = Gtk.MenuItem(label="Quit Tray Applet")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        self.menu.append(item_quit)

        self.menu.show_all()

    def _open_dashboard(self, widget: object = None) -> None:
        if not self.dashboard_window:
            self.dashboard_window = DashboardWindow()
            self.dashboard_window.connect("destroy", self._on_dashboard_closed)
            self.dashboard_window.show_all()
        else:
            self.dashboard_window.present()

    def _on_dashboard_closed(self, widget: Gtk.Widget) -> None:
        self.dashboard_window = None

    def _on_profile_selected(self, profile: str, is_active: bool) -> None:
        if self._loading_profile or not is_active:
            return
        send_ipc_command({"action": "set_profile", "profile": profile})
        self._refresh_tray()

    def _emergency_bypass(self, widget: Gtk.Widget) -> None:
        send_ipc_command({"action": "emergency_bypass"})
        self.item_off.set_active(True)

    def _set_icon(self, icon_name: str) -> None:
        if self.backend == "xapp":
            self.status_icon.set_icon_name(icon_name)
        elif self.backend == "appindicator":
            self.indicator.set_icon_full(icon_name, "Data Guardian")
        elif self.backend == "gtk":
            self.gtk_icon.set_from_icon_name(icon_name)

    def _set_tooltip(self, text: str) -> None:
        if self.backend == "xapp":
            self.status_icon.set_tooltip_text(text)
        elif self.backend == "gtk":
            self.gtk_icon.set_tooltip_text(text)

    def _refresh_tray(self) -> bool:
        status = send_ipc_command({"action": "get_status"})
        if not status:
            self._set_icon("network-wireless-symbolic")
            self._set_tooltip("Linux Data Guardian: Daemon Inactive")
            self.menu_status.set_label("Data Guardian: Daemon Inactive")
            return True

        is_hotspot = status.get("is_hotspot_active", False)
        conn_name = status.get("connection_name", "Disconnected")
        profile = status.get("current_profile", "balanced")
        is_blocked = status.get("is_quota_blocked", False)
        today_total = status.get("today_total", 0)
        daily_limit = status.get("daily_limit_bytes", 1000 * 1024 * 1024)

        if is_blocked:
            self._set_icon("dialog-warning-symbolic")
            self.menu_status.set_label(f"QUOTA EXCEEDED ({conn_name})")
            self._set_tooltip(f"Data Guardian: Quota Exceeded on {conn_name}")
        elif is_hotspot:
            self._set_icon("linux-data-guardian")
            self.menu_status.set_label(f"Metered Hotspot: {conn_name} ({profile.title()})")
            self._set_tooltip(f"Data Guardian: {profile.title()} Mode on {conn_name}")
        else:
            self._set_icon("linux-data-guardian")
            self.menu_status.set_label(f"Unmetered Wi-Fi: {conn_name}")
            self._set_tooltip(f"Data Guardian: Unmetered ({conn_name})")

        self.menu_usage.set_label(f"Today: {format_bytes(today_total)} / {format_bytes(daily_limit)}")

        # Deliver desktop notifications queued by the daemon
        for note in status.get("notifications", []):
            try:
                subprocess.run(
                    ["notify-send", "-u", note.get("urgency", "normal"), note.get("title", "Linux Data Guardian"), note.get("message", "")],
                    check=False
                )
            except Exception:
                pass

        # Sync radio toggles without triggering callbacks
        self._loading_profile = True
        try:
            if profile == "balanced" and not self.item_balanced.get_active():
                self.item_balanced.set_active(True)
            elif profile == "strict" and not self.item_strict.get_active():
                self.item_strict.set_active(True)
            elif profile == "off" and not self.item_off.get_active():
                self.item_off.set_active(True)
        finally:
            self._loading_profile = False

        return True


def run_tray() -> None:
    DataGuardianTray()
    Gtk.main()


if __name__ == "__main__":
    run_tray()
