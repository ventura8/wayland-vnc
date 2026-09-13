"""Conservative pixel checks for the synthetic qualification scene."""

from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

TARGETS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))


@dataclass(frozen=True)
class SceneEvidence:
    width: int
    height: int
    matched_row: int
    segment_centers: tuple[int, int, int, int]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GestureEvidence:
    width: int
    height: int
    matched_row: int
    scroll_center: int
    drag_center: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InputEvidence:
    width: int
    height: int
    matched_row: int
    keyboard_center: int
    pointer_center: int

    def as_dict(self) -> dict:
        return asdict(self)


def _near(pixel: tuple[int, ...], target: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(pixel[channel] - target[channel]) <= tolerance for channel in range(3))


def verify_scene(path: Path, *, tolerance: int = 24) -> SceneEvidence:
    """Find a wide, ordered red/green/blue/white scene row or fail closed."""
    if not 0 <= tolerance <= 64:
        raise ValueError("Color tolerance must be between 0 and 64")
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width < 400 or height < 200:
        raise ValueError("Screenshot is too small to be qualification evidence")
    step_y = max(1, height // 120)
    minimum = width // 12
    for y_pos in range(0, height, step_y):
        centers: list[int] = []
        cursor = 0
        for target in TARGETS:
            while cursor < width and not _near(image.getpixel((cursor, y_pos)), target, tolerance):
                cursor += 1
            start = cursor
            while cursor < width and _near(image.getpixel((cursor, y_pos)), target, tolerance):
                cursor += 1
            if cursor - start < minimum:
                break
            centers.append((start + cursor - 1) // 2)
        if len(centers) == len(TARGETS):
            return SceneEvidence(width, height, y_pos, tuple(centers))
    raise ValueError("Screenshot does not contain ordered RGBW qualification targets")


def _find_band(
    path: Path, targets: tuple[tuple[int, int, int], ...], tolerance: int, failure: str
) -> tuple[int, int, int, list[int]]:
    """Find one row holding the ordered wide color bands; fail closed otherwise."""
    if not 0 <= tolerance <= 64:
        raise ValueError("Color tolerance must be between 0 and 64")
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width < 400 or height < 200:
        raise ValueError("Screenshot is too small to be input evidence")
    minimum = width // 6
    for y_pos in range(0, height, max(1, height // 120)):
        centers = []
        cursor = 0
        for target in targets:
            while cursor < width and not _near(image.getpixel((cursor, y_pos)), target, tolerance):
                cursor += 1
            start = cursor
            while cursor < width and _near(image.getpixel((cursor, y_pos)), target, tolerance):
                cursor += 1
            if cursor - start < minimum:
                break
            centers.append((start + cursor - 1) // 2)
        if len(centers) == len(targets):
            return width, height, y_pos, centers
    raise ValueError(failure)


def verify_input_markers(path: Path, *, tolerance: int = 24) -> InputEvidence:
    """Require cyan keyboard and magenta pointer acknowledgements in order."""
    width, height, row, centers = _find_band(
        path,
        ((0, 255, 255), (255, 0, 255)),
        tolerance,
        "Screenshot lacks keyboard and pointer acknowledgement markers",
    )
    return InputEvidence(width, height, row, centers[0], centers[1])


def verify_gesture_markers(path: Path, *, tolerance: int = 24) -> GestureEvidence:
    """Require yellow scroll and orange drag acknowledgements in order."""
    width, height, row, centers = _find_band(
        path,
        ((255, 255, 0), (255, 170, 0)),
        tolerance,
        "Screenshot lacks scroll and drag acknowledgement markers",
    )
    return GestureEvidence(width, height, row, centers[0], centers[1])


MARKERS = {
    "keyboard": (0, 255, 255),
    "pointer": (255, 0, 255),
    "scroll": (255, 255, 0),
    "drag": (255, 170, 0),
}


def detect_markers(path: Path, *, tolerance: int = 24) -> set[str]:
    """Report which acknowledgement bands are present, each judged on its own."""
    if not 0 <= tolerance <= 64:
        raise ValueError("Color tolerance must be between 0 and 64")
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    # width // 6 is 0 for a very small image, and a run length of 0 is satisfied
    # by the first pixel of every row, so every marker would be "found" on a
    # blank capture. An image that cannot hold a band is not evidence either way.
    if width < 6:
        raise ValueError(f"image is too narrow to carry a marker band: {width}px")
    minimum = width // 6
    found: set[str] = set()
    for y_pos in range(0, height, max(1, height // 120)):
        raw = image.crop((0, y_pos, width, y_pos + 1)).tobytes()
        for name, target in MARKERS.items():
            if name in found:
                continue
            run = 0
            for offset in range(0, len(raw), 3):
                pixel = raw[offset : offset + 3]
                run = run + 1 if _near(pixel, target, tolerance) else 0
                if run >= minimum:
                    found.add(name)
                    break
    return found
