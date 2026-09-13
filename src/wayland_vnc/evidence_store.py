"""Move qualification evidence from the trusted runner into the store the release gate
reads.

The runner (`scripts/qualify-desktop.py`) writes one record per run under
`<evidence-root>/records/` and the files those records reference under
`<evidence-root>/<target>/<viewer>/<run-id>/`. The release gate cannot see that
directory: it lives on the runner, and the tag is built on a hosted machine. The
store is a private git repository laid out per commit exactly like an evidence root,
so `check-release.py --records <store>/<sha>/records.json --evidence-root
<store>/<sha>` is the same call the local gate makes:

    <sha>/records.json          every run record for that commit, as one list
    <sha>/records/<run>.json    the records as the runner wrote them
    <sha>/<target>/<viewer>/<run-id>/...   the referenced artifacts

Publishing verifies every artifact against the hash in its record before copying it,
so a record whose evidence has been altered on the runner never reaches the store;
the gate verifies the hashes again on its side.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from wayland_vnc.qualification import validate_record


@dataclass
class Publication:
    """What one publish run selected and copied."""

    commit: str
    records: list[dict] = field(default_factory=list)
    copied: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def pairs(self) -> set[tuple[str, str]]:
        return {(record["target"], record["viewer"]) for record in self.records}


def _load_records(evidence_root: Path) -> list[tuple[Path, dict]]:
    records_dir = evidence_root / "records"
    loaded = []
    for path in sorted(records_dir.glob("*.json")) if records_dir.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"{path}: unreadable record ({error})") from error
        if isinstance(data, dict):
            loaded.append((path, data))
    return loaded


def _verified_source(entry: dict, evidence_root: Path) -> tuple[Path, Path]:
    """One referenced artifact as (relative path, real file), refusing a path that
    escapes the root or a file whose bytes no longer match the record. A mismatch is
    corruption of the evidence, not an incomplete run, so it is never skipped over."""
    relative = Path(str(entry.get("path", "")))
    source = (evidence_root / relative).resolve()
    if relative.is_absolute() or not source.is_relative_to(evidence_root.resolve()):
        raise ValueError(f"artifact path escapes the evidence root: {relative}")
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"artifact is not a regular file: {relative}")
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != entry.get("sha256"):
        raise ValueError(f"artifact does not match its record: {relative}")
    return relative, source


def publish(
    evidence_root: Path,
    store: Path,
    commit: str,
    *,
    include_incomplete: bool = False,
) -> Publication:
    """Copy this commit's records and their artifacts into `<store>/<commit>/`.

    By default only records that validate for `commit` are published: the store is
    the set of proofs the gate will weigh. `include_incomplete` also carries records
    that do not (yet) validate, so partial progress on a target is visible next to
    the complete pairs; the gate rejects them exactly as it would locally.
    """
    if not commit or "/" in commit or commit in (".", ".."):
        raise ValueError("a full commit hash is required")
    evidence_root = evidence_root.resolve()
    publication = Publication(commit=commit)
    destination_root = store / commit
    for path, record in _load_records(evidence_root):
        if record.get("commit") != commit:
            publication.skipped.append(f"{path.name}: belongs to commit {record.get('commit')}")
            continue
        sources = [
            _verified_source(entry, evidence_root) for entry in record.get("artifacts") or []
        ]
        errors = validate_record(record, commit, evidence_root)
        if errors and not include_incomplete:
            publication.skipped.append(f"{path.name}: {'; '.join(errors)}")
            continue
        for relative, source in sources:
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            publication.copied.append(destination)
        records_dir = destination_root / "records"
        records_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, records_dir / path.name)
        publication.records.append(record)
    if publication.records:
        (destination_root / "records.json").write_text(
            json.dumps(publication.records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return publication
