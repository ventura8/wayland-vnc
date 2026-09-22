"""Command interface: read-only diagnostics, staging, and the installed serving path."""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from wayland_vnc import runtime
from wayland_vnc.backends import BACKENDS, get_backend
from wayland_vnc.installer import install, uninstall
from wayland_vnc.probe import collect, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status"):
        subcommands.add_parser(name).add_argument("--json", action="store_true")
    setup = subcommands.add_parser("setup", help="Stage package files in a non-root tree")
    setup.add_argument("--json", action="store_true")
    setup.add_argument("--staging-root", type=Path, required=True)
    setup.add_argument("--backend", choices=BACKENDS, required=True)
    remove = subcommands.add_parser("uninstall", help="Undo a staging installation")
    remove.add_argument("--json", action="store_true")
    remove.add_argument("--staging-root", type=Path, required=True)
    for name in ("start", "stop"):
        subcommands.add_parser(name).add_argument("--json", action="store_true")
    password = subcommands.add_parser("set-password", help="Store the VNC viewer credential")
    password.add_argument("--json", action="store_true")
    password.add_argument("--username", default="vnc")
    password.add_argument(
        "--stdin",
        action="store_true",
        help="read the password (then its confirmation) as two lines from stdin",
    )
    prov = subcommands.add_parser("provision", help="Write the WayVNC config and key")
    prov.add_argument("--json", action="store_true")
    prov.add_argument("--address", default=runtime.DEFAULT_ADDRESS)
    prov.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    serve = subcommands.add_parser("serve", help="Run the Wayland VNC server (systemd ExecStart)")
    serve.add_argument("--json", action="store_true")
    return parser


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if "backend_candidate" in result:
        print(f"Backend candidate: {result['backend_candidate'] or 'none'}")
        print(result["reason"])
        print("Qualification: unqualified. No desktop has passed the release suite yet.")
    for key in ("message", "error"):
        if key in result:
            print(result[key])


def _diagnostic(args: argparse.Namespace) -> int:
    result = report(collect())
    _emit(result, args.json)
    return 0 if result["backend_candidate"] else 2


def _stdin_reader() -> Callable[[str], str]:
    """A read_secret that returns successive stdin lines; deterministic for automation."""

    def reader(_prompt: str) -> str:
        line = sys.stdin.readline()
        if not line:
            raise ValueError("stdin ended before the password was fully entered")
        return line.rstrip("\n")

    return reader


def _serving(args: argparse.Namespace) -> dict:
    directory = runtime.config_dir()
    if args.command == "serve":
        runtime.serve(host=runtime.Host(shutil.which, os.execv))
        return {"schema_version": 1, "message": "started"}  # pragma: no cover - serve execs
    if args.command == "set-password":
        read_secret = _stdin_reader() if args.stdin else getpass.getpass

        def checked(prompt: str) -> str:
            # Refuse a password the backend cannot accept BEFORE anything is written.
            secret = read_secret(prompt)
            runtime.check_password_for_backend(secret)
            return secret

        path = runtime.set_password(directory, read_secret=checked, username=args.username)
        synced = runtime.sync_backend_password(
            runtime.read_credentials(directory), which=shutil.which
        )
        if synced == "grd":
            note = " and updated GNOME Remote Desktop"
        else:
            # WayVNC reads the password from the config file, not from the credentials
            # file, so without this the new password is stored and reported as set
            # while the running server keeps accepting the old one.
            note = ""
            if runtime.refresh_config(directory) is not None:
                note = " and updated the WayVNC configuration"
                if runtime.restart_service(which=shutil.which):
                    note += "; the running server was restarted to apply it"
        return {"schema_version": 1, "message": f"Stored viewer credential at {path}{note}"}
    if args.command == "provision":
        path = runtime.provision(
            directory,
            address=args.address,
            port=args.port,
            generate_key=runtime.default_key_generator,
        )
        return {"schema_version": 1, "message": f"Wrote WayVNC config at {path}"}
    verb = "start" if args.command == "start" else "stop"
    return {
        "schema_version": 1,
        "message": (
            "Manage the installed service with your session manager: "
            f"systemctl --user {verb} wayland-vnc.service"
        ),
    }


def _staging(args: argparse.Namespace) -> dict:
    if args.command == "setup":
        manifest = install(args.staging_root, get_backend(args.backend))
        return {
            "schema_version": 1,
            "message": "Package files staged; no host service or credentials were changed.",
            "manifest": manifest,
        }
    changes = uninstall(args.staging_root)
    return {
        "schema_version": 1,
        "message": "Staged package files removed and backups restored.",
        "changes": changes,
    }


SERVING = frozenset({"serve", "set-password", "provision", "start", "stop"})


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in ("doctor", "status"):
        return _diagnostic(args)
    handler = _serving if args.command in SERVING else _staging
    try:
        result = handler(args)
    except (
        OSError,
        # ValueError also covers json.JSONDecodeError, which derives from it.
        ValueError,
        RuntimeError,
        # default_key_generator shells out to openssl: a non-zero exit or a
        # timeout raises SubprocessError, which is not an OSError.
        subprocess.SubprocessError,
    ) as error:
        _emit({"schema_version": 1, "error": str(error)}, getattr(args, "json", False))
        return 2
    _emit(result, args.json)
    return 0
