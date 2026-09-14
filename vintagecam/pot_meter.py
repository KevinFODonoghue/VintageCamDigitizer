"""
The pot meter: is this pot at its best position?

Point the camera at an evenly lit white card that fills the frame.  Then, for
any pot: turn it fully clockwise and measure, turn it fully anticlockwise and
measure, and turn it until the light goes green.

**What it measures.**  The picture is divided into a 20 × 15 grid (square cells
on a 4:3 picture; the corners count as much as the centre) and each cell's
average colour is compared with what it should be: an even, neutral white as
bright as the card is on average, or, after zeroing with a phone photo, that
photo's colour for the cell.  How far the whole grid is from its target is one
number, the root-mean-square of every cell's distance from it, in RGB code
values (0–255).

**Where the grid sits.**  The Elgato's 720-pixel line isn't all picture.  In
every recording of the camera, whatever it showed, columns 0–17 are line
blanking (black: they read 1–20), the picture's edge rises at column 18 and
overshoots at 19, and the last two columns carry a spike from the end of the
line.  A grid starting at column 14 took 5 of those columns into each left-hand
cell, and those cells read 12–13% too dark (on a white card, about 30 code
values), with a sixth of their tint lost.  So the grid starts at column 22,
where the edge has settled (the cells then read within 0.6% of the picture),
and stops at 716.  Every one of the 480 lines is picture, so the grid runs the
full height.

**Why white at the card's own brightness, not pure white (255)?**  Measured
against pure white, a card that isn't lit to exactly full white is off by the
same amount everywhere, and that one big offset takes over: any pot that also
brightens the picture slightly gets steered towards "brighter" instead of
"whiter".  (In testing, a tenth of a code value of brightness change moved the
best spot by 8% of the pot's travel.)  So tint and unevenness count, and the
light level and the iris don't.  The price: a pot that only changes the overall
brightness, like a gain pot, can't be judged this way.

**Zeroing with a phone photo.**  Outside a studio a white card rarely looks
pure white: room light is warm or cool, and brighter on one side.  A phone
corrects for its surroundings far better than a 1984 camera, so a phone photo of
the card, taken from where the camera sits, shows how the card really looks.
Loaded as the reference (``reference_from_rgb``), it replaces plain white as
the target: each cell aims for the photo's colour there, scaled to the camera's
brightness, so the phone's exposure doesn't matter either.

**How two measurements find the best position.**  Turning a pot moves each
cell's colour roughly in proportion to how far it's turned (a trimmer pot sets
the size of a correction).  So the two ends give every cell a straight path, and
least squares finds the point on it where the grid as a whole is closest to its
target.  While you turn the pot, the live picture is placed on the same path,
which says where the pot is now; within 3% of the pot's travel of the best
point, the light is green.  If the best point lies beyond one end, that end is
the best the pot can do, and it's green there.  Near the ends a colour can clip,
which bends the path a little; then the green spot is approximate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .color import BLACK_Y, KB, KR, MAX_C, MIN_C, NEUTRAL_C, WHITE_Y, split_uyvy

GRID_COLUMNS, GRID_ROWS = 20, 15  # square cells on a 4:3 picture
#: Where the grid sits in the frame, as fractions of its width and height: clear
#: of the Elgato's line blanking and the picture's edge (columns 0–21 of 720) and
#: of the spike at the end of the line (716–719).  Every line is picture, so it
#: runs the full height (see "Where the grid sits").
GRID_LEFT, GRID_RIGHT = 22 / 720, 716 / 720
GRID_TOP, GRID_BOTTOM = 0.0, 1.0
#: The grid area's shape on screen, which phone photos are cropped to.  (NTSC
#: pixels are 10/11 as wide as they are tall; PAL's 12/11 on 576 lines comes out
#: the same.)
GRID_ASPECT = (GRID_RIGHT - GRID_LEFT) * 720 * (10 / 11) / ((GRID_BOTTOM - GRID_TOP) * 480)
MEASURE_FRAMES = 30  # each end is measured over a second of frames
LIVE_FRAMES = 10  # the live reading averages a third of a second: steady, yet quick to follow a turn
TOLERANCE = 0.03  # green within 3% of the pot's travel of the best position
MIN_EFFECT = 1.0  # a pot that changes the grid less than this (RMS, code values) can't be judged
DARK_WARNING = 120.0  # an average below this: the camera probably isn't looking at a lit white card


def grid_edges(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    """The grid's cell boundaries in pixels: GRID_COLUMNS + 1 x positions and GRID_ROWS + 1 y positions."""
    xs = np.round(np.linspace(GRID_LEFT * width, GRID_RIGHT * width, GRID_COLUMNS + 1)).astype(int)
    ys = np.round(np.linspace(GRID_TOP * height, GRID_BOTTOM * height, GRID_ROWS + 1)).astype(int)
    return xs, ys


