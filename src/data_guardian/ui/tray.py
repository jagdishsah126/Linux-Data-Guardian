"""System tray applet (XApp.StatusIcon with AppIndicator3 and Gtk.StatusIcon fallbacks)."""

from __future__ import annotations

import subprocess
import threading
from typing import Any, Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

HAS_XAPP = False
try:  # pragma: no cover - host dependent
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402

    HAS_XAPP = True
except Exception:  # noqa: BLE001
    HAS_XAPP = False

HAS_APPINDICATOR = False
if not HAS_XAPP:
    try:  # pragma: no cover - host dependent
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # noqa: E402

        HAS_APPINDICATOR = True
    except Exception:  # noqa: BLE001
        HAS_APPINDICATOR = False

from src.data_guardian.ipc import send_ipc_command  # noqa: E402
from src.data_guardian.ui.async_ipc import ipc_async  # noqa: E402
from src.data_guardian.network.byte_counter import format_bytes  # noqa: E402
from src.data_guardian.ui.dashboard import DashboardWindow  # noqa: E402
from src.data_guardian.window_tracker.x11_tracker import X11WindowTracker  # noqa: E402

REFRESH_SECONDS = 3
ICON_NAME = "linux-data-guardian"


class DataGuardianTray:
    """Tray applet: status, quick toggles, notifications and focus reporting."""

    def __init__(self) -> None:
        self.dashboard_window: Optional[DashboardWindow] = None
        self._loading_profile = False
        self._since_id: Optional[int] = None
        self._last_focus: Optional[tuple] = None
        self._tracker = X11WindowTracker()
        self._refresh_inflight = False
        self.backend = "none"

        self._build_menu()
        if HAS_XAPP:
            self._init_xapp()
        elif HAS_APPINDICATOR:
            self._init_appindicator()
        else:
            self._init_gtk_status_icon()

        GLib.timeout_add_seconds(REFRESH_SECONDS, self._refresh_tray)
        self._refresh_tray()

    # ------------------------------------------------------------------ backends

    def _init_xapp(self) -> None:
        self.backend = "xapp"
        self.status_icon = XApp.StatusIcon()
        self.status_icon.set_name(ICON_NAME)
        self.status_icon.set_icon_name(ICON_NAME)
        self.status_icon.set_tooltip_text("Linux Data Guardian")
        self.status_icon.set_primary_menu(self.menu)
        self.status_icon.set_secondary_menu(self.menu)
        self.status_icon.connect("activate", self._on_icon_clicked)
        self.status_icon.set_visible(True)

    def _init_appindicator(self) -> None:
        self.backend = "appindicator"
        self.indicator = AppIndicator3.Indicator.new(
            "linux-data-guardian-tray",
            ICON_NAME,
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.menu)

    def _init_gtk_status_icon(self) -> None:
        self.backend = "gtk"
        self.gtk_icon = Gtk.StatusIcon()
        self.gtk_icon.set_from_icon_name(ICON_NAME)
        self.gtk_icon.set_tooltip_text("Linux Data Guardian")
        self.gtk_icon.connect("activate", self._on_icon_clicked)
        self.gtk_icon.connect("popup-menu", self._on_gtk_popup_menu)
        self.gtk_icon.set_visible(True)

    # ------------------------------------------------------------------ callbacks

    def _on_icon_clicked(self, *args: Any) -> None:
        """XApp passes (icon, button, time); Gtk.StatusIcon passes (icon)."""
        self._open_dashboard(None)

    def _on_gtk_popup_menu(self, icon: Gtk.StatusIcon, button: int, activate_time: int) -> None:
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    # ----------------------------------------------------------------------- menu

    def _build_menu(self) -> None:
        self.menu = Gtk.Menu()

        self.menu_status = Gtk.MenuItem(label="Data Guardian: checking…")
        self.menu_status.set_sensitive(False)
        self.menu.append(self.menu_status)

        self.menu_usage = Gtk.MenuItem(label="Usage: —")
        self.menu_usage.set_sensitive(False)
        self.menu.append(self.menu_usage)

        self.menu_warning = Gtk.MenuItem(label="")
        self.menu_warning.set_sensitive(False)
        self.menu.append(self.menu_warning)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.item_balanced = Gtk.RadioMenuItem.new_with_label(None, "Balanced (block background updaters)")
        self.item_strict = Gtk.RadioMenuItem.new_with_label_from_widget(
            self.item_balanced, "Strict (foreground app + allowlisted tools)"
        )
        self.item_off = Gtk.RadioMenuItem.new_with_label_from_widget(self.item_balanced, "Off (monitor only)")
        self.item_balanced.connect("toggled", lambda w: self._on_profile_selected("balanced", w.get_active()))
        self.item_strict.connect("toggled", lambda w: self._on_profile_selected("strict", w.get_active()))
        self.item_off.connect("toggled", lambda w: self._on_profile_selected("off", w.get_active()))
        for item in (self.item_balanced, self.item_strict, self.item_off):
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_add_100 = Gtk.MenuItem(label="+100 MB temporary allowance")
        item_add_100.connect("activate", lambda w: ipc_async({"action": "grant_allowance", "mb": 100}))
        self.menu.append(item_add_100)

        self.item_apt = Gtk.MenuItem(label="Open APT window (15 min)")
        self.item_apt.connect("activate", self._on_apt_clicked)
        self.menu.append(self.item_apt)

        item_dashboard = Gtk.MenuItem(label="Open dashboard…")
        item_dashboard.connect("activate", self._open_dashboard)
        self.menu.append(item_dashboard)

        item_bypass = Gtk.MenuItem(label="Emergency bypass (unrestrict)")
        item_bypass.connect("activate", self._emergency_bypass)
        self.menu.append(item_bypass)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit tray applet")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        self.menu.append(item_quit)

        self.menu.show_all()
        self.menu_warning.hide()

    # ------------------------------------------------------------------- actions

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
        ipc_async({"action": "set_profile", "profile": profile},
                  lambda _resp: self._refresh_tray())

    def _on_apt_clicked(self, widget: Gtk.Widget) -> None:
        if self.item_apt.get_label().startswith("Close"):
            ipc_async({"action": "close_apt_window"})
        else:
            ipc_async({"action": "allow_apt_window", "minutes": 15})

    def _emergency_bypass(self, widget: Gtk.Widget) -> None:
        ipc_async({"action": "emergency_bypass"})

    # ---------------------------------------------------------------- rendering

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

    def _report_focus(self) -> None:
        """Tell the daemon which window is focused so strict mode can allow it."""
        try:
            info = self._tracker.get_active_window()
        except Exception:  # noqa: BLE001
            return
        if not info:
            return
        key = (info.app_name, info.pid)
        if key == self._last_focus:
            return
        self._last_focus = key
        ipc_async({"action": "report_focus", "app_name": info.app_name, "pid": info.pid})

    def _deliver_notifications(self, notes: list) -> None:
        for note in notes:
            try:
                subprocess.run(
                    ["notify-send", "-r", str(note.get("id", 0)),
                     "-u", note.get("urgency", "normal"),
                     note.get("title", "Linux Data Guardian"),
                     note.get("message", "")],
                    check=False,
                )
            except Exception:  # noqa: BLE001
                pass

    def _refresh_tray(self) -> bool:
        """Start a poll in a worker thread; never block the GTK main loop."""
        if self._refresh_inflight:
            return True
        self._refresh_inflight = True
        request: Dict[str, Any] = {"action": "get_status"}
        if self._since_id is not None:
            request["since_id"] = self._since_id

        def worker() -> None:
            status = send_ipc_command(request, timeout=2.0)
            GLib.idle_add(self._apply_tray_status, status)

        threading.Thread(target=worker, daemon=True, name="dg-tray-poll").start()
        return True

    def _apply_tray_status(self, status: Optional[Dict[str, Any]]) -> bool:
        """Apply a poll result on the GTK main thread."""
        self._refresh_inflight = False
        # The panel icon is always the green app icon. State is communicated
        # by the tooltip and menu text, never by swapping in theme icons.
        self._set_icon(ICON_NAME)
        if not status or status.get("status") != "ok":
            self._set_tooltip("Linux Data Guardian: daemon inactive")
            self.menu_status.set_label("Data Guardian: daemon inactive")
            self.menu_usage.set_label("Usage: —")
            return False

        latest = int(status.get("latest_notification_id", 0))
        if self._since_id is None:
            # First successful poll: adopt the cursor without replaying history.
            self._since_id = latest
        else:
            notes = status.get("notifications") or []
            if notes:
                self._deliver_notifications(notes)
            self._since_id = max(latest, max((int(n.get("id", 0)) for n in notes), default=0))

        is_hotspot = bool(status.get("is_hotspot_active"))
        conn_name = status.get("connection_name", "Disconnected")
        profile = status.get("current_profile", "balanced")
        blocked = bool(status.get("is_quota_blocked"))
        firewall_error = status.get("firewall_error")
        apt_remaining = int(status.get("apt_window_remaining", 0) or 0)
        metered_today = int(status.get("metered_today", 0) or 0)
        daily_limit = int(status.get("daily_limit_bytes", 0) or 0)

        if firewall_error:
            self.menu_status.set_label("PROTECTION FAILED")
            self._set_tooltip(f"Data Guardian: firewall error — {firewall_error}")
            self.menu_warning.set_label(f"⚠ {firewall_error[:60]}")
            self.menu_warning.show()
        elif blocked:
            reason = status.get("quota_reason") or "daily"
            self.menu_status.set_label(f"QUOTA EXCEEDED ({reason}) — {conn_name}")
            self._set_tooltip(f"Data Guardian: quota exceeded on {conn_name}")
            self.menu_warning.hide()
        elif is_hotspot:
            self.menu_status.set_label(f"Metered hotspot: {conn_name} ({profile})")
            self._set_tooltip(f"Data Guardian: {profile} on {conn_name}")
            self.menu_warning.hide()
        else:
            self.menu_status.set_label(f"Unmetered: {conn_name}")
            self._set_tooltip(f"Data Guardian: unmetered ({conn_name})")
            self.menu_warning.hide()

        if daily_limit:
            self.menu_usage.set_label(
                f"Today (metered): {format_bytes(metered_today)} / {format_bytes(daily_limit)}"
            )
        else:
            self.menu_usage.set_label(f"Today (metered): {format_bytes(metered_today)} — no limit set")

        if apt_remaining:
            minutes = max(1, apt_remaining // 60)
            self.item_apt.set_label(f"Close APT window ({minutes} min left)")
        else:
            self.item_apt.set_label("Open APT window (15 min)")

        self._loading_profile = True
        try:
            if profile == "balanced":
                self.item_balanced.set_active(True)
            elif profile == "strict":
                self.item_strict.set_active(True)
            elif profile == "off":
                self.item_off.set_active(True)
        finally:
            self._loading_profile = False

        if is_hotspot and profile == "strict":
            self._report_focus()

        return False  # one-shot GLib.idle_add callback


def run_tray() -> None:
    DataGuardianTray()
    Gtk.main()


if __name__ == "__main__":
    run_tray()
