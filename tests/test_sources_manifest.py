"""The source manifest declares the patch series prepare-source.py applies; the Docker
builds glob the same directories. A patch added to a directory but not to the
manifest is applied by one and silently skipped by the other."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_every_patch_on_disk_is_declared_in_the_manifest_in_order():
    manifest = json.loads((REPO / "sources.json").read_text(encoding="utf-8"))
    declared = {source["name"]: source["patches"] for source in manifest["sources"]}
    for name, patches in declared.items():
        on_disk = sorted(path.name for path in (REPO / "patches" / name).glob("*.patch"))
        assert patches == on_disk, f"{name}: manifest {patches} vs patches/{name} {on_disk}"
        assert patches == sorted(patches), f"{name}: the manifest is not in series order"


def _dockerfile_gnome_build_deps() -> set[str]:
    """The apt packages docker/Dockerfile.gnome's grd-build stage installs: the list
    proven to build the patched daemon on ubuntu:26.04 (which is what Launchpad's
    resolute sbuild is)."""
    text = (REPO / "docker" / "Dockerfile.gnome").read_text(encoding="utf-8")
    stage = text.split("AS grd-build", 1)[1].split("COPY", 1)[0]
    run = stage.split("apt-get install -y --no-install-recommends", 1)[1]
    run = run.split("&&", 1)[0]
    return {token for token in run.replace("\\", " ").split() if not token.startswith("-")}


def _grd_debian_build_depends() -> set[str]:
    text = (REPO / "packaging" / "grd" / "debian" / "control").read_text(encoding="utf-8")
    block = text.split("Build-Depends:", 1)[1].split("\n\n", 1)[0]
    return {
        line.strip().rstrip(",").split(" ")[0]
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("debhelper-compat")
    }


def test_grd_ppa_build_depends_cover_the_dockerfile_toolchain():
    """The wayland-vnc-grd source package must declare every build dependency the
    proven Dockerfile.gnome recipe uses, or Launchpad's offline sbuild fails where the
    container succeeded. The Dockerfile's ca-certificates/curl are for fetching, which
    the source package does not do (the tarballs are vendored); gcc/make come with
    build-essential, which dpkg-buildpackage requires implicitly."""
    fetch_only = {"ca-certificates", "curl"}
    implied_by_build_essential = {"gcc", "make"}
    needed = _dockerfile_gnome_build_deps() - fetch_only - implied_by_build_essential
    missing = needed - _grd_debian_build_depends()
    assert not missing, f"packaging/grd/debian/control lacks Build-Depends: {sorted(missing)}"


def test_grd_ppa_rules_vendor_the_pinned_upstream_versions():
    """debian/rules extracts vendored tarballs by name; those names must carry the
    versions sources.json pins, so a version bump cannot leave rules pointing at a
    tarball prepare-grd-ppa-source.sh no longer fetches."""
    manifest = json.loads((REPO / "sources.json").read_text(encoding="utf-8"))
    versions = {s["name"]: s["version"] for s in manifest["sources"]}
    rules = (REPO / "packaging" / "grd" / "debian" / "rules").read_text(encoding="utf-8")
    assert f"gnome-remote-desktop-{versions['gnome-remote-desktop']}.tar.xz" in rules
    assert f"LibVNCServer-{versions['libvncserver']}.tar.gz" in rules
    prepare = (REPO / "scripts" / "prepare-grd-ppa-source.sh").read_text(encoding="utf-8")
    assert f"gnome-remote-desktop-{versions['gnome-remote-desktop']}.tar.xz" in prepare
    assert f"LibVNCServer-{versions['libvncserver']}.tar.gz" in prepare
