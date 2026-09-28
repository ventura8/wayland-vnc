"""scripts/android-emulator.sh: the isolated Android lab in a container.

Two defects made the containerised emulator unusable for the qualification runner:
the emulator wrote its console token into the container's own home, so the host's
`adb emu` (gestures, rotation, the readiness check) was refused every time; and a
lab stopped from outside left the AVD's lock files behind, so the next boot refused
with "Running multiple emulators with the same AVD". `docker`, `pgrep`, `stat` and
the SDK tools are fakes here, and the lab root and HOME are scratch directories.
"""

import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "android-emulator.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$RECORD/docker.calls"
if [[ "$1 $2" == "container inspect" ]]; then
  if [[ "$3" == "-f" ]]; then
    # The running-state check: "true" only when the test says the lab runs.
    [[ -n "${LAB_RUNNING:-}" ]] && echo true && exit 0
    exit 1
  fi
  # Existence, as the start check and the stop's wait loop ask it: gone by then.
  exit 1
fi
"""
# Matches the pattern against the command lines the test says are running.
FAKE_PGREP = """#!/usr/bin/env bash
grep -qE -- "$3" <<<"${FAKE_PROCS:-}"
"""
FAKE_STAT = """#!/usr/bin/env bash
echo 993
"""
FAKE_ADB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$RECORD/adb.calls"
"""


@pytest.fixture
def lab(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sdk = tmp_path / "sdk"
    (sdk / "emulator").mkdir(parents=True)
    (sdk / "platform-tools").mkdir()
    tools = {
        bin_dir / "docker": FAKE_DOCKER,
        bin_dir / "pgrep": FAKE_PGREP,
        bin_dir / "stat": FAKE_STAT,
        sdk / "emulator" / "emulator": "#!/usr/bin/env bash\n",
        sdk / "platform-tools" / "adb": FAKE_ADB,
    }
    for path, body in tools.items():
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    root = tmp_path / "android"
    avd = root / "avd" / "wayland-vnc-api36.avd"
    avd.mkdir(parents=True)
    user = root / "user"
    user.mkdir()
    (user / "adbkey").write_text("key", encoding="utf-8")
    (user / "adbkey.pub").write_text("pub", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    return {"bin": bin_dir, "sdk": sdk, "root": root, "avd": avd, "home": home, "record": record}


def _run(lab, *args, **env):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={
            "PATH": f"{lab['bin']}:/usr/bin:/bin",
            "HOME": str(lab["home"]),
            "RECORD": str(lab["record"]),
            "WAYLAND_VNC_ANDROID_ROOT": str(lab["root"]),
            "ANDROID_SDK_ROOT": str(lab["sdk"]),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _docker_run(lab) -> str:
    calls = (lab["record"] / "docker.calls").read_text(encoding="utf-8").splitlines()
    return next(call for call in calls if call.startswith("run "))


def _stale_locks(lab):
    (lab["avd"] / "multiinstance.lock").write_text("", encoding="utf-8")
    (lab["avd"] / "hardware-qemu.ini.lock").mkdir()
    (lab["avd"] / "snapshot.lock.tmp-abc").write_text("", encoding="utf-8")


def test_stale_locks_are_cleared_when_no_emulator_runs_the_avd(lab):
    _stale_locks(lab)
    result = _run(lab, "start", str(lab["sdk"]))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in lab["avd"].iterdir()) == []


@pytest.mark.parametrize(
    "command",
    [
        "qemu-system-x86_64 -avd wayland-vnc-api36 -no-window",
        "emulator @wayland-vnc-api36 -no-snapshot",
    ],
)
def test_a_running_emulator_keeps_its_locks_and_the_start_is_refused(lab, command):
    _stale_locks(lab)
    result = _run(lab, "start", str(lab["sdk"]), FAKE_PROCS=command)
    assert result.returncode == 2
    assert "already running wayland-vnc-api36" in result.stderr
    assert (lab["avd"] / "hardware-qemu.ini.lock").exists()
    assert not (lab["record"] / "docker.calls").read_text(encoding="utf-8").count("run ")


def test_another_avd_running_does_not_count_as_the_lab(lab):
    _stale_locks(lab)
    result = _run(lab, "start", str(lab["sdk"]), FAKE_PROCS="emulator @wayland-vnc-api36-old")
    assert result.returncode == 0, result.stderr
    assert not (lab["avd"] / "hardware-qemu.ini.lock").exists()


def test_the_container_uses_the_hosts_console_token(lab):
    token = lab["home"] / ".emulator_console_auth_token"
    token.write_text("hosttoken123", encoding="utf-8")
    _run(lab, "start", str(lab["sdk"]))
    run = _docker_run(lab)
    assert f"-v {token}:/lab-home/.emulator_console_auth_token:ro" in run
    assert "-e HOME=/lab-home" in run
    assert "--tmpfs /lab-home:" in run
    assert token.read_text(encoding="utf-8") == "hosttoken123"


def test_a_missing_token_is_created_private_before_the_container_starts(lab):
    _run(lab, "start", str(lab["sdk"]))
    token = lab["home"] / ".emulator_console_auth_token"
    assert token.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_stop_asks_the_emulator_to_quit_before_any_docker_stop(lab):
    result = _run(lab, "stop", LAB_RUNNING="1")
    assert result.returncode == 0, result.stderr
    adb = (lab["record"] / "adb.calls").read_text(encoding="utf-8")
    assert "-s emulator-5554 emu kill" in adb
    docker = (lab["record"] / "docker.calls").read_text(encoding="utf-8")
    assert "stop " not in docker


def test_stop_leaves_an_emulator_alone_when_the_lab_container_is_not_running(lab):
    result = _run(lab, "stop")
    assert result.returncode == 0, result.stderr
    assert "nothing to stop" in result.stdout
    assert not (lab["record"] / "adb.calls").exists()
    assert "stop " not in (lab["record"] / "docker.calls").read_text(encoding="utf-8")
