"""The hardware-validation helpers: obfuscation, frame proof, and the phase report."""

import json
import subprocess

import pytest
from PIL import Image

from wayland_vnc import hardware

# A synthetic, disposable vector: this password has never been used for access
# anywhere. It pins the obfuscation the actual RealVNC Viewer accepts, which is
# deliberately NOT TigerVNC's server-side password format: those use a different byte
# order, and pinning to them produced connection files RealVNC silently refused.
KNOWN_PASSWORD = "TYwkoRu6"
KNOWN_OBFUSCATED = "bc1dfa1a3f8f673b"


def test_obfuscation_matches_a_connection_file_realvnc_accepted():
    """If this drifts, every generated .vnc connection file silently stops working."""
    assert hardware.obfuscate_password(KNOWN_PASSWORD) == KNOWN_OBFUSCATED


def test_obfuscation_round_trips_through_openssl():
    encrypted = bytes.fromhex(hardware.obfuscate_password(KNOWN_PASSWORD))
    plain = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-des-ecb",
            "-K",
            hardware.VNC_FIXED_KEY,
            "-nopad",
            "-provider",
            "legacy",
            "-provider",
            "default",
        ],
        input=encrypted,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    assert plain.rstrip(b"\0").decode() == KNOWN_PASSWORD


@pytest.mark.parametrize(
    "password,expected_block",
    [("short", b"short\x00\x00\x00"), ("waytoolongpassword", b"waytoolo")],
)
def test_obfuscation_pads_and_truncates_to_the_des_block(password, expected_block):
    seen = {}

    def run(args, data):
        seen["data"] = data
        assert "-provider" in args, "OpenSSL 3 needs the legacy provider"
        assert "legacy" in args, "OpenSSL 3 needs the legacy provider"
        return b"\x01\x02\x03\x04\x05\x06\x07\x08"

    assert hardware.obfuscate_password(password, run=run) == "0102030405060708"
    assert seen["data"] == expected_block


def test_connection_file_never_contains_the_plaintext_password():
    body = hardware.connection_file("192.168.1.33:5900", KNOWN_PASSWORD)
    assert KNOWN_PASSWORD not in body
    assert f"Password={KNOWN_OBFUSCATED}" in body
    assert "Host=192.168.1.33:5900" in body
    # The viewer must not prompt or store anything during an unattended run.
    for option in ("AutoReconnect=0", "PasswordStoreOffer=0", "VerifyId=0"):
        assert option in body


def _frame(tmp_path, name, size, builder):
    path = tmp_path / name
    image = Image.new("RGB", size)
    builder(image)
    image.save(path)
    return path


def test_frame_evidence_accepts_a_busy_desktop(tmp_path):
    def busy(image):
        width, height = image.size
        for x in range(width):
            for y in range(0, height, 7):
                image.putpixel((x, y), (x % 256, y % 256, (x * y) % 256))

    evidence = hardware.frame_evidence(_frame(tmp_path, "busy.png", (800, 600), busy))
    assert evidence.width == 800
    assert evidence.height == 600
    assert evidence.colours > 40
    assert evidence.as_dict()["colours"] == evidence.colours


def test_frame_evidence_refuses_a_blank_screen(tmp_path):
    """A black or solid frame means the viewer connected but no desktop arrived."""
    blank = _frame(tmp_path, "blank.png", (800, 600), lambda image: None)
    with pytest.raises(ValueError, match="flat"):
        hardware.frame_evidence(blank)


def test_frame_evidence_refuses_a_thumbnail(tmp_path):
    tiny = _frame(tmp_path, "tiny.png", (320, 200), lambda image: None)
    with pytest.raises(ValueError, match="too small"):
        hardware.frame_evidence(tiny)


def _report():
    return hardware.Report(
        host="zenbook",
        session="wayland",
        desktop="ubuntu:GNOME",
        viewer="RealVNC(R) Viewer 7.15.1",
        commit="abc123",
        recorded_at="20260915T000000Z",
    )


