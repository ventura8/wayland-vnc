"""The Android viewer driver against a scripted adb: the app's connection screens are
answered in order, secrets never touch the host command line, the relative pointer
converges on the target by reading the fixture's report, and gestures go out as the
event sequences the app was seen to accept."""

import subprocess
from pathlib import Path

import pytest

from wayland_vnc import android_viewer
from wayland_vnc.android_viewer import (
    NATURAL,
    SAFE_BOTTOM,
    SAFE_LEFT,
    SAFE_RIGHT,
    SAFE_TOP,
    SLOW_MIN_PX,
    TOUCH_MAX,
    Adb,
    AndroidViewer,
    find,
    parse_ui,
)

CONTINUE_XML = (
    '<node text="Continue connecting?" resource-id="com.realvnc.viewer.android:id/alertTitle"'
    ' class="android.widget.TextView" bounds="[441,380][1478,451]"/>'
    '<node text="OK" resource-id="android:id/button1" class="android.widget.Button"'
    ' bounds="[1341,609][1509,735]"/>'
)
IDENTITY_XML = (
    '<node text="Identity check" resource-id="" class="android.widget.TextView"'
    ' bounds="[189,100][420,151]"/>'
    '<node text="CONTINUE" resource-id="com.realvnc.viewer.android:id/menu_done"'
    ' class="android.widget.Button" bounds="[1657,63][1878,189]"/>'
)
AUTH_XML = (
    '<node text="Authentication" resource-id="" class="android.widget.TextView"'
    ' bounds="[189,100][432,151]"/>'
    '<node text="CONTINUE" resource-id="com.realvnc.viewer.android:id/menu_done"'
    ' class="android.widget.Button" bounds="[1657,63][1878,189]"/>'
    '<node text="Username" resource-id="com.realvnc.viewer.android:id/UserEdit"'
    ' class="android.widget.EditText" bounds="[48,405][1872,523]"/>'
    '<node text="Password" resource-id="com.realvnc.viewer.android:id/PassEdit"'
    ' class="android.widget.EditText" bounds="[48,584][1872,710]"/>'
)
DESKTOP_XML = (
    '<node text="" resource-id="com.realvnc.viewer.android:id/menu_pin"'
    ' class="android.widget.ImageButton" bounds="[58,72][172,187]"/>'
)
REFUSED_XML = (
    '<node text="Invalid username or password" resource-id="android:id/message"'
    ' class="android.widget.TextView" bounds="[378,450][1541,507]"/>'
    '<node text="OK" resource-id="android:id/button1" class="android.widget.Button"'
    ' bounds="[1341,540][1509,666]"/>'
)
DESKTOP_ACTIVITY = (
    "topResumedActivity=ActivityRecord{1 u0 com.realvnc.viewer.android/.app.DesktopActivity t27}"
)


