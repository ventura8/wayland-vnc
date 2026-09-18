"""A minimal GDM Manager on the system bus, so gnome-shell builds its lock screen.

gnome-shell creates its ScreenShield (and thus registers org.gnome.Shell.ScreenShield,
which the lock scenario calls) only when `LoginManager.canLock()` succeeds, and that
asks GDM for org.gnome.DisplayManager.Manager's Version property on the SYSTEM bus and
checks it is at least 3.5.91. A real GNOME machine has GDM; the disposable KVM guest
does not run a display manager, so this stands in for exactly that one probe -- it
exposes the Version property and a no-op RegisterSession, and nothing else. It runs as
root (a system-bus name); the accompanying D-Bus policy lets the session read it.
"""

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402  (typelib version selected above)

NAME = "org.gnome.DisplayManager"
PATH = "/org/gnome/DisplayManager/Manager"
IFACE = "org.gnome.DisplayManager.Manager"
# What a real GDM reports; canLock() requires >= 3.5.91.
VERSION = "48.0"

INTROSPECTION = f"""
<node>
  <interface name="{IFACE}">
    <method name="RegisterSession">
      <arg type="a{{sv}}" name="details" direction="in"/>
    </method>
    <property name="Version" type="s" access="read"/>
  </interface>
</node>
"""


def _handle_method(_conn, _sender, _path, _iface, method, _params, invocation):
    if method == "RegisterSession":
        invocation.return_value(None)
    else:
        invocation.return_error_literal(
            Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method
        )


def _get_property(_conn, _sender, _path, _iface, prop):
    return GLib.Variant("s", VERSION) if prop == "Version" else None


def _on_bus_acquired(connection, _name):
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
    connection.register_object(
        PATH,
        node.interfaces[0],
        _handle_method,
        _get_property,
        None,
    )


def main() -> None:
    loop = GLib.MainLoop()
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        NAME,
        Gio.BusNameOwnerFlags.NONE,
        _on_bus_acquired,
        None,
        None,
    )
    loop.run()


if __name__ == "__main__":
    main()