def test_report_records_phases_in_order_and_is_incomplete_when_any_blocked(tmp_path):
    report = _report()
    report.record("install", hardware.PASSED, "enabled and active")
    report.record("e2e", hardware.BLOCKED, "keyring locked")
    assert report.ok, "blocked must not fail the run"
    written = json.loads(report.write(tmp_path / "report.json").read_text())
    assert written["result"] == hardware.INCOMPLETE, "blocked must never read as passed"
    assert list(written["phases"]) == ["install", "e2e"]
    assert written["phases"]["e2e"]["detail"] == "keyring locked"
    assert written["schema_version"] == 1
    assert format((tmp_path / "report.json").stat().st_mode & 0o777, "03o") == "600"


def test_report_passes_only_when_every_phase_passed():
    report = _report()
    report.record("install", hardware.PASSED, "enabled and active")
    report.record("e2e", hardware.PASSED, "a real frame arrived")
    assert report.as_dict()["result"] == hardware.PASSED


def test_report_with_no_phases_is_incomplete_not_passed():
    assert _report().as_dict()["result"] == hardware.INCOMPLETE


def test_report_fails_the_run_when_any_phase_failed():
    report = _report()
    report.record("install", hardware.PASSED, "ok")
    report.record("uninstall", hardware.FAILED, "left files behind")
    assert not report.ok
    assert report.as_dict()["result"] == hardware.FAILED


def test_report_rejects_an_unknown_status():
    report = _report()
    with pytest.raises(ValueError, match="unknown status"):
        report.record("install", "probably-fine", "no")


def test_grd_stored_password_reads_the_keyring(monkeypatch):
    """The grd backend's effective password lives in the keyring, not our config."""
    looked_up = {}

    class FakeSchema:
        @staticmethod
        def new(name, flags, attrs):
            looked_up["schema"] = (name, attrs)
            return "schema-handle"

    class FakeSecret:
        Schema = FakeSchema
        SchemaFlags = type("Flags", (), {"NONE": 0})

        @staticmethod
        def password_lookup_sync(schema, attrs, cancellable):
            looked_up["lookup"] = (schema, attrs, cancellable)
            return "from-keyring"

    class FakeGi:
        @staticmethod
        def require_version(namespace, version):
            looked_up["version"] = (namespace, version)

    def fake_import(name):
        return FakeGi if name == "gi" else FakeSecret

    monkeypatch.setattr(hardware.importlib, "import_module", fake_import)
    assert hardware.grd_stored_password() == "from-keyring"
    assert looked_up["version"] == ("Secret", "1")
    assert looked_up["schema"] == (hardware.GRD_SECRET_SCHEMA, {})
    assert looked_up["lookup"][1] == {}


def test_grd_stored_password_returns_none_when_the_keyring_is_locked(monkeypatch):
    """libsecret reports a locked keyring, or no Secret Service on the bus, as a
    GLib.Error; the documented answer for both is None, never a traceback."""

    class FakeGLib:
        class Error(Exception):
            pass

    class FakeSecret:
        Schema = type("S", (), {"new": staticmethod(lambda *a: None)})
        SchemaFlags = type("Flags", (), {"NONE": 0})

        @staticmethod
        def password_lookup_sync(*_args):
            raise FakeGLib.Error("Cannot autolaunch D-Bus without X11 $DISPLAY")

    fakes = {
        "gi": type("Gi", (), {"require_version": staticmethod(lambda *a: None)}),
        "gi.repository.Secret": FakeSecret,
        "gi.repository.GLib": FakeGLib,
    }
    monkeypatch.setattr(hardware.importlib, "import_module", fakes.__getitem__)
    assert hardware.grd_stored_password() is None


def test_grd_stored_password_returns_none_when_the_keyring_has_no_secret(monkeypatch):
    class FakeSecret:
        Schema = type("S", (), {"new": staticmethod(lambda *a: None)})
        SchemaFlags = type("Flags", (), {"NONE": 0})

        @staticmethod
        def password_lookup_sync(*_args):
            return None

    monkeypatch.setattr(
        hardware.importlib,
        "import_module",
        lambda name: (
            type("Gi", (), {"require_version": staticmethod(lambda *a: None)})
            if name == "gi"
            else FakeSecret
        ),
    )
    assert hardware.grd_stored_password() is None
