"""Desktop notification, sent only when the pending set changed.

Uses the org.freedesktop.Notifications session-bus API directly (PyGObject) and
falls back to notify-send. The `desktop-entry` hint is what makes GNOME Shell
launch xyz.scottnelson.Rivet.desktop when the user clicks the notification, so we do not need to
stay alive waiting for an ActionInvoked signal (notify-send --action implies
--wait, which a Type=oneshot unit must not do).
"""
from __future__ import annotations

from common import log, run

APP_ID = "xyz.scottnelson.Rivet"   # the desktop file id
ICON = "software-update-available-symbolic"


def build_body(items: list[dict], flags: dict) -> tuple[str, str]:
    n = len(items)
    title = f"Rivet: {n} update" + ("" if n == 1 else "s")
    bits = []
    reboot = [i for i in items if i.get("requires_reboot")]
    if reboot:
        who = []
        if flags.get("kernel_pending"):
            who.append("kernel")
        if flags.get("akmod_nvidia_pending"):
            who.append("NVIDIA")
        label = f" ({', '.join(who)})" if who else ""
        bits.append(f"{len(reboot)} need reboot{label}")
    if flags.get("security_count"):
        bits.append(f"{flags['security_count']} security")
    if flags.get("blocked_count"):
        bits.append(f"{flags['blocked_count']} blocked by Steam")
    return title, ", ".join(bits) if bits else "No reboot needed."


def send(title: str, body: str, *, quiet: bool = False) -> bool:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        hints = {
            "desktop-entry": GLib.Variant("s", APP_ID),
            "urgency": GLib.Variant("y", 1),
        }
        bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                ("Rivet", 0, ICON, title, body, [], hints, -1),
            ),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return True
    except Exception as exc:
        log(f"notify via Gio failed ({type(exc).__name__}: {exc}); trying notify-send", quiet=quiet)

    cp = run(
        [
            "notify-send", "--app-name=Rivet", f"--icon={ICON}",
            f"--hint=string:desktop-entry:{APP_ID}", title, body,
        ],
        timeout=15,
    )
    return cp.returncode == 0
