"""Helpers for the on-hardware validation run.

Qualification tooling, not part of the installed payload. The shell driver
(`scripts/run_hardware_validation.sh`) calls these so the fiddly parts -- RealVNC's
stored-password obfuscation, deciding whether a captured frame is a real desktop, and
the phase report -- are unit-tested in the repository rather than living only in shell.

Pillow is a qualification dependency; this module is deliberately not part of the
payload `packaging/stage-payload.sh` installs, so the product keeps no image stack.
"""

import importlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

# RealVNC obfuscates the password stored in a .vnc file with DES under this fixed key;
# it is obfuscation, not secrecy. This is the classic VNC key with the per-byte bit
# reversal DES applies, which is NOT the byte order TigerVNC's `vncpasswd` writes into
# a server-side password file -- pinning against that file produced a value RealVNC
# silently refused. The vector below comes from a .vnc that actually authenticated.
VNC_FIXED_KEY = "e84ad660c4721ae0"
GRD_SECRET_SCHEMA = "org.gnome.RemoteDesktop.VncCredentials"
PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
INCOMPLETE = "incomplete"


def _openssl(args: list[str], data: bytes) -> bytes:
    return subprocess.run(args, input=data, capture_output=True, check=True, timeout=15).stdout


def obfuscate_password(
    password: str, *, run: Callable[[list[str], bytes], bytes] = _openssl
) -> str:
    """Return the hex form RealVNC stores in a .vnc file's `Password=` field.

    The password is truncated or zero-padded to the 8 bytes DES takes. The legacy
    provider is requested explicitly because OpenSSL 3 retired single DES by default.
    """
    block = password.encode("utf-8")[:8].ljust(8, b"\0")
    encrypted = run(
        [
            "openssl",
            "enc",
            "-des-ecb",
            "-K",
            VNC_FIXED_KEY,
            "-nopad",
            "-provider",
            "legacy",
            "-provider",
            "default",
        ],
        block,
    )
    return encrypted[:8].hex()


def connection_file(host: str, password: str, *, username: str = "vnc", **kwargs) -> str:
    """A private RealVNC .vnc connection file body; the password is never plaintext."""
    obfuscated = obfuscate_password(password, **kwargs)
    return (
        "[Connection]\n"
        f"Host={host}\n"
        f"UserName={username}\n"
        f"Username={username}\n"
        f"Password={obfuscated}\n\n"
        "[Options]\n"
        "Encryption=Server\n"
        "WarnUnencrypted=0\n"
        "AutoReconnect=0\n"
        "EnableUdpRfb=0\n"
        "ProxyTcpRfb=0\n"
        "PasswordStoreOffer=0\n"
        "VerifyId=0\n"
    )


@dataclass(frozen=True)
class FrameEvidence:
    """What a captured viewer frame proves."""

    width: int
    height: int
    colours: int

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "colours": self.colours}


def frame_evidence(path: Path, *, minimum_colours: int = 40) -> FrameEvidence:
    """Fail closed unless the capture is a real desktop, not a blank or error screen.

    A genuine desktop has many distinct colours once downsampled; a black screen, a
    solid error page or a 'connecting' placeholder does not.
    """
    with Image.open(path) as image:
        frame = image.convert("RGB")
        width, height = frame.size
        colours = len(set(frame.resize((160, 100)).getdata()))
    if width < 640 or height < 400:
        raise ValueError(f"frame is too small to be a desktop: {width}x{height}")
    if colours <= minimum_colours:
        raise ValueError(f"frame is flat ({colours} distinct colours): no desktop arrived")
    return FrameEvidence(width, height, colours)


@dataclass
class Report:
    """Ordered phase outcomes for one hardware run, written as JSON evidence."""

    host: str
    session: str
    desktop: str
    viewer: str
    commit: str
    recorded_at: str
    phases: dict = field(default_factory=dict)

    def record(self, phase: str, status: str, detail: str) -> None:
        if status not in (PASSED, FAILED, BLOCKED):
            raise ValueError(f"unknown status: {status}")
        self.phases[phase] = {"status": status, "detail": detail}

    @property
    def ok(self) -> bool:
        """Blocked is not a pass, but it does not fail the run; failed does."""
        return not any(entry["status"] == FAILED for entry in self.phases.values())

    @property
    def result(self) -> str:
        """`passed` only when every phase passed; blocked makes the run incomplete.

        A blocked phase is a prerequisite that could not be met on this host, so it
        proves nothing either way. Reporting the whole run as `passed` would let
        blocked phases -- the viewer phases on a grd host, for instance -- read as
        evidence the product was validated there, which is exactly what they are not.
        A run with no phases at all is incomplete for the same reason.
        """
        if not self.ok:
            return FAILED
        if not self.phases:
            return INCOMPLETE
        if any(entry["status"] == BLOCKED for entry in self.phases.values()):
            return INCOMPLETE
        return PASSED

    def as_dict(self) -> dict:
        return {
            "kind": "hardware-validation",
            "schema_version": 1,
            "recorded_at": self.recorded_at,
            "host": self.host,
            "session": self.session,
            "desktop": self.desktop,
            "viewer": self.viewer,
            "commit": self.commit,
            "result": self.result,
            "phases": self.phases,
        }

    def write(self, path: Path) -> Path:
        # Not sorted: phases are recorded in the order the run executed them, which is
        # what makes the report readable when something fails part-way through.
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        path.chmod(0o600)
        return path


def grd_stored_password(schema_name: str = GRD_SECRET_SCHEMA) -> str | None:
    """Read GNOME Remote Desktop's effective VNC password from the login keyring.

    The hardware run needs the password the server will actually accept, which for the
    grd backend lives in the keyring rather than in our config. Returns None when the
    keyring is locked or holds no such secret, so the caller can record `blocked`
    instead of guessing.
    """
    gi = importlib.import_module("gi")
    gi.require_version("Secret", "1")
    secret = importlib.import_module("gi.repository.Secret")
    glib = importlib.import_module("gi.repository.GLib")
    schema = secret.Schema.new(schema_name, secret.SchemaFlags.NONE, {})
    try:
        return secret.password_lookup_sync(schema, {}, None)
    except glib.Error:
        # A locked keyring, or no Secret Service on the bus at all, comes back as a
        # GError rather than as None; either way the password is unknown, not a crash.
        return None
