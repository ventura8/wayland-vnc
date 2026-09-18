"""Shared fixtures.

GTK needs a display to construct widgets, so the settings-app tests run against a
private Xvfb server rather than the developer's session; nothing is ever shown.
"""

import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from wayland_vnc import runtime, settings

# The genuine subprocess helpers, kept for the few tests that exercise the helpers
# themselves (with subprocess.run patched); every other test gets the refusing guard.
# grd_password is in the same class: it reads the developer's login keyring through
# libsecret, not through a runner, so the runner guard never sees it.
REAL_RUNNERS = SimpleNamespace(
    run=runtime._run,
    systemctl=settings._systemctl,
    command=settings._command,
    grd_password=runtime.grd_password,
)


@pytest.fixture(name="real_runners")
def real_runners_fixture():
    return REAL_RUNNERS


@pytest.fixture(name="display", scope="session")
def display_fixture():
    """A private X server so GTK can build widgets headlessly."""
    binary = shutil.which("Xvfb")
    if binary is None:
        pytest.fail("Xvfb is required to construct GTK widgets headlessly", pytrace=False)
    for number in range(99, 110):
        server = subprocess.Popen(
            [binary, f":{number}", "-screen", "0", "800x600x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        if server.poll() is None:
            os.environ["DISPLAY"] = f":{number}"
            os.environ["GDK_BACKEND"] = "x11"
            yield f":{number}"
            server.terminate()
            server.wait(timeout=10)
            return
    pytest.fail("could not start an Xvfb server", pytrace=False)


@pytest.fixture(autouse=True)
def _private_config_home(monkeypatch, tmp_path_factory):
    """serve's GNOME path writes systemd user drop-ins under XDG_CONFIG_HOME (restart
    tolerance, listen address). The developer's own configuration is never the target."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config-home")))


@pytest.fixture(autouse=True)
def _never_exec_for_real(monkeypatch):
    """`serve` ends in os.execv, which would replace the test process with a real
    server. Refuse by default; tests that exercise the exec boundary inject their own
    exec_fn or patch os.execv explicitly, which overrides this."""

    def refuse(path, _argv):
        raise AssertionError(f"test reached a real exec of {path}; inject exec_fn instead")

    monkeypatch.setattr(os, "execv", refuse)


@pytest.fixture(autouse=True)
def _never_touch_the_real_desktop(monkeypatch):
    """Block the subprocess helpers that reach the developer's own session.

    A settings test once called the real `grdctl` and rewrote the maintainer's GNOME
    Remote Desktop password, restarting their daemon. Tests inject a fake Host or
    runner; anything reaching these helpers is a bug, so fail loudly instead.
    """

    def refuse(args, *_rest, **_kw):
        raise AssertionError(
            f"test tried to run {args!r} on the real system; inject a fake host/runner"
        )

    # The helpers below resolve these names at call time (never as defaults bound
    # when their signatures were defined), so a Host or Actions built without an
    # explicit runner lands here too; tests/test_conftest_guards.py proves it.
    monkeypatch.setattr(runtime, "_run", refuse)
    monkeypatch.setattr(settings, "_systemctl", refuse)
    monkeypatch.setattr(settings, "_command", refuse)

    def refuse_keyring(**_kw):
        raise AssertionError(
            "test tried to read the real GNOME Remote Desktop password from the login "
            "keyring; patch runtime.grd_password"
        )

    # Two credential tests passed for months only because an unguarded run had once
    # written their fixture password into the maintainer's keyring; the day the real
    # password changed they tried to run grdctl. The keyring is not test input.
    monkeypatch.setattr(runtime, "grd_password", refuse_keyring)
