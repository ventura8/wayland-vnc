"""The autouse guards in conftest must catch the DEFAULT runners, not only explicit
calls to the module helpers: a Host or Actions built without a runner once slipped
past them because the defaults were bound when the signatures were defined."""

import os
import shutil

import pytest

from wayland_vnc import runtime, settings


def test_a_host_built_without_a_runner_is_intercepted():
    host = runtime.Host(shutil.which, os.execv)
    with pytest.raises(AssertionError, match="on the real system"):
        host.run(["true"])


def test_actions_built_without_a_runner_are_intercepted(tmp_path):
    with pytest.raises(AssertionError, match="on the real system"):
        settings.Actions(tmp_path).status()


def test_the_real_keyring_is_never_read(tmp_path):
    with pytest.raises(AssertionError, match="login keyring"):
        runtime.grd_password()
    gnome = {
        "session_type": "wayland",
        "gnome_remote_desktop": True,
        "gnome_screencast": True,
        "kwin": False,
        "remote_desktop_portal": False,
        "screencast_portal": False,
        "interfaces": [],
    }
    with pytest.raises(AssertionError, match="login keyring"):
        runtime.sync_backend_password(
            runtime.Credentials("vnc", "hunter2x"),
            which=lambda name: f"/usr/bin/{name}",
            run=lambda *_a: None,
            capabilities=gnome,
        )


def test_the_module_helpers_default_to_the_guarded_runner():
    with pytest.raises(AssertionError, match="on the real system"):
        settings.service_state()
    with pytest.raises(AssertionError, match="on the real system"):
        settings.local_addresses()
    with pytest.raises(AssertionError, match="on the real system"):
        settings.connect_info(settings.ServerConfig(False, None, None, False))
