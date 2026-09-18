"""Drive Mutter's monitor configuration for the GNOME fixture in a KVM guest.

    mutter-monitors.py state
    mutter-monitors.py mode CONNECTOR WIDTHxHEIGHT SCALE     e.g. mode Virtual-1 1280x720 1
    mutter-monitors.py enable CONNECTOR on|off              a second connector plugged in or out

`state` prints what Mutter sees; `mode` puts the named connector at that mode and scale
as the only logical monitor at the origin; `enable` adds the named connector to the
right of the first logical monitor with its preferred mode, or removes it. Everything
goes through org.gnome.Mutter.DisplayConfig (GetCurrentState / ApplyMonitorsConfig,
temporary method), the same calls GNOME Settings makes, so a real monitor change is
what the VNC daemon sees. Runs inside the fixture's session (its session bus).
"""

import json
import sys

import gi

STATE_TYPE = "(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})"
APPLY_TYPE = "(uua(iiduba(ssa{sv}))a{sv})"
TEMPORARY = 1


def _load_gio():
    """Select the Gio 2.0 typelib before importing it (keeps imports call-ordered)."""
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    return Gio, GLib


def _state(bus, gio, glib):
    reply = bus.call_sync(
        "org.gnome.Mutter.DisplayConfig",
        "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig",
        "GetCurrentState",
        None,
        glib.VariantType(STATE_TYPE),
        gio.DBusCallFlags.NONE,
        10000,
        None,
    )
    serial, monitors, logical, _properties = reply.unpack()
    return serial, monitors, logical


def _apply(bus, gio, glib, serial, logical_monitors):
    bus.call_sync(
        "org.gnome.Mutter.DisplayConfig",
        "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig",
        "ApplyMonitorsConfig",
        glib.Variant(APPLY_TYPE, (serial, TEMPORARY, logical_monitors, {})),
        None,
        gio.DBusCallFlags.NONE,
        30000,
        None,
    )


def _monitor(monitors, connector):
    for (name, _vendor, _product, _serial), modes, _properties in monitors:
        if name == connector:
            return modes
    raise SystemExit(f"no connector {connector!r}; Mutter has {[m[0][0] for m in monitors]}")


def _mode_id(modes, width, height, scale):
    """The id of a mode with that size that supports the scale, best refresh first."""
    candidates = [
        (refresh, mode_id)
        for mode_id, mode_width, mode_height, refresh, _scale, scales, _props in modes
        if (mode_width, mode_height) == (width, height) and float(scale) in scales
    ]
    if not candidates:
        sizes = sorted({(w, h) for _i, w, h, *_rest in modes})
        raise SystemExit(f"no {width}x{height} mode supporting scale {scale}; sizes {sizes}")
    return max(candidates)[1]


def _preferred(modes):
    for mode_id, _w, _h, _refresh, _scale, _scales, props in modes:
        if props.get("is-preferred"):
            return mode_id
    return modes[0][0]


def _current(modes):
    """The (mode id, width) of the mode a connector is showing now."""
    for mode_id, width, _h, _refresh, _scale, _scales, props in modes:
        if props.get("is-current"):
            return mode_id, width
    raise SystemExit("connector has no current mode")


def _print_state(monitors, logical):
    summary = {
        "monitors": [
            {
                "connector": name,
                "modes": sorted({f"{w}x{h}" for _i, w, h, *_r in modes}),
                "current": [
                    f"{w}x{h}" for _i, w, h, _r, _s, _sc, p in modes if p.get("is-current")
                ],
            }
            for (name, _v, _p, _s), modes, _props in monitors
        ],
        "logical": [
            {"x": x, "y": y, "scale": scale, "primary": primary, "connectors": [m[0] for m in mons]}
            for x, y, scale, _transform, primary, mons, _props in logical
        ],
    }
    print(json.dumps(summary))


def main(argv) -> None:
    gio, glib = _load_gio()
    bus = gio.bus_get_sync(gio.BusType.SESSION, None)
    serial, monitors, logical = _state(bus, gio, glib)
    command = argv[1] if len(argv) > 1 else "state"
    if command == "state":
        _print_state(monitors, logical)
        return
    if command == "mode":
        connector, size, scale = argv[2], argv[3], float(argv[4])
        width, height = (int(part) for part in size.split("x"))
        mode_id = _mode_id(_monitor(monitors, connector), width, height, scale)
        _apply(bus, gio, glib, serial, [(0, 0, scale, 0, True, [(connector, mode_id, {})])])
        return
    if command == "enable":
        connector, state = argv[2], argv[3]
        # The first logical monitor stays exactly as it is (the state lists its
        # connectors by name; the config wants each with the mode it shows now).
        x_pos, y_pos, scale, transform, _primary, mons, _props = logical[0]
        kept_monitors = [(name, _current(_monitor(monitors, name))[0], {}) for name, *_ in mons]
        kept = [(x_pos, y_pos, scale, transform, True, kept_monitors)]
        if state == "on":
            # Right of the first logical monitor, in its own preferred mode.
            _current_id, width = _current(_monitor(monitors, mons[0][0]))
            plugged = [(connector, _preferred(_monitor(monitors, connector)), {})]
            kept.append((x_pos + int(width / scale), 0, 1.0, 0, False, plugged))
        _apply(bus, gio, glib, serial, kept)
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
