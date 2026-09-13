from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from wayland_vnc.image_evidence import verify_input_markers, verify_scene


def save_scene(path: Path, colors=None, size=(800, 400)):
    colors = colors or ("red", "lime", "blue", "white")
    image = Image.new("RGB", size, "black")
    drawing = ImageDraw.Draw(image)
    for index, color in enumerate(colors):
        drawing.rectangle((index * 200, 40, (index + 1) * 200 - 1, 240), fill=color)
    image.save(path)


def add_input_markers(path: Path, keyboard=True, pointer=True):
    image = Image.open(path)
    drawing = ImageDraw.Draw(image)
    if keyboard:
        drawing.rectangle((0, 200, 399, 239), fill="cyan")
    if pointer:
        drawing.rectangle((400, 200, 799, 239), fill="magenta")
    image.save(path)


def test_verifies_ordered_scene(tmp_path):
    screenshot = tmp_path / "scene.png"
    save_scene(screenshot)
    evidence = verify_scene(screenshot)
    assert evidence.width == 800
    assert evidence.height == 400
    assert evidence.segment_centers == (99, 299, 499, 699)
    assert 40 <= evidence.as_dict()["matched_row"] <= 240


@pytest.mark.parametrize("colors", [("black",) * 4, ("red", "blue", "lime", "white")])
def test_rejects_black_or_reordered_scene(tmp_path, colors):
    screenshot = tmp_path / "bad.png"
    save_scene(screenshot, colors)
    with pytest.raises(ValueError, match="RGBW"):
        verify_scene(screenshot)


def test_rejects_small_image(tmp_path):
    screenshot = tmp_path / "small.png"
    save_scene(screenshot, size=(200, 100))
    with pytest.raises(ValueError, match="too small"):
        verify_scene(screenshot)


@pytest.mark.parametrize("tolerance", [-1, 65])
def test_rejects_invalid_tolerance(tmp_path, tolerance):
    screenshot = tmp_path / "scene.png"
    save_scene(screenshot)
    with pytest.raises(ValueError, match="tolerance"):
        verify_scene(screenshot, tolerance=tolerance)


def test_verifies_keyboard_and_pointer_markers(tmp_path):
    screenshot = tmp_path / "input.png"
    save_scene(screenshot)
    add_input_markers(screenshot)
    evidence = verify_input_markers(screenshot)
    assert evidence.keyboard_center == 199
    assert evidence.pointer_center == 599
    assert evidence.as_dict()["matched_row"] == 201


@pytest.mark.parametrize("keyboard,pointer", [(False, False), (True, False), (False, True)])
def test_rejects_missing_input_markers(tmp_path, keyboard, pointer):
    screenshot = tmp_path / "input.png"
    save_scene(screenshot)
    add_input_markers(screenshot, keyboard, pointer)
    with pytest.raises(ValueError, match="acknowledgement"):
        verify_input_markers(screenshot)


def test_input_rejects_small_image_and_bad_tolerance(tmp_path):
    screenshot = tmp_path / "small.png"
    save_scene(screenshot, size=(200, 100))
    with pytest.raises(ValueError, match="too small"):
        verify_input_markers(screenshot)
    with pytest.raises(ValueError, match="tolerance"):
        verify_input_markers(screenshot, tolerance=65)


def add_gesture_markers(path: Path, scroll=True, drag=True):
    image = Image.open(path)
    drawing = ImageDraw.Draw(image)
    if scroll:
        drawing.rectangle((0, 300, 399, 339), fill=(255, 255, 0))
    if drag:
        drawing.rectangle((400, 300, 799, 339), fill=(255, 170, 0))
    image.save(path)


def test_gesture_markers_are_independent_of_input_markers(tmp_path):
    from wayland_vnc.image_evidence import verify_gesture_markers

    screenshot = tmp_path / "gestures.png"
    save_scene(screenshot)
    with pytest.raises(ValueError, match="scroll and drag"):
        verify_gesture_markers(screenshot)
    add_gesture_markers(screenshot, drag=False)
    with pytest.raises(ValueError, match="scroll and drag"):
        verify_gesture_markers(screenshot)
    add_gesture_markers(screenshot)
    evidence = verify_gesture_markers(screenshot)
    assert (evidence.scroll_center, evidence.drag_center) == (199, 599)
    assert evidence.as_dict()["matched_row"] == 300
    with pytest.raises(ValueError, match="keyboard and pointer"):
        verify_input_markers(screenshot)
    with pytest.raises(ValueError, match="tolerance"):
        verify_gesture_markers(screenshot, tolerance=99)


def test_detect_markers_reports_each_band_independently(tmp_path):
    from wayland_vnc.image_evidence import detect_markers

    screenshot = tmp_path / "markers.png"
    save_scene(screenshot)
    assert detect_markers(screenshot) == set()
    add_input_markers(screenshot, pointer=False)
    assert detect_markers(screenshot) == {"keyboard"}
    add_gesture_markers(screenshot, scroll=False)
    assert detect_markers(screenshot) == {"keyboard", "drag"}
    with pytest.raises(ValueError):
        detect_markers(screenshot, tolerance=-1)
