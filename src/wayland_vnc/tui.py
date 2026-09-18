"""Launcher menu for wayland-vnc: install, diagnostics, and uninstall.

The menu logic is a pure state machine driven through an injected ``Screen`` so it is
unit-tested with a scripted fake screen and never needs a real terminal. The curses
front-end is a thin adapter over the same interface. Actions delegate to the runtime
and to the session's own ``systemctl --user``; nothing here touches a system service.
"""

import curses
import getpass
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wayland_vnc import runtime
from wayland_vnc.probe import collect, report

SERVICE = "wayland-vnc.service"


@dataclass
class MenuItem:
    key: str
    label: str
    action: Callable[[], str]


class Screen:
    """Abstract terminal: render a menu and read a selection key. Injectable for tests."""

    def render(self, title: str, items: list[MenuItem], selected: int, status: str) -> None:
        raise NotImplementedError

    def read_key(self) -> str:
        raise NotImplementedError

    def message(self, text: str) -> None:
        raise NotImplementedError


@dataclass
class Installation:
    """What the launcher can tell about the current per-user installation state."""

    config_dir: Path
    which: Callable[[str], str | None]
    run: Callable[[list[str]], subprocess.CompletedProcess]

    def is_provisioned(self) -> bool:
        try:
            return runtime.read_credentials(self.config_dir) is not None
        except (ValueError, OSError):
            # A malformed or unreadable credential still counts as "installed" for the
            # menu: something is there, and the menu's uninstall is how to clear it.
            return True

    def systemctl(self, *args: str) -> str:
        if self.which("systemctl") is None:
            return "systemctl is not available in this session"
        completed = self.run(["systemctl", "--user", *args])
        output = (completed.stdout or completed.stderr or "").strip()
        return output or f"systemctl --user {args[0]} ok"


def _install(install: Installation, read_secret: Callable[[str], str]) -> str:
    def checked(prompt: str) -> str:
        # Same guard the CLI applies: refuse a password this desktop's backend
        # cannot accept BEFORE it is written, rather than storing one that the
        # server will then reject at every connection.
        secret = read_secret(prompt)
        runtime.check_password_for_backend(secret)
        return secret

    runtime.set_password(install.config_dir, read_secret=checked)
    # Preserving: reconfiguring must not move a server someone bound to loopback, or
    # to another port, back onto the local network.
    runtime.provision_preserving_bind(
        install.config_dir, generate_key=runtime.default_key_generator
    )
    # A previous uninstall masked the unit for this user; lift that before enabling,
    # or `enable --now` fails against the mask.
    install.systemctl("unmask", SERVICE)
    return install.systemctl("enable", "--now", SERVICE)


def _diagnostics() -> str:
    result = report(collect())
    candidate = result["backend_candidate"] or "none"
    return f"Backend candidate: {candidate}\n{result['reason']}\nQualification: unqualified."


def _uninstall(install: Installation) -> str:
    message = install.systemctl("disable", "--now", SERVICE)
    # The package enables the unit globally, so a per-user disable alone is undone at
    # the next login, when the service would start again and generate a fresh
    # credential. Masking it for this user makes the uninstall stick until this menu
    # (or `systemctl --user unmask`) reverses it.
    install.systemctl("mask", SERVICE)
    # The credentials file is not the only copy: provisioning writes the same
    # password into wayvnc.conf, so removing one and leaving the other would
    # keep the plaintext secret on disk after an uninstall.
    for name in (runtime.CREDENTIALS_NAME, runtime.CONFIG_NAME):
        stored = install.config_dir / name
        if stored.exists():
            stored.unlink()
    return (
        f"{message}\nMasked the unit for this user.\n"
        "Removed the stored viewer credential and the provisioned configuration."
    )


def build_menu(install: Installation, read_secret: Callable[[str], str]) -> list[MenuItem]:
    """The menu adapts to installation state: uninstall only appears once provisioned."""
    items = [
        MenuItem("d", "Run diagnostics (read-only)", _diagnostics),
    ]
    if install.is_provisioned():
        items.insert(
            0,
            MenuItem(
                "i",
                "Reconfigure and restart the VNC server",
                lambda: _install(install, read_secret),
            ),
        )
        items.append(
            MenuItem("u", "Uninstall (stop server, remove credential)", lambda: _uninstall(install))
        )
    else:
        items.insert(
            0,
            MenuItem(
                "i", "Install and start the VNC server", lambda: _install(install, read_secret)
            ),
        )
    items.append(MenuItem("q", "Quit", lambda: ""))
    return items


@dataclass
class Menu:
    """Drive the menu until Quit; return the transcript of status lines for tests."""

    install: Installation
    read_secret: Callable[[str], str]
    transcript: list[str] = field(default_factory=list)

    def run(self, screen: Screen) -> list[str]:
        selected = 0
        status = "Select an action."
        while True:
            items = build_menu(self.install, self.read_secret)
            selected = min(selected, len(items) - 1)
            screen.render("wayland-vnc setup", items, selected, status)
            key = screen.read_key()
            if key == "up":
                selected = (selected - 1) % len(items)
                continue
            if key == "down":
                selected = (selected + 1) % len(items)
                continue
            chosen = None
            if key == "enter":
                chosen = items[selected]
            else:
                chosen = next((item for item in items if item.key == key), None)
            if chosen is None:
                continue
            if chosen.key == "q":
                return self.transcript
            try:
                status = chosen.action() or "Done."
            except EOFError:
                # Ctrl-D at the password prompt: the action is cancelled, the menu stays.
                status = "Cancelled."
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                # A refused password or a failed systemctl is something to report and
                # carry on from; quitting the launcher would lose the menu with it.
                status = f"Failed: {error}"
            self.transcript.append(status)
            screen.message(status)


class CursesScreen(Screen):  # pragma: no cover - thin curses adapter over the tested Menu
    """Real terminal front-end. The tested logic lives in Menu/build_menu, not here."""

    def __init__(self, window):
        self._window = window

    def render(self, title, items, selected, status):
        self._window.clear()
        self._window.addstr(0, 0, title, curses.A_BOLD)
        for index, item in enumerate(items):
            marker = "> " if index == selected else "  "
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            self._window.addstr(index + 2, 0, f"{marker}[{item.key}] {item.label}", attr)
        self._window.addstr(len(items) + 3, 0, status)
        self._window.refresh()

    def read_key(self):
        code = self._window.getch()
        if code in (curses.KEY_UP,):
            return "up"
        if code in (curses.KEY_DOWN,):
            return "down"
        if code in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            return "enter"
        if code == 27:
            return "q"
        return chr(code).lower() if 0 <= code < 256 else ""

    def message(self, text):
        self._window.addstr(text)
        self._window.refresh()


def main() -> int:  # pragma: no cover - the curses wrapper is exercised interactively
    install = Installation(
        config_dir=runtime.config_dir(),
        which=shutil.which,
        run=lambda argv: subprocess.run(argv, capture_output=True, text=True, check=False),
    )

    def run(window):
        def read_secret(prompt: str) -> str:
            # curses leaves the terminal in cbreak/noecho mode, in which getpass
            # cannot offer line editing and the prompt lands wherever the cursor is.
            # endwin() hands the terminal back in its shell state for the prompt; the
            # next refresh() puts curses' own modes back and redraws the screen.
            curses.endwin()
            try:
                return getpass.getpass(prompt)
            finally:
                window.refresh()

        return Menu(install, read_secret=read_secret).run(CursesScreen(window))

    curses.wrapper(run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