class ScriptedAdb:
    """Records every adb invocation; serves UI dumps from a queue of screens, and keeps
    the two credential fields the way the app does: focus follows the tap on the
    username field and the IME's Next, typing appends to the focused field, deletes
    clear it. `drop_first` mimics the dropped first keystroke of a freshly focused
    field, once per field."""

    # Taps that move the app to its next screen: the dialog's OK / the CONTINUE button
    # / the toolbar's information button / the first-run tour's Next, its final page's
    # Get Started, and the full-screen hint's Got it. The analytics checkbox is
    # deliberately absent: clearing it stays on the same page.
    ADVANCING_TAPS = (
        ["1425", "672"],
        ["1767", "126"],
        ["457", "129"],
        ["539", "1308"],
        ["540", "938"],
        ["1462", "500"],
        ["959", "639"],
    )

    def __init__(self, screens, *, drop_first=False):
        self.screens = list(screens)
        self.index = 0
        self.calls = []
        self.stdin = []
        self.activity = DESKTOP_ACTIVITY
        self.fields = {"UserEdit": "", "PassEdit": ""}
        self.focus = None
        # The virtual sensor: each `emu rotate` is a quarter turn; landscape is 90.
        self.rotations = ["ROTATION_0", "ROTATION_270", "ROTATION_180", "ROTATION_90"]
        self.turn = 3
        self.dropped = set() if drop_first else {"UserEdit", "PassEdit"}
        # A screen that only appears after this many dumps (an information screen
        # opening late on a loaded emulator).
        self.advance_after_dumps = 0
        # How many Back presses the keyboard swallows before it really closes.
        self.sticky_ime = 0

    def _typed(self, text):
        if self.focus is None:
            return
        if self.focus not in self.dropped:
            self.dropped.add(self.focus)
            text = text[1:]
        self.fields[self.focus] += text

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        tail = args[3:]  # after adb -s serial
        if "input" in kwargs:
            self.stdin.append(kwargs["input"])
            self._typed(kwargs["input"].strip().removeprefix("input text "))
        if tail[:3] == ["shell", "input", "tap"] and tail[3:5] in self.ADVANCING_TAPS:
            self.index = min(self.index + 1, len(self.screens) - 1)
        if tail[:3] == ["shell", "input", "tap"] and tail[3:5] == ["960", "464"]:
            self.focus = "UserEdit"
        elif tail[:3] == ["shell", "input", "keyevent"] and tail[3] == "66" and self.focus:
            self.focus = "PassEdit"
        elif tail[:3] == ["shell", "input", "keyevent"] and tail[3] == "67" and self.focus:
            self.fields[self.focus] = ""
        elif tail[:3] == ["shell", "input", "keyevent"] and tail[3] == "4":
            if self.sticky_ime > 0 and self.focus:
                self.sticky_ime -= 1  # the keyboard stayed up this time
            else:
                self.focus = None  # Back closes the keyboard: nothing is focused for typing
            # Back closes the information screen and the first-run help view.
            closable = ("text_view_desktop_size_details", "help_view")
            if any(marker in self.screens[self.index] for marker in closable):
                self.index = min(self.index + 1, len(self.screens) - 1)  # closes the screen
        elif tail[:3] == ["shell", "input", "text"]:
            self._typed(tail[3])
        stdout = ""
        if tail[:2] == ["shell", "cat"] and tail[2] == "/sdcard/ui.xml":
            screen = self.screens[self.index]
            screen = screen.replace('text="Username"', f'text="{self.fields["UserEdit"]}"')
            screen = screen.replace('text="Password"', f'text="{self.fields["PassEdit"]}"')
            stdout = f"<hierarchy>{screen}</hierarchy>"
            if self.advance_after_dumps > 0:
                self.advance_after_dumps -= 1
                if self.advance_after_dumps == 0:
                    self.index = min(self.index + 1, len(self.screens) - 1)
        elif tail[:3] == ["shell", "dumpsys", "activity"]:
            stdout = self.activity
        elif tail[:3] == ["shell", "dumpsys", "package"]:
            stdout = "    versionName=4.9.4.60176\n"
        elif tail[:3] == ["shell", "dumpsys", "input_method"]:
            stdout = "  mInputShown=true\n" if self.focus else "  mInputShown=false\n"
        elif tail[:3] == ["shell", "dumpsys", "window"]:
            stdout = f"    mCurrentRotation={self.rotations[self.turn % 4]}\n"
        elif tail[:2] == ["emu", "rotate"]:
            self.turn += 1
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def shells(self):
        return [" ".join(c[4:]) for c in self.calls if len(c) > 4 and c[3] == "shell"]


def _viewer(screens, pointer=lambda: None):
    scripted = ScriptedAdb(screens)
    adb = Adb("/sdk/adb", "emulator-5554", {}, run=scripted)
    return AndroidViewer(adb, pointer_report=pointer), scripted


# A freshly installed viewer: the tour, then the final page whose analytics checkbox
# ships ticked, then the full-screen hint the app lays over the desktop on first use.
TOUR_XML = (
    '<node text="Take control" resource-id="com.realvnc.viewer.android:id/textView"'
    ' class="android.widget.TextView" bounds="[159,431][920,577]"/>'
    '<node text="Next" resource-id="com.realvnc.viewer.android:id/next_button"'
    ' class="android.widget.Button" bounds="[424,1245][655,1371]"/>'
)
FIRST_RUN_LAST_XML = (
    '<node text="Get Started" resource-id="com.realvnc.viewer.android:id/accept_button"'
    ' class="android.widget.Button" bounds="[358,875][722,1001]"/>'
    '<node text="Send anonymous usage data to improve the app"'
    ' resource-id="com.realvnc.viewer.android:id/analytics_checkbox"'
    ' class="android.widget.CheckBox" checked="true" bounds="[63,1211][1017,1329]"/>'
)
FIRST_RUN_LAST_UNTICKED_XML = FIRST_RUN_LAST_XML.replace('checked="true"', 'checked="false"')
FULLSCREEN_HINT_XML = (
    '<node text="Viewing full screen" resource-id="" class="android.widget.TextView"'
    ' bounds="[700,220][1220,300]"/>'
    '<node text="Got it" resource-id="" class="android.widget.Button"'
    ' bounds="[1375,455][1550,545]"/>'
)


