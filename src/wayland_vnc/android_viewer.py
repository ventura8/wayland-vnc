"""Drive the actual RealVNC Viewer for Android, in the isolated emulator, over adb.

Everything here was established against the real app (4.9.4) on the project's isolated
AVD, not assumed:

* `vnc://host:port` opens the connection; the app then shows, in order and only when
  applicable, "Continue connecting?" (OK), an identity check (CONTINUE) and the
  Authentication screen. The username goes into `UserEdit`; the IME's *Next* action
  (Enter) moves focus to `PassEdit`, the password is typed there, Back hides the
  keyboard and CONTINUE submits. In landscape the soft keyboard is full-screen, so
  taps on the underlying fields do nothing while it is up -- hence the key sequence.
* On a 1920x1080 landscape screen the app shows a 1920x1080 desktop 1:1 at the
  origin, so screen coordinates are desktop coordinates for captures and taps.
* The pointer is relative (trackpad-style): a one-finger swipe moves the remote
  cursor by an accelerated multiple of the swipe, a tap clicks where the cursor is.
  Positioning is therefore closed-loop: the fixture's scene reports where the pointer
  last landed (`<XDG_RUNTIME_DIR>/pointer`) and the driver corrects until it is there.
* Swipes must stay clear of Android's edge gestures (back on the left and right edges,
  home/recents at the bottom, the shade at the top) and of the app's own toolbar in
  the top-left, or they act on the system instead of the desktop.
* Typing goes through `input text` (the app forwards hardware key events without its
  own keyboard open); secrets travel to `adb shell` over stdin, never on the host's
  command line.
* A drag is the app's double-tap-and-hold gesture; a scroll is its two-finger swipe.
  `input` drives one pointer only and the Play Store image has no root for
  `sendevent`, so two fingers go in through the emulator console (`adb emu event
  send`) as raw multi-touch slots on the touchscreen, whose axes are the natural
  (portrait) frame: `nat_x = 1080 - y`, `nat_y = x` for a landscape point, each scaled
  to the device's 0..32767 range.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "com.realvnc.viewer.android"
SCREEN = (1920, 1080)
# The touch surface that is neither a system gesture zone nor the app's toolbar.
SAFE_LEFT, SAFE_TOP, SAFE_RIGHT, SAFE_BOTTOM = 160, 230, 1760, 900
# IME action (Enter on the soft keyboard = Next/Done) and Back.
KEY_ENTER, KEY_BACK = "66", "4"
# Consecutive UI dumps with nothing to answer before a toolbar-less desktop counts.
QUIET_DUMPS = 4
# The information button on the app's floating toolbar (which the app shows again
# on a tap while it is hidden; the second tap then opens the screen).
INFO_BUTTON = (457, 129)
# How long the information screen may take to open or close on a loaded emulator
# (a single uiautomator dump takes about two seconds there).
INFO_SCREEN_TIMEOUT = 20.0
# Pointer placement: accept within this many pixels; use the fast swipe class above
# this remaining distance; never send a slow swipe shorter than this (touch slop).
TOLERANCE_PX, FAST_ABOVE_PX, SLOW_MIN_PX = 6, 120, 40
# The touchscreen device: natural (portrait) frame and its absolute axis range.
NATURAL = (1080, 1920)
TOUCH_MAX = 32767

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class UiNode:
    text: str
    resource_id: str
    class_name: str
    bounds: tuple[int, int, int, int]
    # Only the first-run analytics checkbox is read through this, but any node
    # carries it, and a missing attribute reads as unchecked.
    checked: bool = False

    @property
    def center(self) -> tuple[int, int]:
        x_min, y_min, x_max, y_max = self.bounds
        return (x_min + x_max) // 2, (y_min + y_max) // 2


@dataclass
class ConnectOutcome:
    connected: bool
    steps: list[str] = field(default_factory=list)
    error: str | None = None
    # When the app was last told to go ahead (credentials submitted, or the final
    # dialog answered): the server's first-frame latency is measured from here, the
    # automation's dialog answering before it is not the server's doing.
    submitted_at: float | None = None
    # What the credential fields held after each typing attempt (the username as
    # seen, the password only as "n/m characters"), so a refusal can be explained.
    notes: list[str] = field(default_factory=list)


UI_DUMP = "/sdcard/ui.xml"
# The positive button of an Android system dialog.
DIALOG_OK = "android:id/button1"
# A freshly installed viewer shows a four-page tour, a final page whose analytics
# checkbox is ticked by default, and -- on the first connection -- a full-screen hint
# over the desktop. A lab AVD is built per campaign, so the suite meets all three; the
# hint in particular covers the desktop, and a capture taken under it is not a frame.
FIRST_RUN_NEXT = "next_button"
FIRST_RUN_ACCEPT = "accept_button"
ANALYTICS_CHECKBOX = "analytics_checkbox"
FULLSCREEN_HINT = "Viewing full screen"
FULLSCREEN_HINT_DISMISS = "Got it"
# The event that ends one input report; every emulator gesture is framed by it.
EV_SYN = "EV_SYN:0:0"

NODE_RE = re.compile(r"<node [^>]*>")
ATTR_RE = re.compile(r'([^\s=]++)="([^"]*+)"')


def parse_ui(xml: str) -> list[UiNode]:
    """The `uiautomator dump` tree as flat nodes; only bounds-bearing nodes matter."""
    nodes = []
    for match in NODE_RE.finditer(xml):
        attrs = dict(ATTR_RE.findall(match.group(0)))
        numbers = re.findall(r"\d+", attrs.get("bounds", ""))
        if len(numbers) != 4:
            continue
        nodes.append(
            UiNode(
                text=attrs.get("text", ""),
                resource_id=attrs.get("resource-id", ""),
                class_name=attrs.get("class", ""),
                bounds=tuple(int(n) for n in numbers),  # type: ignore[arg-type]
                checked=attrs.get("checked") == "true",
            )
        )
    return nodes


def _password_field_holds(shown: str, password: str) -> bool:
    """Whether a password field's dump matches what was typed. The field is masked,
    but the dump masks one character per character, so a dropped keystroke shows as
    a shorter run of bullets -- and a truncated password is a refused connection."""
    if shown == password:
        return True
    return bool(shown) and set(shown) <= {"•", "*"} and len(shown) == len(password)


def find(nodes: list[UiNode], *, text: str | None = None, rid: str | None = None) -> UiNode | None:
    for node in nodes:
        if text is not None and text not in node.text:
            continue
        if rid is not None and not node.resource_id.endswith(rid):
            continue
        return node
    return None


class Adb:
    """The isolated AVD's adb, with the project's own key directories exported."""

    def __init__(self, adb: str, serial: str, env: dict, run: Runner | None = None):
        self.binary = adb
        self.serial = serial
        self.env = env
        self._run = subprocess.run if run is None else run

    def run(self, *args: str, timeout: float = 60, **kwargs) -> subprocess.CompletedProcess:
        return self._run(
            [self.binary, "-s", self.serial, *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **kwargs,
        )

    def shell(self, *args: str, timeout: float = 60) -> str:
        return self.run("shell", *args, timeout=timeout).stdout

    def shell_stdin(self, script: str, timeout: float = 60) -> subprocess.CompletedProcess:
        """Run a shell line fed over stdin: the host's process list never sees it."""
        return self.run("shell", input=script + "\n", timeout=timeout)

    def screencap(self, path: Path) -> bool:
        result = self._run(
            [self.binary, "-s", self.serial, "exec-out", "screencap", "-p"],
            env=self.env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
            return False
        path.write_bytes(result.stdout)
        return True

    def ui(self) -> list[UiNode]:
        """A fresh dump, or nothing: the previous dump file is removed first, because
        a dump that fails mid-transition would otherwise leave the old tree to be
        re-read as if it were current (which once declared a connection 'up')."""
        self.shell("rm", "-f", UI_DUMP)
        self.shell("uiautomator", "dump", UI_DUMP)
        return parse_ui(self.shell("cat", UI_DUMP))


class AndroidViewer:
    """The RealVNC Android app on the isolated AVD, as the scenarios need it."""

    def __init__(self, adb: Adb, *, pointer_report: Callable[[], tuple[int, int] | None]):
        self.adb = adb
        self.pointer_report = pointer_report
        # The app accelerates the pointer with finger speed, so swipes are sent in two
        # speed classes with a gain each, learnt from every observed move: fast for
        # the bulk of a move, slow for the last stretch. A slow swipe is never shorter
        # than SLOW_MIN_PX: below Android's touch slop a swipe is a tap, and a tap is
        # a click at the pointer, which no positioning step may ever cause.
        self.gain = {"fast": 1.2, "slow": 0.6}

    # -- device state -----------------------------------------------------------------

    def prepare(self, local_port: int) -> None:
        """The VNC port bridged, the app not running, auto-rotate on (the virtual
        sensor decides the orientation; see ensure_landscape)."""
        self.adb.shell("settings", "put", "system", "accelerometer_rotation", "1")
        self.adb.run("reverse", "tcp:5900", f"tcp:{local_port}")
        self.adb.shell("am", "force-stop", PACKAGE)

    def rotation(self) -> str:
        match = re.search(
            r"mCurrentRotation=(ROTATION_\d+)", self.adb.shell("dumpsys", "window", "displays")
        )
        return match.group(1) if match else "unknown"

    def ensure_landscape(self, *, sleep=time.sleep) -> bool:
        """ROTATION_90 exactly: the 1:1 desktop mapping and the touchscreen frame of
        the two-finger gesture were established there (ROTATION_270 mirrors it).
        `settings put user_rotation` does not stick on this image; the emulator's
        virtual sensor (`adb emu rotate`) does, one quarter turn per call, and only an
        activity that allows landscape (the desktop, not the launcher) shows it."""
        for _ in range(4):
            if self.rotation() == "ROTATION_90":
                return True
            self.adb.run("emu", "rotate", timeout=30)
            sleep(1.5)
        return self.rotation() == "ROTATION_90"

    def version(self) -> str:
        dump = self.adb.shell("dumpsys", "package", PACKAGE)
        match = re.search(r"versionName=(\S+)", dump)
        return f"RealVNC Viewer for Android {match.group(1) if match else 'unknown'} ({PACKAGE})"

    # -- connection ---------------------------------------------------------------------

    def connect(
        self, username: str, password: str, *, timeout: float = 90, sleep=time.sleep
    ) -> ConnectOutcome:
        """Open vnc://127.0.0.1:5900 and answer the app's screens until the desktop is
        up (a real frame is on screen, no dialog) or the app refuses."""
        outcome = ConnectOutcome(connected=False)
        self.adb.shell("am", "force-stop", PACKAGE)
        self.adb.shell(
            "am", "start", "-a", "android.intent.action.VIEW", "-d", "vnc://127.0.0.1:5900"
        )
        outcome.submitted_at = time.monotonic()
        deadline = time.monotonic() + timeout
        quiet = 0
        while time.monotonic() < deadline:
            sleep(0.5)
            nodes = self.adb.ui()
            step = self._answer_screen(nodes, outcome, username, password, sleep)
            if step == "refused":
                outcome.error = "the server refused the credentials"
                return outcome
            if step is not None:
                quiet = 0
                continue
            # The connecting screen is the desktop activity too, and a dialog can be a
            # dump late: the app counts as connected only once its toolbar has been
            # seen (it shows itself on connection for a few seconds), or after several
            # consecutive dumps with nothing to answer and the desktop in front.
            quiet += 1
            if find(nodes, rid="menu_pin") is not None or (
                quiet >= QUIET_DUMPS and self.desktop_visible(nodes)
            ):
                if not self.ensure_landscape(sleep=sleep):
                    outcome.error = "the emulator would not turn to landscape"
                    return outcome
                outcome.connected = True
                outcome.steps.append("desktop")
                return outcome
        outcome.error = outcome.error or "the desktop did not appear in time"
        return outcome

    def _answer_first_run(self, nodes) -> str | None:
        """Answer whichever first-run screen is up, or None when none is.

        Declining the analytics checkbox is deliberate: it ships ticked, so a lab
        device that simply accepted the final page would start reporting usage data
        from a qualification run.
        """
        if find(nodes, text=FULLSCREEN_HINT) is not None:
            dismiss = find(nodes, text=FULLSCREEN_HINT_DISMISS)
            if dismiss is not None:
                self._tap(dismiss)
                return "first-run-fullscreen-hint"
        accept = find(nodes, rid=FIRST_RUN_ACCEPT)
        if accept is not None:
            analytics = find(nodes, rid=ANALYTICS_CHECKBOX)
            step = "first-run-accepted"
            if analytics is not None and analytics.checked:
                self._tap(analytics)
                step = "first-run-analytics-declined"
            self._tap(accept)
            return step
        nxt = find(nodes, rid=FIRST_RUN_NEXT)
        if nxt is not None:
            self._tap(nxt)
            return "first-run-tour"
        return None

    def _answer_screen(self, nodes, outcome, username, password, sleep) -> str | None:
        """Answer whichever of the app's screens is up; the step's name, "refused"
        for a credential refusal, None when no screen needed an answer."""
        first_run = self._answer_first_run(nodes)
        if first_run is not None:
            return self._step(outcome, first_run)
        simple = (
            ("Continue connecting?", DIALOG_OK, "continue-connecting"),
            ("Identity check", "menu_done", "identity-check"),
            ("Invalid username or password", DIALOG_OK, "refused"),
        )
        for text, button, step in simple:
            if find(nodes, text=text):
                self._tap(find(nodes, rid=button))
                return step if step == "refused" else self._step(outcome, step)
        user_field = find(nodes, rid="UserEdit")
        if user_field is not None:
            if "credentials-entered" in outcome.steps:
                return "waiting"
            outcome.notes += self._enter_credentials(username, password, sleep)
            self._tap(find(nodes, rid="menu_done"))
            sleep(1)
            return self._step(outcome, "credentials-entered")
        title = find(nodes, rid="alertTitle")
        if title is not None and find(nodes, rid=DIALOG_OK):
            self._tap(find(nodes, rid=DIALOG_OK))
            return self._step(outcome, f"dialog:{title.text}")
        return None

    def _enter_credentials(self, username, password, sleep) -> list[str]:
        """Type both fields and read them back from the UI tree, retyping a field
        whose content is not what was typed: a freshly focused field drops its first
        keystroke now and then, and a truncated password is a refused connection.
        Returns one note per attempt saying what the fields held."""
        notes = []
        for attempt in range(1, 4):
            # Each attempt starts from the fields themselves: a keyboard still up from
            # the previous attempt hides them from the dump, and a tap at the field's
            # place would then hit a key. Close it first and look again.
            self._hide_ime(sleep)
            user_field = find(self.adb.ui(), rid="UserEdit")
            if user_field is None:
                notes.append(f"credentials attempt {attempt}: username field not in the tree")
                sleep(1)
                continue
            # The tap opens the full-screen IME; every key below goes to the focused
            # field; Back closes the IME again, and only then are the fields visible
            # to a UI dump (with the IME up a tap on the field's place hits a key).
            self._tap(user_field)
            sleep(0.3)
            self._retype("UserEdit", username, secret=False, sleep=sleep)
            self.adb.shell("input", "keyevent", KEY_ENTER)  # IME Next: focus PassEdit
            sleep(0.3)
            self._retype("PassEdit", password, secret=True, sleep=sleep)
            self._hide_ime(sleep)
            nodes = self.adb.ui()
            user = find(nodes, rid="UserEdit")
            secret = find(nodes, rid="PassEdit")
            shown = None if secret is None else secret.text
            notes.append(
                f"credentials attempt {attempt}: username field "
                f"{'missing' if user is None else repr(user.text)}, password field "
                f"{'missing' if shown is None else f'{len(shown)}/{len(password)} characters'}"
            )
            if user is not None and user.text == username and shown is not None:
                if _password_field_holds(shown, password):
                    break
        return notes

    def _ime_up(self) -> bool:
        """Whether the soft keyboard is on screen: the input method service says so,
        or its full-screen extract view is in the UI tree -- the latter has been seen
        with the former already reporting the keyboard down."""
        if "mInputShown=true" in self.adb.shell("dumpsys", "input_method"):
            return True
        return any(node.class_name.endswith("ExtractEditText") for node in self.adb.ui())

    def _hide_ime(self, sleep) -> None:
        """Back closes the soft keyboard -- and only then: with no keyboard up, Back on
        the Authentication screen cancels the connection and leaves the app. Asked
        again after each Back: one press did not always take on a loaded emulator, and
        the fields stay out of the dump until the keyboard is really gone."""
        for _ in range(3):
            if not self._ime_up():
                return
            self.adb.shell("input", "keyevent", KEY_BACK)
            sleep(0.5)

    def _retype(self, rid: str, value: str, *, secret: bool, sleep) -> None:
        """Clear the focused field (end of text, then as many deletes as it could
        hold) and type the value; never through the host's command line for a secret."""
        deletes = " ".join(["67"] * (len(value) + 8))
        self.adb.shell("input", "keyevent", "123")  # MOVE_END
        self.adb.shell("input", "keyevent", *deletes.split())
        if secret:
            self.type_secret(value)
        else:
            self.adb.shell("input", "text", value)
        sleep(0.3)
        del rid

    @staticmethod
    def _step(outcome: ConnectOutcome, name: str) -> str:
        outcome.steps.append(name)
        outcome.submitted_at = time.monotonic()
        return name

    def desktop_visible(self, nodes: list[UiNode] | None = None) -> bool:
        """The connected view: the app's desktop activity is in front and no dialog
        is up. (Its toolbar hides itself after a few seconds, so it is no signal.)"""
        nodes = self.adb.ui() if nodes is None else nodes
        if find(nodes, rid="alertTitle") is not None:
            return False
        activities = self.adb.shell("dumpsys", "activity", "activities")
        line = re.search(r"topResumedActivity=([^\n]*)", activities)
        if line is None:
            return False
        activity = next((word for word in line.group(1).split() if "/" in word), None)
        return activity is not None and activity.endswith("/.app.DesktopActivity")

    def disconnect(self) -> None:
        self.adb.shell("am", "force-stop", PACKAGE)

    def _open_info_screen(self, *, sleep, clock) -> tuple[object, bool, int, list[str]]:
        """Poll until the information screen's size field is in the tree, tapping the
        toolbar's information button once the toolbar itself has been seen in a dump.

        Returns (field, tapped, dumps, ids): the field or None, whether the button was
        ever tapped, how many dumps it took, and -- when it timed out -- the resource
        ids of the last dump, which is what makes a timeout diagnosable afterwards.
        """
        deadline = clock() + INFO_SCREEN_TIMEOUT
        tapped = False
        dumps = 0
        while True:
            nodes = self.adb.ui()
            dumps += 1
            details = self._info_field(nodes)
            if details is not None:
                return details, tapped, dumps, []
            if not tapped and find(nodes, rid="menu_pin") is not None:
                self._tap_screen(*INFO_BUTTON)
                tapped = True
            if clock() >= deadline:
                ids = sorted({node.resource_id.rsplit("/", 1)[-1] for node in nodes})[:12]
                return None, tapped, dumps, ids
            sleep(0.5)

    def desktop_size(self, *, sleep=time.sleep, clock=time.monotonic) -> tuple[int, int] | None:
        """The desktop size the app itself reports on its information screen, for a
        live session; None when the screen could not be reached in time.

        The toolbar's information button is tapped where the app draws it, but only
        once the toolbar has been seen in a dump: a tap on the bare desktop is a click
        the app sends to the server. Everything is polled under one deadline rather
        than read after fixed waits -- right after a mode change the app re-lays out
        and `uiautomator` answers with an empty hierarchy for a moment, and on a loaded
        emulator the screen opens late; one early dump read as "no size" made the mode
        scenarios fail. Back closes the screen, and the field must be gone again before
        the caller captures the desktop.
        """
        details, tapped, dumps, ids = self._open_info_screen(sleep=sleep, clock=clock)
        size = None
        if details is not None:
            match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", details.text)
            size = (int(match.group(1)), int(match.group(2))) if match else None
            if size is None:
                self.size_report_failure = f"size field read {details.text!r}"
        else:
            what = "the information screen did not open" if tapped else "the toolbar was not seen"
            self.size_report_failure = (
                f"{what} within {INFO_SCREEN_TIMEOUT:.0f}s ({dumps} dumps; last dump ids {ids})"
            )
            if not tapped:
                return None
        self.adb.shell("input", "keyevent", KEY_BACK)
        if self._await_info(present=False, sleep=sleep, clock=clock) is not None:
            self.adb.shell("input", "keyevent", KEY_BACK)
        return size

    # Why the last desktop_size() answered None, for the record.
    size_report_failure = ""

    def transient_error(self) -> bool:
        """The RA2 handshake occasionally fails on a rapid reconnect with the app's
        "RSA decrypt/check error: bad length" dialog, and a connection the transport
        dropped before the handshake shows as "The connection closed unexpectedly":
        viewer-side transients, not server refusals; the desktop driver treats its
        log lines the same way."""
        return any(
            "RSA decrypt/check error" in node.text
            or "bad length" in node.text
            or "connection closed unexpectedly" in node.text
            for node in self.adb.ui()
        )

    @staticmethod
    def _info_field(nodes: list[UiNode]) -> UiNode | None:
        return find(nodes, rid="text_view_desktop_size_details")

    def _await_info(self, *, present: bool, sleep, clock) -> UiNode | None:
        """Poll until the information screen's size field is there (present=True) or
        gone (present=False); returns the field as last seen."""
        deadline = clock() + INFO_SCREEN_TIMEOUT
        while True:
            details = self._info_field(self.adb.ui())
            if (details is not None) == present or clock() >= deadline:
                return details
            sleep(0.5)

    def screenshot(self, path: Path) -> bool:
        return self.adb.screencap(path)

    # -- input --------------------------------------------------------------------------

    def type_text(self, text: str) -> None:
        if text == "\n":
            self.adb.shell("input", "keyevent", KEY_ENTER)
            return
        if not text.replace("-", "").isalnum():
            raise ValueError("only alphanumeric text is typed")
        self.adb.shell("input", "text", text)

    def type_secret(self, secret: str) -> None:
        if not secret or any(ch.isspace() or ch in "'\"\\;&|$`" for ch in secret):
            raise ValueError("secret contains characters the input tool cannot carry safely")
        result = self.adb.shell_stdin(f"input text {secret}")
        if result.returncode != 0:
            raise OSError("typing the secret failed")

    def type_unlock_secret(self, secret: str) -> None:
        """The fixture's lock screen, through the app: a warm-up key and its Backspace
        first, so the occasional dropped first keystroke costs nothing, then the
        secret and Return."""
        self.adb.shell("input", "keyevent", "62", "67")  # SPACE, DEL
        self.type_secret(secret)
        self.adb.shell("input", "keyevent", KEY_ENTER)

    def move_cursor_to(
        self, x_pos: int, y_pos: int, *, attempts: int = 14, sleep=time.sleep
    ) -> bool:
        """Closed loop: read where the pointer is, swipe toward the target with the
        learnt gain of the speed class the distance calls for, read again; done
        within TOLERANCE_PX."""
        for _ in range(attempts):
            current = self.pointer_report()
            if current is None:
                self._swipe(SLOW_MIN_PX, SLOW_MIN_PX, "slow", sleep)  # provoke a first report
                continue
            delta_x, delta_y = x_pos - current[0], y_pos - current[1]
            if max(abs(delta_x), abs(delta_y)) <= TOLERANCE_PX:
                return True
            speed = "fast" if max(abs(delta_x), abs(delta_y)) > FAST_ABOVE_PX else "slow"
            current, commanded = self._step_toward(current, delta_x, delta_y, speed, sleep)
            after = self.pointer_report()
            if after is not None and commanded and after != current:
                moved = max(abs(after[0] - current[0]), abs(after[1] - current[1]))
                asked = max(abs(commanded[0]), abs(commanded[1]))
                if asked:
                    self.gain[speed] = min(4.0, max(0.2, moved / asked))
        current = self.pointer_report()
        return (
            current is not None
            and max(abs(current[0] - x_pos), abs(current[1] - y_pos)) <= TOLERANCE_PX
        )

    def _step_toward(self, current, delta_x, delta_y, speed, sleep):
        """One swipe toward the target in the given speed class; returns the pointer
        position the swipe started from and the delta actually commanded."""
        gain = self.gain[speed]
        wanted_x, wanted_y = round(delta_x / gain), round(delta_y / gain)
        if speed == "slow" and 0 < max(abs(wanted_x), abs(wanted_y)) < SLOW_MIN_PX:
            # Finer than one minimal swipe can move: go the minimal distance away
            # first, then come back by the minimal distance plus what was wanted.
            # Both swipes stay above the tap threshold; the net move is what was wanted.
            def away(wanted: int) -> int:
                """The minimal swipe pointing away from where the move is headed."""
                if wanted == 0:
                    return 0
                return -SLOW_MIN_PX if wanted > 0 else SLOW_MIN_PX

            away_x, away_y = away(wanted_x), away(wanted_y)
            self._swipe(away_x, away_y, "slow", sleep)
            wanted_x, wanted_y = wanted_x - away_x, wanted_y - away_y
            current = self.pointer_report() or current
        return current, self._swipe(wanted_x, wanted_y, speed, sleep)

    def click(self, x_pos: int, y_pos: int, *, sleep=time.sleep) -> None:
        if not self.move_cursor_to(x_pos, y_pos, sleep=sleep):
            raise OSError(f"could not place the pointer at {x_pos},{y_pos}")
        self._tap_screen(960, 800)

    def drag(self, x_from: int, y_from: int, x_to: int, y_to: int, *, sleep=time.sleep) -> None:
        """Press-and-hold, then move: the app's drag gesture, with the pointer first
        placed at the start and the held move scaled by the learnt gain."""
        if not self.move_cursor_to(x_from, y_from, sleep=sleep):
            raise OSError(f"could not place the pointer at {x_from},{y_from}")
        delta_x = round((x_to - x_from) / self.gain["fast"])
        delta_y = round((y_to - y_from) / self.gain["fast"])
        start_x, start_y = self._start_for(delta_x, delta_y)
        # One shell invocation for the whole gesture: the double-tap window is a few
        # hundred milliseconds, which separate adb round trips do not reliably meet.
        steps = 8
        script = [
            f"input tap {start_x} {start_y}",
            f"input motionevent DOWN {start_x} {start_y}",
            "sleep 0.7",
        ]
        for step in range(1, steps + 1):
            script.append(
                f"input motionevent MOVE {start_x + delta_x * step // steps} "
                f"{start_y + delta_y * step // steps}"
            )
        script.append(f"input motionevent UP {start_x + delta_x} {start_y + delta_y}")
        self.adb.shell("; ".join(script), timeout=120)

    def scroll(self, x_pos: int, y_pos: int, *, sleep=time.sleep) -> None:
        """Two-finger swipe over the target: the app's scroll gesture."""
        if not self.move_cursor_to(x_pos, y_pos, sleep=sleep):
            raise OSError(f"could not place the pointer at {x_pos},{y_pos}")
        self.two_finger_swipe(0, -200, sleep=sleep)

    def two_finger_swipe(self, delta_x: int, delta_y: int, *, sleep=time.sleep) -> None:
        """Two pointers 100 px apart moving together by (delta_x, delta_y), as raw
        multi-touch slots through the emulator console."""
        x_start, y_start = 900, 600
        fingers = ((x_start, y_start), (x_start + 100, y_start))
        for slot, (x_pos, y_pos) in enumerate(fingers):
            dev_x, dev_y = self._natural(x_pos, y_pos)
            self._emu_send(
                f"EV_ABS:ABS_MT_SLOT:{slot}",
                f"EV_ABS:ABS_MT_TRACKING_ID:{500 + slot}",
                f"EV_ABS:ABS_MT_POSITION_X:{dev_x}",
                f"EV_ABS:ABS_MT_POSITION_Y:{dev_y}",
                "EV_ABS:ABS_MT_PRESSURE:1024",
            )
        self._emu_send(EV_SYN)
        sleep(0.1)
        steps = 12
        for step in range(1, steps + 1):
            events = []
            for slot, (x_pos, y_pos) in enumerate(fingers):
                dev_x, dev_y = self._natural(
                    x_pos + delta_x * step // steps, y_pos + delta_y * step // steps
                )
                events += [
                    f"EV_ABS:ABS_MT_SLOT:{slot}",
                    f"EV_ABS:ABS_MT_POSITION_X:{dev_x}",
                    f"EV_ABS:ABS_MT_POSITION_Y:{dev_y}",
                ]
            self._emu_send(*events, EV_SYN)
            sleep(0.03)
        self._emu_send(
            "EV_ABS:ABS_MT_SLOT:0",
            "EV_ABS:ABS_MT_TRACKING_ID:4294967295",
            "EV_ABS:ABS_MT_SLOT:1",
            "EV_ABS:ABS_MT_TRACKING_ID:4294967295",
            EV_SYN,
        )

    @staticmethod
    def _natural(x_pos: int, y_pos: int) -> tuple[int, int]:
        """A landscape screen point as touchscreen axis values."""
        natural_x, natural_y = NATURAL[0] - y_pos, x_pos
        return (
            round(natural_x * TOUCH_MAX / NATURAL[0]),
            round(natural_y * TOUCH_MAX / NATURAL[1]),
        )

    def _emu_send(self, *events: str) -> None:
        result = self.adb.run("emu", "event", "send", *events, timeout=30)
        if result.returncode != 0 or "KO" in result.stdout:
            raise OSError(f"emulator console refused events: {result.stdout.strip()}")

    # -- helpers ------------------------------------------------------------------------

    def _tap(self, node: UiNode | None) -> None:
        if node is None:
            raise OSError("expected control is not on screen")
        self._tap_screen(*node.center)

    def _tap_screen(self, x_pos: int, y_pos: int) -> None:
        self.adb.shell("input", "tap", str(x_pos), str(y_pos))

    @staticmethod
    def _start_for(delta_x: int, delta_y: int) -> tuple[int, int]:
        """A start point from which the whole swipe stays inside the safe zone."""
        start_x = SAFE_LEFT if delta_x >= 0 else SAFE_RIGHT
        start_y = SAFE_TOP if delta_y >= 0 else SAFE_BOTTOM
        return start_x, start_y

    def _swipe(self, delta_x: int, delta_y: int, speed: str, sleep) -> tuple[int, int] | None:
        """One swipe of the given delta, clamped to the safe zone and stretched to
        SLOW_MIN_PX when slow, at the class's steady speed so its gain stays comparable
        between moves."""
        max_x = SAFE_RIGHT - SAFE_LEFT
        max_y = SAFE_BOTTOM - SAFE_TOP
        delta_x = max(-max_x, min(max_x, delta_x))
        delta_y = max(-max_y, min(max_y, delta_y))
        if delta_x == 0 and delta_y == 0:
            return None
        length = max(abs(delta_x), abs(delta_y))
        if speed == "slow" and length < SLOW_MIN_PX:
            scale = SLOW_MIN_PX / length
            delta_x, delta_y = round(delta_x * scale), round(delta_y * scale)
            length = max(abs(delta_x), abs(delta_y))
        start_x, start_y = self._start_for(delta_x, delta_y)
        duration = length if speed == "fast" else length * 4  # 1 px/ms or 0.25 px/ms
        self.adb.shell(
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(start_x + delta_x),
            str(start_y + delta_y),
            str(max(100, duration)),
        )
        sleep(0.6)
        return delta_x, delta_y
