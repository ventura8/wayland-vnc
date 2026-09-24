"""Conservative pixel checks for the synthetic qualification scene."""

from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

TARGETS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
TOLERANCE_RANGE_ERROR = "Color tolerance must be between 0 and 64"


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


def _evidence_image(path: Path, tolerance: int, too_small: str) -> tuple[Image.Image, int, int]:
    """The screenshot as RGB, once it is big enough to carry a band at all."""
    if not 0 <= tolerance <= 64:
        raise ValueError(TOLERANCE_RANGE_ERROR)
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width < 400 or height < 200:
        raise ValueError(too_small)
    return image, width, height


def _row_centers(
    image: Image.Image,
    y_pos: int,
    targets: tuple[tuple[int, int, int], ...],
    tolerance: int,
    minimum: int,
) -> list[int]:
    """The centre of each target's run along one row, in order. The list stops short
    at the first target whose run is missing or too narrow, which is how the caller
    tells a complete row from a partial one."""
    width = image.size[0]
    centers: list[int] = []
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
    return centers


@dataclass(frozen=True)
class _Band:
    """One kind of evidence band: the colors it is made of, how much of the width each
    must occupy, and what to say when the screenshot does not carry it."""

    targets: tuple[tuple[int, int, int], ...]
    share: int
    failure: str
    too_small: str


SCENE_BAND = _Band(
    TARGETS,
    12,
    "Screenshot does not contain ordered RGBW qualification targets",
    "Screenshot is too small to be qualification evidence",
)
INPUT_BAND = _Band(
    ((0, 255, 255), (255, 0, 255)),
    6,
    "Screenshot lacks keyboard and pointer acknowledgement markers",
    "Screenshot is too small to be input evidence",
)
GESTURE_BAND = _Band(
    ((255, 255, 0), (255, 170, 0)),
    6,
    "Screenshot lacks scroll and drag acknowledgement markers",
    "Screenshot is too small to be input evidence",
)


def _find_band(path: Path, band: _Band, tolerance: int) -> tuple[int, int, int, list[int]]:
    """Find one row holding the band's ordered colors; fail closed otherwise."""
    image, width, height = _evidence_image(path, tolerance, band.too_small)
    minimum = width // band.share
    for y_pos in range(0, height, max(1, height // 120)):
        centers = _row_centers(image, y_pos, band.targets, tolerance, minimum)
        if len(centers) == len(band.targets):
            return width, height, y_pos, centers
    raise ValueError(band.failure)


def verify_scene(path: Path, *, tolerance: int = 24) -> SceneEvidence:
    """Find a wide, ordered red/green/blue/white scene row or fail closed."""
    width, height, row, centers = _find_band(path, SCENE_BAND, tolerance)
    return SceneEvidence(width, height, row, tuple(centers))


def verify_input_markers(path: Path, *, tolerance: int = 24) -> InputEvidence:
    """Require cyan keyboard and magenta pointer acknowledgements in order."""
    width, height, row, centers = _find_band(path, INPUT_BAND, tolerance)
    return InputEvidence(width, height, row, centers[0], centers[1])


def verify_gesture_markers(path: Path, *, tolerance: int = 24) -> GestureEvidence:
    """Require yellow scroll and orange drag acknowledgements in order."""
    width, height, row, centers = _find_band(path, GESTURE_BAND, tolerance)
    return GestureEvidence(width, height, row, centers[0], centers[1])


MARKERS = {
    "keyboard": (0, 255, 255),
    "pointer": (255, 0, 255),
    "scroll": (255, 255, 0),
    "drag": (255, 170, 0),
}


def _row_holds_run(raw: bytes, target: tuple[int, int, int], tolerance: int, minimum: int) -> bool:
    """Whether one packed RGB row carries an unbroken run of `minimum` near-target
    pixels -- the shape a marker band leaves behind."""
    run = 0
    for offset in range(0, len(raw), 3):
        run = run + 1 if _near(raw[offset : offset + 3], target, tolerance) else 0
        if run >= minimum:
            return True
    return False


def detect_markers(path: Path, *, tolerance: int = 24) -> set[str]:
    """Report which acknowledgement bands are present, each judged on its own."""
    if not 0 <= tolerance <= 64:
        raise ValueError(TOLERANCE_RANGE_ERROR)
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
            if name not in found and _row_holds_run(raw, target, tolerance, minimum):
                found.add(name)
    return found
