"""packaging/appimage-pins.sh: the AppImage toolchain every AppImage build uses.

appimagetool embeds a runtime into each AppImage and, unless handed one, downloads it
from type2-runtime's mutable `continuous` release with no checksum. These tests pin
that both builds hand it the pinned runtime, and that a cached tool or runtime that
does not match its digest is refused and removed rather than used. (Downloading from
GitHub is exercised by the release build and the portable smoke, not here.)
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PINS = REPO / "packaging" / "appimage-pins.sh"


def _bash(script: str, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", f". {PINS}\n{script}"],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_each_supported_architecture_pins_both_the_tool_and_the_runtime(arch):
    result = _bash(f'appimage_pins {arch}; echo "$appimagetool_sha256 $appimage_runtime_sha256"')
    assert result.returncode == 0, result.stderr
    tool, runtime = result.stdout.split()
    for digest in (tool, runtime):
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert tool != runtime


def test_an_unpinned_architecture_is_refused():
    result = _bash("appimage_pins riscv64")
    assert result.returncode != 0
    assert "no pinned AppImage toolchain" in result.stderr


def test_the_runtime_comes_from_a_tagged_release_not_continuous():
    result = _bash('echo "$appimage_runtime_version $appimagetool_version"')
    assert result.returncode == 0, result.stderr
    assert "continuous" not in result.stdout


def test_a_cached_file_matching_its_digest_is_used_without_a_download(tmp_path):
    cached = tmp_path / "runtime-x86_64"
    cached.write_bytes(b"stand-in runtime")
    digest = hashlib.sha256(b"stand-in runtime").hexdigest()
    # An unreachable URL: the cache must be verified, never refetched.
    result = _bash(f"appimage_fetch https://invalid.invalid/runtime {digest} {cached}")
    assert result.returncode == 0, result.stderr
    assert cached.read_bytes() == b"stand-in runtime"


def test_a_cached_file_that_does_not_match_is_refused_and_removed(tmp_path):
    cached = tmp_path / "appimagetool-x86_64"
    cached.write_bytes(b"tampered")
    result = _bash(f"appimage_fetch https://invalid.invalid/tool {'0' * 64} {cached}")
    assert result.returncode != 0
    assert "failed its checksum" in result.stderr
    assert not cached.exists()


@pytest.mark.parametrize(
    "script", ["scripts/build_release_package.sh", "scripts/run_portable_package_smoke.sh"]
)
def test_every_appimage_build_hands_appimagetool_the_pinned_runtime(script):
    text = (REPO / script).read_text()
    assert "packaging/appimage-pins.sh" in text
    assert "appimage_toolchain" in text
    assert "--runtime-file" in text
    assert "releases/download/continuous" not in text
