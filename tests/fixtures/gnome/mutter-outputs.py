"""Print Mutter's current monitor modes as JSON for the fixture smoke checker."""

import json

import gi


def _load_gio():
    """Select the Gio 2.0 typelib before importing it (keeps imports call-ordered)."""
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    return Gio, GLib


def main() -> None:
    gio, glib = _load_gio()
    bus = gio.bus_get_sync(gio.BusType.SESSION, None)
    reply = bus.call_sync(
        "org.gnome.Mutter.DisplayConfig",
        "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig",
        "GetCurrentState",
        None,
        glib.VariantType("(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})"),
        gio.DBusCallFlags.NONE,
        10000,
        None,
    )
    outputs = []
    for (connector, _vendor, _product, _serial), modes, _properties in reply.unpack()[1]:
        for _mode_id, width, height, _refresh, _scale, _scales, mode_properties in modes:
            if mode_properties.get("is-current"):
                outputs.append(
                    {"name": connector, "width": width, "height": height, "captured": True}
                )
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