HELP_VIEW_XML = (
    '<node text="" resource-id="com.realvnc.viewer.android:id/help_view"'
    ' class="android.widget.FrameLayout" bounds="[0,0][1920,1080]"/>'
    '<node text="How to control" resource-id="" class="android.widget.TextView"'
    ' bounds="[189,100][430,151]"/>'
)


TUTORIAL_XML = (
    '<node text="This is your toolbar" resource-id="" class="android.widget.TextView"'
    ' bounds="[736,390][1183,445]"/>'
    '<node text="SKIP TUTORIAL" resource-id="" class="android.widget.Button"'
    ' bounds="[777,586][1142,692]"/>'
)


def test_the_toolbar_coach_mark_is_skipped():
    """The last first-run screen sits on the live desktop: the scene really is behind
    it, which is why a capture taken under it looks almost right and is not a frame."""
    viewer, _scripted = _viewer([AUTH_XML, TUTORIAL_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert "first-run-tutorial-skipped" in outcome.steps


def test_the_desktop_is_not_visible_under_the_coach_mark():
    viewer, _scripted = _viewer([TUTORIAL_XML])
    assert viewer.desktop_visible(parse_ui(f"<hierarchy>{TUTORIAL_XML}</hierarchy>")) is False


def test_the_first_run_help_view_is_closed_with_back():
    """The app opens a full-page help view over the desktop after the first
    connection. Its close button carries no resource id; Back closes it."""
    viewer, scripted = _viewer([AUTH_XML, HELP_VIEW_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert "first-run-help" in outcome.steps
    assert "input keyevent 4" in scripted.shells()


def test_the_desktop_is_not_visible_under_the_help_view():
    """The help view belongs to the desktop activity, so the activity check cannot
    see it. Without this, a capture under it counts as a frame -- which is how a
    fresh AVD produced a full set of screenshots of the help text."""
    viewer, _scripted = _viewer([HELP_VIEW_XML])
    assert viewer.desktop_visible(parse_ui(f"<hierarchy>{HELP_VIEW_XML}</hierarchy>")) is False


def test_an_untouched_password_field_does_not_read_as_a_typed_one():
    """The hint "Password" is exactly eight characters, so a length alone made an
    empty field look like a correctly typed eight-character password -- which is how
    a total input failure was read as "only the username failed" for an afternoon."""
    from wayland_vnc.android_viewer import _password_field_note

    # The hint is eight characters, exactly like the password, and must not read as one.
    assert _password_field_note("Password", "hunter2x") == (
        "8 characters that are neither the password nor masked input"
    )
    assert _password_field_note("\u2022" * 8, "hunter2x") == "8/8 characters"
    assert _password_field_note(None, "hunter2x") == "missing"
    # A short run of bullets is masked input, just not enough of it.
    assert _password_field_note("\u2022" * 5, "hunter2x") == "5/8 masked characters, short"


def test_parse_ui_reads_the_checked_attribute():
    """The analytics checkbox is answered on this attribute, so it has to survive the
    parse; a node without it reads as unchecked rather than as missing."""
    nodes = parse_ui(f"<hierarchy>{FIRST_RUN_LAST_XML}</hierarchy>")
    assert find(nodes, rid="analytics_checkbox").checked is True
    assert find(nodes, rid="accept_button").checked is False


def test_first_run_tour_is_walked_and_analytics_is_declined():
    """A fresh install shows the tour before anything else. The suite must reach the
    desktop through it, and must clear the analytics checkbox on the way: it ships
    ticked, so accepting the page as-is would opt a qualification device into
    reporting usage data."""
    viewer, scripted = _viewer([TOUR_XML, TOUR_XML, FIRST_RUN_LAST_XML, AUTH_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert outcome.steps.count("first-run-tour") == 2
    assert "first-run-analytics-declined" in outcome.steps
    # The checkbox is tapped before the page is accepted, or the tap lands on a page
    # that is already gone.
    taps = [c for c in scripted.calls if "tap" in " ".join(c)]
    assert len(taps) >= 2


def test_an_already_ticked_off_analytics_box_is_left_alone():
    """Declining twice would re-enable it."""
    viewer, _scripted = _viewer([FIRST_RUN_LAST_UNTICKED_XML, AUTH_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert "first-run-accepted" in outcome.steps
    assert "first-run-analytics-declined" not in outcome.steps


def test_the_full_screen_hint_is_dismissed_before_the_desktop_counts():
    """The hint covers the desktop. A capture taken under it is a picture of a
    tutorial card, which is how a fresh AVD produced fifteen failed scenarios."""
    viewer, _scripted = _viewer([AUTH_XML, FULLSCREEN_HINT_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert "first-run-fullscreen-hint" in outcome.steps


def test_a_provisioned_viewer_shows_no_first_run_screens():
    """An AVD that has already been through the tour must not pay for any of this."""
    viewer, _scripted = _viewer([AUTH_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert not [s for s in outcome.steps if s.startswith("first-run")]


def test_parse_ui_and_find_read_bounds_ids_and_text():
    nodes = parse_ui(f"<hierarchy>{AUTH_XML}</hierarchy>")
    assert [n.resource_id.rsplit("/", 1)[-1] for n in nodes] == [
        "",
        "menu_done",
        "UserEdit",
        "PassEdit",
    ]
    assert find(nodes, rid="UserEdit").center == (960, 464)
    assert find(nodes, text="Authentication") is not None
    assert find(nodes, rid="nothing") is None


def test_connect_answers_every_screen_in_order_and_keeps_the_secret_off_argv():
    viewer, scripted = _viewer([CONTINUE_XML, IDENTITY_XML, AUTH_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert outcome.steps == [
        "continue-connecting",
        "identity-check",
        "credentials-entered",
        "desktop",
    ]
    assert outcome.submitted_at is not None
    shells = scripted.shells()
    assert "am start -a android.intent.action.VIEW -d vnc://127.0.0.1:5900" in shells
    assert "input tap 1425 672" in shells  # OK on "Continue connecting?"
    assert shells.count("input tap 1767 126") == 2  # identity CONTINUE, then auth CONTINUE
    assert "input text fixture" in shells
    assert "input keyevent 66" in shells
    assert "input keyevent 4" in shells
    # The password went over stdin, never as an argument of any process on the host.
    assert all("s3cr3tpw" not in " ".join(call) for call in scripted.calls)
    assert scripted.stdin == ["input text s3cr3tpw\n"]


def test_connect_reports_a_refusal_without_retyping():
    viewer, scripted = _viewer([AUTH_XML, REFUSED_XML])
    outcome = viewer.connect("fixture", "wrongpassw", sleep=lambda _s: None)
    assert not outcome.connected
    assert outcome.error == "the server refused the credentials"
    assert scripted.stdin.count("input text wrongpassw\n") == 1


def test_a_dropped_first_keystroke_is_noticed_and_the_field_retyped():
    """A freshly focused field drops its first keystroke now and then; the driver
    reads both fields back and retypes, so the server never sees 'ixture'."""
    viewer, scripted = _viewer([AUTH_XML, DESKTOP_XML])
    scripted.dropped = set()  # both fields will drop their first keystroke once
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert scripted.fields == {"UserEdit": "fixture", "PassEdit": "s3cr3tpw"}
    assert scripted.stdin.count("input text s3cr3tpw\n") == 2
    assert all("s3cr3tpw" not in " ".join(call) for call in scripted.calls)


def test_connect_times_out_when_the_desktop_never_comes(monkeypatch):
    viewer, scripted = _viewer([AUTH_XML])
    scripted.activity = DESKTOP_ACTIVITY.replace("DesktopActivity", "ConnectionChooserActivity")
    clock = iter([0.0] * 3 + [1000.0] * 10)
    monkeypatch.setattr(android_viewer.time, "monotonic", lambda: next(clock))
    outcome = viewer.connect("fixture", "s3cr3tpw", timeout=5, sleep=lambda _s: None)
    assert not outcome.connected
    assert "did not appear" in outcome.error


def test_type_secret_refuses_characters_the_shell_would_interpret():
    viewer, _scripted = _viewer([DESKTOP_XML])
    for bad in ("", "has space", "semi;colon", "quote'd", "back\\slash"):
        with pytest.raises(ValueError):
            viewer.type_secret(bad)


class RelativePointer:
    """A trackpad-style pointer: each swipe moves it by gain * delta, clamped."""

    def __init__(self, start=(960, 540), gain=1.5):
        self.position = start
        self.gain = gain

    def report(self):
        return self.position

    def apply(self, args):
        if len(args) > 6 and args[4] == "input" and args[5] == "swipe":
            x0, y0, x1, y1 = (int(v) for v in args[6:10])
            x_pos = round(self.position[0] + (x1 - x0) * self.gain)
            y_pos = round(self.position[1] + (y1 - y0) * self.gain)
            self.position = (max(0, min(1919, x_pos)), max(0, min(1079, y_pos)))


def test_move_cursor_converges_with_an_unknown_gain_and_swipes_only_inside_the_safe_zone():
    pointer = RelativePointer(gain=1.5)
    viewer, scripted = _viewer([DESKTOP_XML], pointer=pointer.report)
    original = scripted.__call__

    def apply_then_record(args, **kwargs):
        pointer.apply(args)
        return original(args, **kwargs)

    viewer.adb._run = apply_then_record
    assert viewer.move_cursor_to(960, 626, sleep=lambda _s: None)
    assert max(abs(pointer.position[0] - 960), abs(pointer.position[1] - 626)) <= 6
    for call in scripted.calls:
        if len(call) > 6 and call[4] == "input" and call[5] == "swipe":
            x0, y0, x1, y1, duration = (int(v) for v in call[6:11])
            for x_pos, y_pos in ((x0, y0), (x1, y1)):
                assert SAFE_LEFT <= x_pos <= SAFE_RIGHT
                assert SAFE_TOP <= y_pos <= SAFE_BOTTOM
            # Never short enough to be read as a tap (a tap would be a click).
            assert max(abs(x1 - x0), abs(y1 - y0)) >= min(SLOW_MIN_PX, 40) or duration >= 100


def test_click_places_the_pointer_then_taps_and_drag_is_one_double_tap_and_hold_script():
    pointer = RelativePointer(start=(960, 626), gain=1.0)
    viewer, scripted = _viewer([DESKTOP_XML], pointer=pointer.report)
    viewer.click(960, 626, sleep=lambda _s: None)
    assert "input tap 960 800" in scripted.shells()
    viewer.drag(960, 626, 1460, 626, sleep=lambda _s: None)
    script = scripted.shells()[-1]
    assert script.startswith("input tap ")
    assert "input motionevent DOWN" in script
    assert "sleep 0.7" in script
    assert script.rstrip().endswith("input motionevent UP 660 230".split()[-1])
    assert script.count("input motionevent MOVE") == 8


def test_two_finger_swipe_uses_the_touchscreen_frame_and_releases_both_slots():
    viewer, scripted = _viewer([DESKTOP_XML])
    viewer.two_finger_swipe(0, -200, sleep=lambda _s: None)
    emu = [c for c in scripted.calls if c[3:6] == ["emu", "event", "send"]]
    first = " ".join(emu[0])
    dev_x, dev_y = viewer._natural(900, 600)
    assert dev_x == round((NATURAL[0] - 600) * TOUCH_MAX / NATURAL[0])
    assert dev_y == round(900 * TOUCH_MAX / NATURAL[1])
    assert (
        f"EV_ABS:ABS_MT_SLOT:0 EV_ABS:ABS_MT_TRACKING_ID:500 EV_ABS:ABS_MT_POSITION_X:{dev_x}"
        in first
    )
    assert " ".join(emu[-1]).count("EV_ABS:ABS_MT_TRACKING_ID:4294967295") == 2
    assert all("EV_SYN:0:0" in " ".join(c) for c in emu[2:])


def test_prepare_bridges_the_port_and_landscape_is_turned_to_by_the_sensor():
    viewer, scripted = _viewer([DESKTOP_XML])
    viewer.prepare(5911)
    shells = scripted.shells()
    assert "settings put system accelerometer_rotation 1" in shells
    scripted.turn = 0  # portrait now: three quarter turns to ROTATION_90
    assert viewer.ensure_landscape(sleep=lambda _s: None)
    assert sum(1 for c in scripted.calls if c[3:5] == ["emu", "rotate"]) == 3
    assert viewer.rotation() == "ROTATION_90"
    assert ["/sdk/adb", "-s", "emulator-5554", "reverse", "tcp:5900", "tcp:5911"] in scripted.calls
    assert viewer.version() == "RealVNC Viewer for Android 4.9.4.60176 (com.realvnc.viewer.android)"


def test_screencap_rejects_anything_that_is_not_a_png(tmp_path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, b"error: device offline", b"")

    adb = Adb("/sdk/adb", "emulator-5554", {}, run=run)
    assert adb.screencap(tmp_path / "shot.png") is False
    assert not (tmp_path / "shot.png").exists()
    assert Path(tmp_path).is_dir()


UNKNOWN_DIALOG_XML = (
    '<node text="Disconnect?" resource-id="com.realvnc.viewer.android:id/alertTitle"'
    ' class="android.widget.TextView" bounds="[100,100][500,150]"/>'
    '<node text="OK" resource-id="android:id/button1" class="android.widget.Button"'
    ' bounds="[1341,540][1509,666]"/>'
)


def test_an_unexpected_dialog_is_answered_and_recorded_as_a_step():
    viewer, scripted = _viewer([UNKNOWN_DIALOG_XML, DESKTOP_XML])
    scripted.ADVANCING_TAPS = (["1425", "603"],)
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert outcome.steps[0] == "dialog:Disconnect?"


def test_desktop_visible_needs_the_desktop_activity_and_no_dialog():
    viewer, scripted = _viewer([DESKTOP_XML])
    assert viewer.desktop_visible()
    scripted.activity = DESKTOP_ACTIVITY.replace("DesktopActivity", "ConnectionChooserActivity")
    assert not viewer.desktop_visible()
    viewer, _scripted = _viewer([UNKNOWN_DIALOG_XML])
    assert not viewer.desktop_visible()


def test_connect_waits_for_the_toolbar_or_several_quiet_dumps():
    """The connecting screen is the desktop activity too: one quiet dump must not
    count as connected (a dialog can be a dump late), several must."""
    empty = '<node text="" resource-id="" class="android.view.View" bounds="[0,0][1920,1080]"/>'
    viewer, scripted = _viewer([empty])
    outcome = viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    assert outcome.connected
    assert outcome.steps == ["desktop"]
    dumps = sum(1 for call in scripted.calls if call[3:5] == ["shell", "uiautomator"])
    assert dumps >= android_viewer.QUIET_DUMPS


def test_typing_and_the_unlock_secret_and_the_lifecycle_calls():
    viewer, scripted = _viewer([DESKTOP_XML])
    viewer.type_text("nonce-2026")
    viewer.type_text("\n")
    with pytest.raises(ValueError):
        viewer.type_text("no spaces allowed")
    viewer.type_unlock_secret("s3cr3tpw")
    viewer.disconnect()
    shells = scripted.shells()
    assert "input text nonce-2026" in shells
    assert shells.count("input keyevent 66") == 2
    assert "input keyevent 62 67" in shells  # warm-up key and its Backspace
    assert scripted.stdin == ["input text s3cr3tpw\n"]
    assert "am force-stop com.realvnc.viewer.android" in shells


def test_secret_typing_reports_a_failed_shell():
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "closed")

    adb = Adb("/sdk/adb", "emulator-5554", {}, run=run)
    viewer = AndroidViewer(adb, pointer_report=lambda: None)
    with pytest.raises(OSError, match="typing the secret failed"):
        viewer.type_secret("s3cr3tpw")


def test_placement_failures_are_errors_not_silent_taps():
    viewer, scripted = _viewer([DESKTOP_XML], pointer=lambda: None)  # never a report
    for action in (
        lambda: viewer.click(960, 626, sleep=lambda _s: None),
        lambda: viewer.drag(460, 250, 1460, 250, sleep=lambda _s: None),
        lambda: viewer.scroll(960, 250, sleep=lambda _s: None),
    ):
        with pytest.raises(OSError, match="could not place the pointer"):
            action()
    # Without a report the loop provokes one with minimal slow swipes, never a tap.
    assert not any("input tap" in s for s in scripted.shells())
    assert any(s.startswith("input swipe") for s in scripted.shells())


def test_scroll_places_the_pointer_then_sends_the_two_finger_gesture():
    pointer = RelativePointer(start=(960, 250), gain=1.0)
    viewer, scripted = _viewer([DESKTOP_XML], pointer=pointer.report)
    viewer.scroll(960, 250, sleep=lambda _s: None)
    assert any(c[3:6] == ["emu", "event", "send"] for c in scripted.calls)


def test_console_refusals_and_missing_controls_are_errors():
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "KO: invalid event", "")

    adb = Adb("/sdk/adb", "emulator-5554", {}, run=run)
    viewer = AndroidViewer(adb, pointer_report=lambda: None)
    with pytest.raises(OSError, match="emulator console refused"):
        viewer._emu_send("EV_SYN:0:0")
    with pytest.raises(OSError, match="not on screen"):
        viewer._tap(None)


def test_screencap_writes_a_png(tmp_path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, b"\x89PNG\r\n\x1a\nrest", b"")

    adb = Adb("/sdk/adb", "emulator-5554", {}, run=run)
    assert adb.screencap(tmp_path / "shot.png") is True
    assert (tmp_path / "shot.png").read_bytes().startswith(b"\x89PNG")
    viewer = AndroidViewer(adb, pointer_report=lambda: None)
    assert viewer.screenshot(tmp_path / "again.png") is True


INFO_XML = (
    '<node text="1920 x 1080"'
    ' resource-id="com.realvnc.viewer.android:id/text_view_desktop_size_details"'
    ' class="android.widget.TextView" bounds="[1012,218][1914,328]"/>'
    '<node text="Desktop size" resource-id="com.realvnc.viewer.android:id/text_view_desktop_size"'
    ' class="android.widget.TextView" bounds="[1012,328][1914,432]"/>'
)


def test_desktop_size_comes_from_the_apps_information_screen_and_closes_it():
    viewer, scripted = _viewer([DESKTOP_XML, INFO_XML, DESKTOP_XML])
    assert viewer.desktop_size(sleep=lambda _s: None) == (1920, 1080)
    inputs = [shell for shell in scripted.shells() if shell.startswith("input ")]
    assert f"input tap {android_viewer.INFO_BUTTON[0]} {android_viewer.INFO_BUTTON[1]}" in inputs
    assert inputs[-1] == "input keyevent 4"  # Back closes the screen again
    assert inputs.count("input keyevent 4") == 1  # and it was seen to close: no second Back


def test_desktop_size_never_taps_without_the_toolbar():
    """Without the toolbar the information button's spot is bare desktop, and a tap
    there is a click the app sends to the server."""
    viewer, scripted = _viewer([""])
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]

    assert viewer.desktop_size(sleep=lambda _s: None, clock=clock) is None
    assert not any(shell.startswith("input ") for shell in scripted.shells())


def test_desktop_size_waits_for_a_toolbar_that_a_relayout_hid_from_the_dump():
    """Right after a mode change uiautomator answers with an empty hierarchy; the
    toolbar is still there a moment later and the tap must wait for it."""
    viewer, scripted = _viewer(["", DESKTOP_XML, INFO_XML, DESKTOP_XML])
    scripted.advance_after_dumps = 1  # the first dump is the empty one
    slept = []
    assert viewer.desktop_size(sleep=slept.append, clock=lambda: 0.0) == (1920, 1080)
    assert slept
    assert f"input tap {android_viewer.INFO_BUTTON[0]}" in " ".join(scripted.shells())


def test_the_ra2_glitch_dialog_is_a_transient_error():
    glitch = (
        '<node text="RSA decrypt/check error: bad length 255 (should be 256)"'
        ' resource-id="android:id/message" class="android.widget.TextView"'
        ' bounds="[440,440][1480,540]"/>'
    )
    viewer, _scripted = _viewer([glitch])
    assert viewer.transient_error() is True
    viewer, _scripted = _viewer([DESKTOP_XML])
    assert viewer.transient_error() is False


def test_desktop_size_waits_for_an_information_screen_that_opens_late():
    viewer, scripted = _viewer([DESKTOP_XML, DESKTOP_XML, INFO_XML, DESKTOP_XML])
    # Two dumps precede the tap; the screen shows up only on the second dump after it.
    scripted.advance_after_dumps = 3
    slept = []
    assert viewer.desktop_size(sleep=slept.append, clock=lambda: 0.0) == (1920, 1080)
    assert slept, "the driver polled instead of reading once"


def test_desktop_size_gives_up_when_the_screen_never_opens():
    viewer, scripted = _viewer([DESKTOP_XML, DESKTOP_XML])
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]

    assert viewer.desktop_size(sleep=lambda _s: None, clock=clock) is None
    inputs = [shell for shell in scripted.shells() if shell.startswith("input ")]
    assert inputs[-1] == "input keyevent 4"  # still closes whatever the tap opened


def test_back_is_pressed_only_while_the_keyboard_is_up():
    viewer, scripted = _viewer([AUTH_XML, DESKTOP_XML])
    viewer.connect("fixture", "s3cr3tpw", sleep=lambda _s: None)
    shells = scripted.shells()
    assert shells.count("input keyevent 4") == 1
    # ...and it came after the password was typed, before the CONTINUE tap.
    assert shells.index("input keyevent 4") < shells.index("input tap 1767 126")
    viewer, scripted = _viewer([DESKTOP_XML])
    viewer._hide_ime(lambda _s: None)  # no keyboard up: no Back
    assert "input keyevent 4" not in scripted.shells()


def test_a_masked_password_field_must_show_one_bullet_per_character():
    holds = android_viewer._password_field_holds
    assert holds("secret-pw", "secret-pw")
    assert holds("•" * 9, "secret-pw")
    assert holds("*" * 9, "secret-pw")
    assert not holds("•" * 8, "secret-pw"), "a dropped keystroke shows as one bullet fewer"
    assert not holds("", "secret-pw")
    assert not holds("secret-p", "secret-pw")


def test_each_credential_attempt_is_noted_without_the_secret():
    viewer, _scripted = _viewer([AUTH_XML, DESKTOP_XML])
    outcome = viewer.connect("fixture", "secret-pw", sleep=lambda _s: None)
    assert outcome.connected
    assert outcome.notes
    assert outcome.notes[0].startswith("credentials attempt 1: username field")
    assert "9/9 characters" in outcome.notes[0]
    assert "secret-pw" not in " ".join(outcome.notes)


def test_the_keyboard_is_closed_again_when_one_back_did_not_take():
    viewer, scripted = _viewer([AUTH_XML, DESKTOP_XML])
    scripted.sticky_ime = 1
    outcome = viewer.connect("fixture", "secret-pw", sleep=lambda _s: None)
    assert outcome.connected, outcome
    backs = [s for s in scripted.shells() if s == "input keyevent 4"]
    assert len(backs) >= 2
    assert scripted.fields == {"UserEdit": "fixture", "PassEdit": "secret-pw"}


def test_the_keyboards_extract_view_in_the_tree_counts_as_keyboard_up():
    """The input method service has reported the keyboard down while its full-screen
    extract view was still in the tree; the dump decides."""
    extract = (
        '<node text="Password" resource-id="" class="android.inputmethodservice.ExtractEditText"'
        ' bounds="[0,0][1920,300]"/>'
    )
    viewer, scripted = _viewer([extract, AUTH_XML, DESKTOP_XML])
    scripted.advance_after_dumps = 1  # the extract view is what the first dump shows
    assert viewer._ime_up() is True
    viewer, _scripted = _viewer([DESKTOP_XML])
    assert viewer._ime_up() is False


def test_the_information_button_is_found_where_the_toolbar_was_left():
    """The app remembers where its toolbar was dragged; tapping the fresh-install
    position then opens nothing, and the desktop size can never be read."""
    moved = (
        '<node text="" resource-id="com.realvnc.viewer.android:id/menu_pin"'
        ' class="android.widget.ImageButton" bounds="[58,894][172,1009]"/>'
        '<node text="" resource-id="com.realvnc.viewer.android:id/menu_information"'
        ' class="android.widget.ImageButton" bounds="[400,894][514,1009]"/>'
    )
    viewer, scripted = _viewer([moved])
    viewer._open_info_screen(sleep=lambda _s: None, clock=iter([0.0, 0.0, 999.0]).__next__)
    assert "input tap 457 951" in scripted.shells()
    assert "input tap 457 129" not in scripted.shells()
