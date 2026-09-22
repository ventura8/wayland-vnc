"""The KVM guest builder: its cloud-init renderer produces valid user-data for every
wlroots target from the target's own container package list, keeps secrets verbatim,
and the builder/runner agree on targets and ports."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "scripts" / "kvm" / "render-user-data.py"
TEMPLATE = REPO / "tests" / "kvm" / "wlroots.user-data.template"
WLROOTS_TARGETS = ("sway", "wayfire", "xfce-labwc", "lxqt-labwc", "hyprland")
AWKWARD_PASSWORD = r"pw=1&2\3$4 five"
KEY_PEM = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\nBBBB\n-----END RSA PRIVATE KEY-----\n"


def _render(target, tmp_path, *, password=AWKWARD_PASSWORD, key=True, template=TEMPLATE):
    key_path = tmp_path / "key.pem"
    key_path.write_text(KEY_PEM)
    out = tmp_path / f"{target}.yaml"
    import base64

    env = {
        **os.environ,
        "WAYLAND_VNC_TEMPLATE_TARGET": target,
        "WAYLAND_VNC_TEMPLATE_HASH": "$6$salt$hash",
        "WAYLAND_VNC_TEMPLATE_PASSWORD": password,
        "WAYLAND_VNC_TEMPLATE_KEY": str(key_path) if key else "",
        # The desktop template embeds a custom EDID; a valid 128-byte blob stands in.
        "WAYLAND_VNC_TEMPLATE_EDID_B64": base64.b64encode(bytes(128)).decode(),
    }
    result = subprocess.run(
        [sys.executable, str(RENDER), str(template), str(out)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out


def _unit_environment(data):
    """The Environment= assignments of the fixture unit cloud-init writes."""
    unit = next(f for f in data["write_files"] if f["path"].endswith("wayland-vnc-guest.service"))
    lines = [line for line in unit["content"].splitlines() if line.startswith("Environment=")]
    return dict(line[len("Environment=") :].split("=", 1) for line in lines)


@pytest.mark.parametrize("target", WLROOTS_TARGETS)
def test_every_wlroots_target_renders_valid_cloud_config(tmp_path, target):
    result, out = _render(target, tmp_path)
    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert text.startswith("#cloud-config\n")
    data = yaml.safe_load(text)
    assert data["hostname"] == f"wayland-vnc-{target}"
    assert data["users"][0]["name"] == "fixture"
    assert data["users"][0]["uid"] == 1000
    # The guest installs the container image's packages, plus what a machine needs.
    dockerfile = (REPO / "docker" / f"Dockerfile.{target}").read_text(encoding="utf-8")
    for package in ("wayvnc", "swaylock", "wlr-randr"):
        assert package in dockerfile
        assert package in data["packages"]
    assert "dbus-daemon" not in data["packages"], "container-only package leaked into the guest"
    for machine_package in ("seatd", "qemu-guest-agent"):
        assert machine_package in data["packages"]
    environment = _unit_environment(data)
    assert environment["FIXTURE_COMPOSITOR"] == target
    assert environment["FIXTURE_OUTPUT"] == "Virtual-1"
    assert environment["FIXTURE_SPARE_OUTPUT"] == "HEADLESS-1"
    assert environment["WLR_BACKENDS"] == "drm,headless"
    assert environment["WLR_RENDERER_ALLOW_SOFTWARE"] == "1"
    # sway and Hyprland create their hot-plug output on request; the others keep one
    # spare switched off.
    assert environment["WLR_HEADLESS_OUTPUTS"] == ("0" if target in ("sway", "hyprland") else "1")
    assert environment["XDG_RUNTIME_DIR"] == "/run/wayland-vnc-runtime"


def test_secrets_are_taken_verbatim_and_the_key_is_indented_into_its_block(tmp_path):
    result, out = _render("sway", tmp_path)
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    files = {f["path"]: f for f in data["write_files"]}
    credential = files["/home/fixture/.config/wayland-vnc/fixture.conf"]
    assert credential["content"] == f"username=fixture\npassword={AWKWARD_PASSWORD}\n"
    assert credential["permissions"] == "0600"
    assert credential["defer"] is True
    key = files["/home/fixture/.config/wayland-vnc/rsa.pem"]
    assert key["content"] == KEY_PEM
    assert data["users"][0]["passwd"] == "$6$salt$hash"


def test_a_multi_line_password_or_a_missing_key_is_refused(tmp_path):
    result, _ = _render("sway", tmp_path, password="two\nlines")
    assert result.returncode != 0
    assert "single line" in result.stderr
    result, _ = _render("sway", tmp_path, key=False)
    assert result.returncode != 0
    assert "WAYLAND_VNC_TEMPLATE_KEY" in result.stderr


def test_no_placeholder_survives_rendering(tmp_path):
    result, out = _render("wayfire", tmp_path)
    assert result.returncode == 0, result.stderr
    assert not re.search(r"__[A-Z_]+__", out.read_text(encoding="utf-8"))


def test_builder_targets_and_ports_are_distinct_and_documented():
    builder = (REPO / "scripts" / "kvm" / "build-guest.sh").read_text(encoding="utf-8")
    ports = dict(re.findall(r"^([a-z-]+)\) default_port=(\d+) ;;$", builder, re.M))
    assert set(ports) == {
        "hyprland",
        "sway",
        "wayfire",
        "xfce-labwc",
        "lxqt-labwc",
        "gnome",
        "plasma",
    }
    assert len(set(ports.values())) == len(ports), "two guests would fight over a port"
    testing = (REPO / "docs" / "testing.md").read_text(encoding="utf-8")
    assert "scripts/kvm/build-guest.sh TARGET" in testing
    qualify_all = (REPO / "scripts" / "qualify-all.sh").read_text(encoding="utf-8")
    assert "--kvm" in qualify_all
    assert "boot_guest" in qualify_all


@pytest.mark.parametrize("target", ("gnome", "plasma"))
def test_the_desktop_guests_render_a_logind_session_unit(tmp_path, target):
    template = REPO / "tests" / "kvm" / "desktop.user-data.template"
    result, out = _render(target, tmp_path, template=template)
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    unit = next(f for f in data["write_files"] if f["path"].endswith("wayland-vnc-guest.service"))
    assert "PAMName=login" in unit["content"]
    environment = _unit_environment(data)
    assert environment["FIXTURE_DRM"] == "1"
    assert environment["FIXTURE_SPARE_OUTPUT"] == "Virtual-2"
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    expected_desktop = "GNOME" if target == "gnome" else "KDE"
    assert environment["XDG_CURRENT_DESKTOP"] == expected_desktop
    session = "gnome-session.py" if target == "gnome" else "plasma-session.py"
    assert session in unit["content"]
    # The runtime stage's packages, not the build stage's.
    compositor = "gnome-shell" if target == "gnome" else "kwin-wayland"
    assert compositor in data["packages"]
    assert "meson" not in data["packages"]
    # A custom EDID (720p + 1080p + 4K) loaded via the kernel cmdline, and a reboot to
    # read it: the compositor's DisplayConfig needs all three modes and virtio-gpu
    # advertises only some of them without this.
    import base64

    edid = next(f for f in data["write_files"] if f["path"] == "/lib/firmware/edid/wayland-vnc.bin")
    assert len(base64.b64decode(edid["content"])) == 128
    grub = next(f for f in data["write_files"] if f["path"].endswith("99-wayland-vnc-edid.cfg"))
    assert "drm.edid_firmware=Virtual-1:edid/wayland-vnc.bin" in grub["content"]
    assert data["power_state"]["mode"] == "reboot"
    # The fixture is enabled but not started in runcmd; it comes up on the reboot.
    runcmd = " ".join(str(c) for c in data["runcmd"])
    assert "enable" in runcmd
    assert "wayland-vnc-guest.service" in runcmd
    assert "start" not in runcmd or "guest-agent" in runcmd


def _dtds(edid: bytes):
    """Decode the four 18-byte EDID descriptors as (width, height, refresh) detailed
    timings, skipping any that are not timings (a zero pixel clock marks a text/other
    descriptor). Mirrors what the guest kernel parses from the firmware EDID."""
    timings = []
    for start in range(54, 54 + 4 * 18, 18):
        d = edid[start : start + 18]
        clock_khz = (d[0] | (d[1] << 8)) * 10
        if clock_khz == 0:
            continue
        hactive = d[2] | ((d[4] >> 4) << 8)
        hblank = d[3] | ((d[4] & 0xF) << 8)
        vactive = d[5] | ((d[7] >> 4) << 8)
        vblank = d[6] | ((d[7] & 0xF) << 8)
        refresh = round(clock_khz * 1000 / ((hactive + hblank) * (vactive + vblank)))
        timings.append((hactive, vactive, refresh))
    return timings


def test_make_edid_leads_with_a_sacrificial_timing_then_the_scenario_modes(tmp_path):
    # The guest's virtio-gpu connector always prunes the FIRST (preferred) detailed
    # timing of a firmware EDID, so the blob must lead with a throwaway timing and put
    # the three real modes after it, and 4K must be 30 Hz (its 60 Hz clock is pruned as
    # too fast). Reordering these back to a real-mode-first list silently breaks the
    # GNOME/Plasma guests, so pin the order here.
    out = tmp_path / "edid.bin"
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "kvm" / "make-edid.py"), str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    edid = out.read_bytes()
    assert len(edid) == 128
    assert edid[127] == (-sum(edid[:127])) & 0xFF  # valid checksum
    timings = _dtds(edid)
    assert timings[0] == (1600, 1200, 60), f"first timing must be sacrificial: {timings}"
    assert (3840, 2160, 30) in timings, timings
    assert (1920, 1080, 60) in timings, timings
    assert (1280, 720, 60) in timings, timings
