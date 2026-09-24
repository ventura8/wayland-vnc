"""scripts/ensure-venv.sh owns the project venv: it must reuse one that is already
current without touching pip, and it must announce the bin directory callers put on
PATH. (Building a venv from PyPI is exercised by every gate run, not here.)"""

import hashlib
import subprocess
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ensure-venv.sh"


def _current_venv(root: Path) -> Path:
    target = root / "venv"
    # --without-pip: any attempt to install would fail loudly, which is the point.
    venv.EnvBuilder(system_site_packages=True, with_pip=False).create(target)
    digest = hashlib.sha256((REPO / "requirements-dev.txt").read_bytes()).hexdigest()
    (target / ".requirements-dev.sha256").write_text(digest + "\n", encoding="utf-8")
    return target


def test_a_current_venv_is_reused_and_its_bin_dir_printed(tmp_path):
    target = _current_venv(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "WAYLAND_VNC_VENV": str(target)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target / "bin")
    assert result.stderr == ""


def test_a_venv_whose_interpreter_is_gone_is_rebuilt_rather_than_trusted(tmp_path):
    target = _current_venv(tmp_path)
    python = target / "bin" / "python"
    python.unlink()
    python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python.chmod(0o755)
    # No network in this test: the rebuild is expected to get as far as creating a
    # fresh interpreter and then fail on pip, which must be a loud failure, never a
    # silent reuse of the broken environment.
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "WAYLAND_VNC_VENV": str(target), "PIP_NO_INDEX": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rebuilding" in result.stderr
    assert result.returncode != 0, "pip cannot succeed without an index"
    stamp = target / ".requirements-dev.sha256"
    assert not stamp.exists(), "a failed install must not be stamped"
    rebuilt = target / "bin" / "python"
    assert rebuilt.exists()
    assert "exit 1" not in rebuilt.read_bytes()[:64].decode("latin-1")


def test_the_script_is_wired_into_every_host_side_entry_point():
    """A script that runs Python on the host and is invoked from the host must put
    the venv first on PATH; the two that run inside containers inherit it instead."""
    wired = {
        path.name
        for path in (REPO / "scripts").glob("*.sh")
        if "ensure-venv.sh" in path.read_text(encoding="utf-8") and path.name != "ensure-venv.sh"
    }
    expected = {
        "build-and-test.sh",
        "run_hardware_validation.sh",
        "qualify-all.sh",
        "fixture-smoke.sh",
        "android-stage.sh",
        "android-provision-viewer.sh",
        "attended-input.sh",
        "realvnc-connection.sh",
        "realvnc-desktop-test.sh",
        "reproduce-upstream-libvncserver.sh",
        "run_settings_app_e2e.sh",
        "run_service_activation_smoke.sh",
        "run_portable_package_smoke.sh",
        "run_deb_package_smoke.sh",
        "run_rpm_package_smoke.sh",
        "run_ppa_source_smoke.sh",
        "run-sonar-scan.sh",
        "test-units.sh",
        "build_release_package.sh",
        "prepare-grd-ppa-source.sh",
    }
    assert wired == expected
    for inside_containers in ("build-translations.sh", "package_smoke_scenarios.sh"):
        text = (REPO / "scripts" / inside_containers).read_text(encoding="utf-8")
        assert "ensure-venv" not in text
