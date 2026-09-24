import json
import stat
from pathlib import Path

import pytest

from wayland_vnc import installer
from wayland_vnc.backends import get_backend
from wayland_vnc.installer import _safe_destination, install, package_files, uninstall


def test_install_and_uninstall(tmp_path):
    manifest = install(tmp_path, get_backend("wayvnc"))
    assert manifest["backend"] == "wayvnc"
    assert len(manifest["installed"]) == 2
    unit = tmp_path / "usr/lib/systemd/user/wayland-vnc.service"
    assert unit.is_file()
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    state = tmp_path / "var/lib/wayland-vnc/manifest.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    result = uninstall(tmp_path)
    assert len(result["removed"]) == 2
    assert not unit.exists()


def test_existing_file_is_backed_up_and_restored(tmp_path):
    unit = tmp_path / "usr/lib/systemd/user/wayland-vnc.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("original\n", encoding="utf-8")
    manifest = install(tmp_path, get_backend("grd"))
    assert manifest["backups"] == ["usr/lib/systemd/user/wayland-vnc.service"]
    assert "original" not in unit.read_text(encoding="utf-8")
    assert uninstall(tmp_path)["restored"] == ["usr/lib/systemd/user/wayland-vnc.service"]
    assert unit.read_text(encoding="utf-8") == "original\n"


def test_repeated_install_refuses_overwrite(tmp_path):
    install(tmp_path, get_backend("wayvnc"))
    backend = get_backend("wayvnc")
    with pytest.raises(FileExistsError):
        install(tmp_path, backend)


def test_changed_installed_file_blocks_uninstall(tmp_path):
    install(tmp_path, get_backend("wayvnc"))
    unit = tmp_path / "usr/lib/systemd/user/wayland-vnc.service"
    unit.write_text("local change\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="locally changed"):
        uninstall(tmp_path)
    assert unit.read_text(encoding="utf-8") == "local change\n"


def test_invalid_manifest_blocks_uninstall(tmp_path):
    install(tmp_path, get_backend("wayvnc"))
    state = tmp_path / "var/lib/wayland-vnc/manifest.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    state.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        uninstall(tmp_path)


def test_live_root_and_path_escape_rejected(tmp_path):
    with pytest.raises(ValueError, match="Live-root"):
        _safe_destination(Path("/"), "etc/test")
    with pytest.raises(ValueError, match="inside"):
        _safe_destination(tmp_path, "../escape")


def test_package_contains_no_credentials():
    for name in ("grd", "w0vncserver", "wayvnc"):
        files = package_files(get_backend(name))
        assert all(mode == 0o644 for _, mode in files.values())
        assert all(b"password=" not in contents for contents, _ in files.values())


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "usr").symlink_to(outside, target_is_directory=True)
    backend = get_backend("wayvnc")
    with pytest.raises(ValueError, match="escapes"):
        install(tmp_path, backend)
    assert not (outside / "lib/systemd/user/wayland-vnc.service").exists()


def test_interrupted_install_restores_backup(tmp_path, monkeypatch):
    unit = tmp_path / "usr/lib/systemd/user/wayland-vnc.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("original\n", encoding="utf-8")
    original = installer._atomic_write
    calls = 0

    def fail_second(destination, contents, mode):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original(destination, contents, mode)

    monkeypatch.setattr(installer, "_atomic_write", fail_second)
    backend = get_backend("grd")
    with pytest.raises(OSError, match="injected"):
        install(tmp_path, backend)
    assert unit.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "usr/share/wayland-vnc/backend.conf").exists()


def test_missing_backup_blocks_all_removal(tmp_path):
    unit = tmp_path / "usr/lib/systemd/user/wayland-vnc.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("original\n", encoding="utf-8")
    install(tmp_path, get_backend("grd"))
    backup = tmp_path / "var/lib/wayland-vnc/backup/usr/lib/systemd/user/wayland-vnc.service"
    backup.unlink()
    with pytest.raises(FileNotFoundError, match="Missing rollback"):
        uninstall(tmp_path)
    assert unit.exists()
    assert (tmp_path / "usr/share/wayland-vnc/backend.conf").exists()


def test_missing_installed_file_blocks_uninstall(tmp_path):
    install(tmp_path, get_backend("wayvnc"))
    marker = tmp_path / "usr/share/wayland-vnc/backend.conf"
    marker.unlink()
    with pytest.raises(RuntimeError, match="locally changed"):
        uninstall(tmp_path)


def test_temporary_file_cleaned_after_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "target"
    real_replace = installer.os.replace

    def fail_replace(source, destination):
        if destination == target:
            raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        installer._atomic_write(target, b"contents", 0o600)
    assert list(tmp_path.iterdir()) == []


def test_the_packaged_unit_carries_no_mount_sandbox_and_no_fence():
    """The unit every package installs. A mount-namespacing directive in a user unit
    is an unprivileged user namespace, which Ubuntu's AppArmor bars from reading other
    processes' fd tables (and none of the read-only mounts apply): serve on GNOME then
    cannot attribute port 5900 to the daemon and the unit restarts every 75 seconds
    for ever. IPAddressAllow is not applied by a user manager at all, so it must not
    be there to be mistaken for a fence."""
    unit = Path(__file__).resolve().parent.parent / "packaging/systemd/wayland-vnc.service"
    text = unit.read_text(encoding="utf-8")
    directives = [line for line in text.splitlines() if line and not line.startswith("#")]
    for banned in ("PrivateTmp", "ProtectSystem", "ProtectHome", "ReadWritePaths", "IPAddress"):
        assert not any(line.startswith(banned) for line in directives), banned
    assert "NoNewPrivileges=yes" in directives
