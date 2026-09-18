"""Transactional staging installer with explicit backup and rollback records."""

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from wayland_vnc.backends import Backend

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class InstalledFile:
    path: str
    sha256: str
    mode: int


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_files(backend: Backend) -> dict[str, tuple[bytes, int]]:
    """Return project-owned files only; credentials and backend binaries are external."""
    marker = (f"backend={backend.name}\nqualified=false\ncredentials=not-installed\n").encode()
    return {
        "usr/lib/systemd/user/wayland-vnc.service": (backend.service_unit().encode(), 0o644),
        "usr/share/wayland-vnc/backend.conf": (marker, 0o644),
    }


def _safe_destination(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    if resolved_root == Path("/"):
        raise ValueError("Live-root installation is disabled until release qualification")
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("Install paths must remain inside the staging root")
    destination = (resolved_root / requested).resolve()
    if not destination.is_relative_to(resolved_root):
        raise ValueError("Install path escapes the staging root")
    return destination


def _atomic_write(destination: Path, contents: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".wayland-vnc-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(root: Path, backend: Backend) -> dict:
    """Install atomically into an explicit non-root filesystem tree."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_dir = _safe_destination(root, "var/lib/wayland-vnc")
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / "manifest.json"
    if manifest_path.exists():
        raise FileExistsError("Installation already exists; uninstall it first")
    installed: list[dict] = []
    backups: list[str] = []
    try:
        for relative, (contents, mode) in package_files(backend).items():
            destination = _safe_destination(root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                backup = _safe_destination(root, f"var/lib/wayland-vnc/backup/{relative}")
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                backups.append(relative)
            _atomic_write(destination, contents, mode)
            installed.append(asdict(InstalledFile(relative, _digest(contents), mode)))
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "backend": backend.name,
            "installed": installed,
            "backups": backups,
        }
        _atomic_write(
            manifest_path,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            0o600,
        )
        return manifest
    except Exception:
        _rollback_partial(root, installed, backups)
        raise


def _rollback_partial(root: Path, installed: list[dict], backups: list[str]) -> None:
    for item in reversed(installed):
        destination = _safe_destination(root, item["path"])
        if destination.exists() or destination.is_symlink():
            destination.unlink()
    for relative in reversed(backups):
        backup = _safe_destination(root, f"var/lib/wayland-vnc/backup/{relative}")
        destination = _safe_destination(root, relative)
        if backup.exists() or backup.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, destination)


def _require_manifest_shape(manifest: dict) -> None:
    """Refuse a manifest whose records are missing or malformed.

    Everything past this point removes and restores files, so a missing key must be a
    stated refusal rather than a KeyError traceback out of the CLI.
    """
    installed = manifest.get("installed")
    backups = manifest.get("backups")
    if not isinstance(installed, list) or not isinstance(backups, list):
        raise ValueError("Install manifest is missing its file records; refusing to remove")
    for item in installed:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("path", "sha256")
        ):
            raise ValueError("Install manifest has a malformed file record; refusing to remove")
    if not all(isinstance(relative, str) for relative in backups):
        raise ValueError("Install manifest has a malformed backup record; refusing to remove")


def uninstall(root: Path) -> dict:
    """Remove unchanged project files and restore exact pre-install backups."""
    root = root.resolve()
    manifest_path = _safe_destination(root, "var/lib/wayland-vnc/manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_VERSION:
        raise ValueError("Unsupported install manifest; refusing destructive changes")
    _require_manifest_shape(manifest)
    changed = []
    for item in manifest["installed"]:
        destination = _safe_destination(root, item["path"])
        if not destination.is_file() or _digest(destination.read_bytes()) != item["sha256"]:
            changed.append(item["path"])
    if changed:
        raise RuntimeError("Refusing to remove locally changed files: " + ", ".join(changed))
    missing_backups = []
    for relative in manifest["backups"]:
        backup = _safe_destination(root, f"var/lib/wayland-vnc/backup/{relative}")
        if not backup.exists() and not backup.is_symlink():
            missing_backups.append(relative)
    if missing_backups:
        raise FileNotFoundError("Missing rollback backups: " + ", ".join(missing_backups))
    removed = []
    for item in manifest["installed"]:
        destination = _safe_destination(root, item["path"])
        destination.unlink()
        removed.append(item["path"])
    restored = []
    for relative in manifest["backups"]:
        backup = _safe_destination(root, f"var/lib/wayland-vnc/backup/{relative}")
        destination = _safe_destination(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, destination)
        restored.append(relative)
    manifest_path.unlink()
    shutil.rmtree(manifest_path.parent / "backup", ignore_errors=True)
    return {"removed": removed, "restored": restored}
