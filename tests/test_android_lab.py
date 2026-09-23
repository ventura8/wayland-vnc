"""The isolated lab AVD's configuration.

avdmanager's pixel_2 profile writes `hw.keyboard=no`, and `adb shell input text`
injects through the hardware-keyboard path: with no hardware keyboard it reports
success and types nothing. A lab AVD created that way reaches the viewer's credential
dialog, fails to fill either field, and every scenario then fails on "no valid frame"
with nothing in the logs pointing at the keyboard. That is what these assertions are
for.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("android_lab", REPO / "scripts" / "android-lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)

# config.ini as avdmanager writes it for the profile this lab uses.
AS_CREATED = (
    "avd.ini.encoding=UTF-8\n"
    "hw.keyboard=no\n"
    "hw.lcd.density=420\n"
    "PlayStore.enabled=false\n"
    "tag.id=google_apis_playstore\n"
)


def test_the_hardware_keyboard_is_turned_on():
    assert "hw.keyboard=yes\n" in lab.configure(AS_CREATED)
    assert "hw.keyboard=no\n" not in lab.configure(AS_CREATED)


def test_the_play_store_is_turned_on():
    assert "PlayStore.enabled=true\n" in lab.configure(AS_CREATED)
    assert "PlayStore.enabled=false\n" not in lab.configure(AS_CREATED)


def test_a_setting_the_profile_omits_entirely_is_added():
    """Neither key is guaranteed to be present; an absent one must not be skipped."""
    configured = lab.configure("avd.ini.encoding=UTF-8\ntag.id=google_apis_playstore\n")
    assert "hw.keyboard=yes\n" in configured
    assert "PlayStore.enabled=true\n" in configured


def test_settings_already_correct_are_left_alone():
    """Re-running must not duplicate a line; a config.ini with a key twice is
    ambiguous and the emulator reads whichever it reads."""
    once = lab.configure(AS_CREATED)
    assert lab.configure(once) == once
    assert once.count("hw.keyboard=") == 1
    assert once.count("PlayStore.enabled=") == 1


def test_the_no_spelling_of_the_play_store_flag_is_handled():
    """avdmanager has written both `false` and `no` for this key."""
    configured = lab.configure(
        AS_CREATED.replace("PlayStore.enabled=false", "PlayStore.enabled=no")
    )
    assert "PlayStore.enabled=true\n" in configured
    assert configured.count("PlayStore.enabled=") == 1