def _cell_means(plane: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """The average of ``plane`` over each grid cell: shape (rows, columns)."""
    block = plane[ys[0]:ys[-1], xs[0]:xs[-1]].astype(np.float64)
    sums = np.add.reduceat(np.add.reduceat(block, ys[:-1] - ys[0], axis=0), xs[:-1] - xs[0], axis=1)
    return sums / np.outer(np.diff(ys), np.diff(xs))


def _to_rgb(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    """BT.601 limited-range Y'CbCr → full-range RGB, as floats and not clipped (color.ycbcr_to_rgb's formula)."""
    yf = (y - BLACK_Y) * (255.0 / (WHITE_Y - BLACK_Y))
    pb = (cb - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    pr = (cr - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    r = yf + 2 * (1 - KR) * pr
    b = yf + 2 * (1 - KB) * pb
    g = (yf - KR * r - KB * b) / (1 - KR - KB)
    return np.stack([r, g, b], axis=-1)


def cell_colours(uyvy: np.ndarray) -> np.ndarray:
    """Each grid cell's average colour as full-range RGB: shape (rows, columns, 3).

    Averaging Y', Cb and Cr and then converting is the same as averaging RGB,
    since the conversion is a straight-line (affine) formula.  Cb and Cr come one
    per pair of pixels (4:2:2), so their cell edges are at half the x positions.
    """
    y, cb, cr = split_uyvy(uyvy)
    xs, ys = grid_edges(uyvy.shape[0], uyvy.shape[1] // 2)
    return _to_rgb(_cell_means(y, xs, ys), _cell_means(cb, xs // 2, ys), _cell_means(cr, xs // 2, ys))


def reference_from_rgb(rgb: np.ndarray, aspect: float = GRID_ASPECT) -> np.ndarray:
    """A phone photo (height, width, 3; RGB 0–255) → the colour each grid cell should have: (rows, columns, 3).

    The photo should show what the camera's picture shows.  It's cropped, centred,
    to the grid area's shape and split into the same 20 × 15 cells.  Raises
    ValueError (with a reason to show) if it can't serve.
    """
    height, width = rgb.shape[:2]
    if height > width:
        raise ValueError("it's in portrait. Hold the phone sideways (landscape), like the camera's picture")
    if width / height > aspect:
        keep = round(height * aspect)
        rgb = rgb[:, (width - keep) // 2:(width - keep) // 2 + keep]
    else:
        keep = round(width / aspect)
        rgb = rgb[(height - keep) // 2:(height - keep) // 2 + keep]
    height, width = rgb.shape[:2]
    xs = np.round(np.linspace(0, width, GRID_COLUMNS + 1)).astype(int)
    ys = np.round(np.linspace(0, height, GRID_ROWS + 1)).astype(int)
    reference = np.stack([_cell_means(rgb[..., k], xs, ys) for k in range(3)], axis=-1)
    if reference.mean() < 20:
        raise ValueError("it's almost black. Photograph the lit white card")
    return reference


def off_target(colours: np.ndarray, reference: np.ndarray | None = None) -> np.ndarray:
    """How far each cell is from what it should be, in R, G and B.

    Without a reference, the target is an even, neutral white as bright as the
    picture's average.  With one, it's the reference's colour for the cell,
    scaled so the reference's average brightness matches the picture's.  (Plain
    white is the same thing with an evenly white reference.)
    """
    if reference is None:
        return colours - colours.mean()
    return colours - colours.mean() * reference / reference.mean()


def distance_from_target(colours: np.ndarray, reference: np.ndarray | None = None) -> float:
    """How far the grid is from its target: the root-mean-square of each cell's distance (see off_target)."""
    return float(np.sqrt(np.mean(np.sum(off_target(colours, reference) ** 2, axis=-1))))


@dataclass(frozen=True)
class Judgement:
    at_best: bool | None
    """True: at the best position.  False: not yet.  None: this pot can't be judged."""
    position: float | None = None
    """Where the pot seems to be: 0 = fully anticlockwise, 1 = fully clockwise."""
    best: float | None = None
    """Where the grid comes closest to its target, on the same scale."""
    note: str = ""


def judge(current: np.ndarray, ccw: np.ndarray, cw: np.ndarray, tolerance: float = TOLERANCE,
          reference: np.ndarray | None = None) -> Judgement:
    """Is the pot at its best position?  ``current``, ``ccw`` and ``cw`` are cell_colours() arrays."""
    start = off_target(ccw, reference).ravel()
    step = off_target(cw, reference).ravel() - start  # how every cell's distance moves from one end to the other
    travel = float(step @ step)
    if np.sqrt(travel / step.size) < MIN_EFFECT:
        return Judgement(None, note="This pot hardly changes the colours or how even they are (perhaps only the "
                                    "overall brightness), so the meter can't judge it.")
    # Distance at position p: start + p·step.  It's smallest at:
    best = float(np.clip(-(start @ step) / travel, 0.0, 1.0))
    position = float(((off_target(current, reference).ravel() - start) @ step) / travel)  # the live picture, placed
    note = ""
    if best in (0.0, 1.0):
        end = "clockwise" if best == 1.0 else "anticlockwise"
        goal = "match the photo" if reference is not None else "white"
        note = f"The best this pot can do is fully {end}: it can't bring the card all the way to {goal}."
    return Judgement(abs(min(max(position, 0.0), 1.0) - best) <= tolerance, position, best, note)


class PotMeter:
    """The live picture's cell colours and the pot's two measured ends.  Use it from one thread only."""

    def __init__(self, live_frames: int = LIVE_FRAMES, measure_frames: int = MEASURE_FRAMES) -> None:
        self.measure_frames = measure_frames
        self.ends: dict[str, np.ndarray | None] = {"cw": None, "ccw": None}
        self.measuring = ""
        """"cw" or "ccw" while that end is being measured, otherwise ""."""
        self.reference: np.ndarray | None = None
        """What each cell should look like (reference_from_rgb), or None for plain white."""
        self._live: deque[np.ndarray] = deque(maxlen=live_frames)
        self._collected: list[np.ndarray] = []

    def add(self, uyvy: np.ndarray) -> None:
        """Take one frame: the card's raw UYVY bytes (``CapturedFrame.uyvy``)."""
        colours = cell_colours(uyvy)
        self._live.append(colours)
        if self.measuring:
            self._collected.append(colours)
            if len(self._collected) >= self.measure_frames:
                self.ends[self.measuring] = np.mean(self._collected, axis=0)
                self.measuring, self._collected = "", []

    def measure(self, end: str) -> None:
        """Start measuring one end of the pot's travel: "cw" (fully clockwise) or "ccw"."""
        if end not in self.ends:
            raise ValueError("end must be 'cw' or 'ccw'")
        self.measuring, self._collected = end, []

    def reset(self) -> None:
        """Forget both ends, for the next pot.  (The reference stays: it's the room, not the pot.)"""
        self.ends = {"cw": None, "ccw": None}
        self.measuring, self._collected = "", []

    @property
    def progress(self) -> float:
        return len(self._collected) / self.measure_frames if self.measuring else 0.0

    def live(self) -> np.ndarray | None:
        return np.mean(self._live, axis=0) if self._live else None

    def judge(self) -> Judgement | None:
        """None until both ends are measured."""
        current, ccw, cw = self.live(), self.ends["ccw"], self.ends["cw"]
        if current is None or ccw is None or cw is None:
            return None
        return judge(current, ccw, cw, reference=self.reference)
