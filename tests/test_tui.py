"""Launcher-menu tests driven by a scripted fake screen; no real terminal or systemctl."""

import subprocess

import pytest

from wayland_vnc import runtime, tui


class FakeScreen(tui.Screen):
    """Feed a fixed key sequence and record every rendered menu and status line."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.renders = []
        self.messages = []

    def render(self, title, items, selected, status):
        self.renders.append([item.key for item in items])

    def read_key(self):
        return self.keys.pop(0)

    def message(self, text):
        self.messages.append(text)


def fake_run(result_map=None):
    result_map = result_map or {}

    def run(argv):
        key = argv[2] if len(argv) > 2 else ""
        out = result_map.get(key, f"{key} ok")
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


def installation(tmp_path, which=lambda name: "/usr/bin/systemctl", run=None):
    return tui.Installation(config_dir=tmp_path, which=which, run=run or fake_run())


def store_credentials(tmp_path):
    (tmp_path / runtime.CREDENTIALS_NAME).write_text(
        "username=vnc\npassword=hunter2x\n", encoding="utf-8"
    )


def test_menu_before_install_has_no_uninstall(tmp_path):
    items = tui.build_menu(installation(tmp_path), read_secret=lambda _p: "hunter2x")
    keys = [item.key for item in items]
    assert keys == ["i", "d", "q"]
    assert "Install and start" in items[0].label


def test_menu_after_install_offers_uninstall(tmp_path):
    store_credentials(tmp_path)
    items = tui.build_menu(installation(tmp_path), read_secret=lambda _p: "hunter2x")
    assert [item.key for item in items] == ["i", "d", "u"] + ["q"]
    assert "Uninstall" in items[-2].label


def test_install_action_provisions_and_enables_the_user_service(tmp_path):
    calls = []

    def record(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "enabled", "")

    install = installation(tmp_path, run=record)
    menu = tui.Menu(install, read_secret=lambda _p: "hunter2x")
    screen = FakeScreen(keys=["i", "q"])
    transcript = menu.run(screen)
    assert (tmp_path / runtime.CREDENTIALS_NAME).exists()
    assert (tmp_path / runtime.CONFIG_NAME).exists()
    # unmask precedes enable: a previous uninstall masks the unit for this user.
    assert ["systemctl", "--user", "unmask", tui.SERVICE] in calls
    assert calls[-1][:4] == ["systemctl", "--user", "enable", "--now"]
    assert "enabled" in transcript[0]


def test_diagnostics_action_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "collect", lambda: {"session_type": "wayland"})
    monkeypatch.setattr(
        tui, "report", lambda _caps: {"backend_candidate": None, "reason": "none detected"}
    )
    menu = tui.Menu(installation(tmp_path), read_secret=lambda _p: "hunter2x")
    transcript = menu.run(FakeScreen(keys=["d", "q"]))
    assert "Backend candidate: none" in transcript[0]
    assert "unqualified" in transcript[0]


def test_uninstall_action_stops_service_and_removes_every_stored_secret(tmp_path):
    store_credentials(tmp_path)
    # Provisioning writes the same password a second time, into wayvnc.conf; an
    # uninstall that removes only the credentials file leaves the plaintext behind.
    config = tmp_path / runtime.CONFIG_NAME
    config.write_text("address=0.0.0.0\nport=5900\npassword=hunter2x\n", encoding="utf-8")
    calls = []

    def record(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "disabled", "")

    install = installation(tmp_path, run=record)
    menu = tui.Menu(install, read_secret=lambda _p: "hunter2x")
    transcript = menu.run(FakeScreen(keys=["u", "q"]))
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists()
    assert not config.exists(), "the provisioned config still holds the password"
    assert calls[0][:4] == ["systemctl", "--user", "disable", "--now"]
    assert "Removed the stored viewer credential" in transcript[0]


def test_arrow_navigation_and_enter_select_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "collect", lambda: {})
    monkeypatch.setattr(tui, "report", lambda _c: {"backend_candidate": "wayvnc", "reason": "ok"})
    menu = tui.Menu(installation(tmp_path), read_secret=lambda _p: "hunter2x")
    # down to diagnostics (index 1), Enter to run it, then quit.
    transcript = menu.run(FakeScreen(keys=["down", "enter", "up", "q"]))
    assert "Backend candidate: wayvnc" in transcript[0]


def test_eof_at_the_password_prompt_cancels_instead_of_crashing(tmp_path):
    """Ctrl-D at getpass raises EOFError; the launcher reports it and stays open."""

    def eof(_prompt):
        raise EOFError

    screen = FakeScreen(["i", "q"])
    transcript = tui.Menu(installation(tmp_path), read_secret=eof).run(screen)
    assert transcript == ["Cancelled."]
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists(), "nothing was stored"


def test_unknown_key_is_ignored(tmp_path):
    menu = tui.Menu(installation(tmp_path), read_secret=lambda _p: "hunter2x")
    transcript = menu.run(FakeScreen(keys=["z", "q"]))
    assert transcript == []


def test_systemctl_absent_is_reported_not_crashed(tmp_path):
    install = installation(tmp_path, which=lambda _n: None)
    assert "systemctl is not available" in install.systemctl("status")


def test_malformed_credentials_still_count_as_installed(tmp_path):
    (tmp_path / runtime.CREDENTIALS_NAME).write_text("garbage\n", encoding="utf-8")
    assert installation(tmp_path).is_provisioned() is True


def test_an_unreadable_credentials_file_still_counts_as_installed(tmp_path):
    # A directory where the file should be: exists, and reading it raises OSError.
    (tmp_path / runtime.CREDENTIALS_NAME).mkdir()
    assert installation(tmp_path).is_provisioned() is True


def test_screen_base_is_abstract():
    screen = tui.Screen()
    for call in (
        lambda: screen.render("t", [], 0, "s"),
        screen.read_key,
        lambda: screen.message("x"),
    ):
        with pytest.raises(NotImplementedError):
            call()


def test_uninstall_masks_the_unit_so_a_global_enable_cannot_revive_it(tmp_path):
    """The package enables the unit globally; a per-user disable alone is undone at the
    next login. Masking makes the uninstall durable, and install lifts the mask."""
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    install = tui.Installation(config_dir=tmp_path, which=lambda name: f"/usr/bin/{name}", run=run)
    (tmp_path / runtime.CREDENTIALS_NAME).write_text("username=vnc\npassword=hunter2x\n")
    message = tui._uninstall(install)
    assert ["systemctl", "--user", "mask", tui.SERVICE] in calls
    assert "Masked" in message and not (tmp_path / runtime.CREDENTIALS_NAME).exists()
    calls.clear()
    tui._install(install, lambda _p: "hunter2x")
    assert calls.index(["systemctl", "--user", "unmask", tui.SERVICE]) < calls.index(
        ["systemctl", "--user", "enable", "--now", tui.SERVICE]
    )
