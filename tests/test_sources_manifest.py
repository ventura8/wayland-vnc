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
