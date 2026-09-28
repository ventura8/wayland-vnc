"""The release's binary assets: one file per format and architecture, none lost.

The snap and the flatpak are built on an amd64 and an arm64 runner, and the release
job gathers every runner's files into one directory. Their names carried no
architecture and the download merged everything into one place, so one runner's
bundle replaced the other's in silence: v1.0.0 to v1.0.3 each shipped one .snap and
one .flatpak for two architectures. These tests run the release job's own assembly
step, taken from the workflow, and pin the per-architecture names.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"


def _assemble_step() -> str:
    """The `run:` script of the release job's "Assemble artifact set" step."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "name: Assemble artifact set" in line)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for line in lines[run + 1 :]:
        if line.strip().startswith("- name:"):
            break
        body.append(line)
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[indent:] for line in body)


def _assemble(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess:
    for relative, content in files.items():
        path = tmp_path / "downloaded" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", _assemble_step()],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_both_architectures_of_a_bundle_are_published(tmp_path):
    result = _assemble(
        tmp_path,
        {
            "release-pkg-snap-amd64/wayland-vnc_1.0.4_amd64.snap": "amd64",
            "release-pkg-snap-arm64/wayland-vnc_1.0.4_arm64.snap": "arm64",
            "release-pkg-flatpak-amd64/wayland-vnc-1.0.4-x86_64.flatpak": "x86_64",
            "release-pkg-flatpak-arm64/wayland-vnc-1.0.4-aarch64.flatpak": "aarch64",
        },
    )
    assert result.returncode == 0, result.stderr
    published = sorted(p.name for p in (tmp_path / "artifacts").iterdir())
    assert published == [
        "SHA256SUMS",
        "wayland-vnc-1.0.4-aarch64.flatpak",
        "wayland-vnc-1.0.4-x86_64.flatpak",
        "wayland-vnc_1.0.4_amd64.snap",
        "wayland-vnc_1.0.4_arm64.snap",
    ]
    sums = (tmp_path / "artifacts" / "SHA256SUMS").read_text(encoding="utf-8")
    assert sums.count("\n") == 4


def test_two_jobs_producing_the_same_name_fail_the_release(tmp_path):
    result = _assemble(
        tmp_path,
        {
            "release-pkg-snap-amd64/wayland-vnc_1.0.4.snap": "amd64",
            "release-pkg-snap-arm64/wayland-vnc_1.0.4.snap": "arm64",
        },
    )
    assert result.returncode == 1
    assert "Two build jobs produced wayland-vnc_1.0.4.snap" in result.stderr


def test_the_download_keeps_each_job_apart():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    download = workflow[workflow.index("name: Download release artifacts") :]
    download = download[: download.index("- name:", 1)]
    assert "merge-multiple" not in download


@pytest.mark.parametrize(
    ("function", "spelling"),
    [("build_snap", "deb"), ("build_flatpak", "appimage")],
)
def test_the_release_build_names_bundles_per_architecture(function, spelling):
    script = (REPO / "scripts" / "build_release_package.sh").read_text(encoding="utf-8")
    body = re.search(rf"^{function}\(\) \{{\n(.*?)^\}}", script, re.S | re.M).group(1)
    assert f"scripts/target-arch.sh {spelling}" in body


def test_the_portable_smoke_names_its_snap_per_architecture():
    smoke = (REPO / "scripts" / "run_portable_package_smoke.sh").read_text(encoding="utf-8")
    assert "snap=/out/wayland-vnc_${version}_$(dpkg --print-architecture).snap" in smoke
