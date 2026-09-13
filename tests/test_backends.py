import pytest

from wayland_vnc.backends import BACKENDS, get_backend


@pytest.mark.parametrize("name", BACKENDS)
def test_units_are_project_scoped_and_hardened(name):
    unit = get_backend(name).service_unit()
    assert f"Description=wayland-vnc {name} backend" in unit
    assert "ExecStart=/opt/wayland-vnc/" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "/usr/bin/" not in unit
    assert "sh -c" not in unit
    # Any mount-namespacing directive in a user unit is an unprivileged user
    # namespace, which Ubuntu's AppArmor bars from reading other processes' fd
    # tables while applying none of the read-only mounts; serve on GNOME must read
    # the daemon's fd table to attribute port 5900 to it, or it fails and restarts
    # for ever. Neither the generated nor the packaged unit may carry one.
    for directive in ("PrivateTmp", "ProtectSystem", "ProtectHome", "ReadWritePaths"):
        assert directive not in unit, directive
    assert "IPAddressAllow" not in unit, "a user manager cannot apply it; never claim it"


def test_gnome_profile_is_conservative():
    unit = get_backend("grd").service_unit()
    assert "WAYLAND_VNC_ENABLE_DMABUF=0" in unit
    assert "WAYLAND_VNC_ENABLE_CLIPBOARD=0" in unit


def test_network_defaults():
    arguments = get_backend("w0vncserver").arguments
    assert "-localhost" in arguments
    assert "-SecurityTypes=RA2_256,RA2" in arguments
    assert not any(argument.startswith("-SecurityTypes=None") for argument in arguments)
    assert "wayvnc.conf" in get_backend("wayvnc").arguments[0]


def test_unknown_backend():
    with pytest.raises(ValueError, match="Unknown backend"):
        get_backend("x11")
