"""
Pot Assist: which adjustment pot to turn, and which way, to zero the camera's
colour shading and dynamic focus errors.

This is a port of ``pot_metrics.py`` (the drop-in analysis for the RCA CKC021)
into the app.  The geometry, the four terms, the pot map and the turn-direction
logic are unchanged.  What's different, and why:

* It reads colour straight from the card's own Cb and Cr samples instead of
  going through OpenCV's BGR and YUV conversions.  Same signs, no detour, and
  no OpenCV (which isn't part of the app).
* Averaging keeps fractions.  pot_metrics rounded the averaged frame back to
  whole numbers before measuring, which threw away the precision the averaging
  was for.  Here the colour boxes are averaged frame by frame (exactly the same
  result as averaging whole frames, since it's all means), so a shading error of
  a quarter of a code value still shows.
* Memory: the colour modes keep five numbers per frame; focus keeps the 90
  luma pictures (31 MB), not 90 full-colour float frames (370 MB).
* The next pot follows pot_metrics' own rule, "saw before para, always": a
  parabola term is only suggested once the tilt (saw) on the same axis is
  inside the tolerance.  (pot_metrics picked the worst term regardless.)

**What it measures.**  Five sample boxes, each 18% of the frame and at least 8%
in from the edges (away from overscan and blanking): centre (C), left (L), right
(R), top (T) and bottom (B).  From their five values come four terms:

* H saw  = R − L              a tilt from left to right
* H para = (R + L) / 2 − C    the sides against the centre: a bowl or a dome
* V saw  = B − T              a tilt from top to bottom
* V para = (B + T) / 2 − C    top and bottom against the centre

The camera corrects each with a waveform set by one pot: a sawtooth ("saw")
for a tilt, a parabola ("para") for a bowl, horizontally and vertically.

**Modes.**

* *Shading, red / blue.*  Point the camera at an evenly lit white card that
  fills the frame.  Each box's value is its average Cr (red) or Cb (blue) minus
  128: how far its colour is from neutral grey, in 8-bit code values.  A camera
  with perfect shading reads 0 everywhere.
* *Focus.*  Point it at a chart with fine detail all over.  Each box's value is
  how much fine detail the picture has there (see ``sharpness_boxes``).

**Which way to turn.**  Whether clockwise raises or lowers a term depends on the
circuit, so it's learned once per pot and remembered (``set_polarity``).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .color import NEUTRAL_C, split_uyvy

MARGIN = 0.08  # stay this far in from the edges: overscan, blanking, the chart's own border
BOX = 0.18  # each sample box is this fraction of the frame's width and height
BOX_NAMES = ("C", "L", "R", "T", "B")
ORDER = ("H saw", "H para", "V saw", "V para")  # saw before para, always

TERM_HELP = {
    "H saw": "Tilt from left to right: the right box minus the left box.",
    "H para": "The sides against the centre: the average of left and right, minus the centre.",
    "V saw": "Tilt from top to bottom: the bottom box minus the top box.",
    "V para": "Top and bottom against the centre: their average, minus the centre.",
}

MODES = ("shading_red", "shading_blue", "focus")
MODE_LABELS = {"shading_red": "Shading: red", "shading_blue": "Shading: blue", "focus": "Focus"}
MODE_HELP = {
    "shading_red": "Point the camera at an evenly lit white card that fills the frame. Each box reads how far its "
                   "colour is from neutral grey on the red axis (Cr − 128, in code values).",
    "shading_blue": "Point the camera at an evenly lit white card that fills the frame. Each box reads how far its "
                    "colour is from neutral grey on the blue axis (Cb − 128, in code values).",
    "focus": "Point the camera at a chart with fine detail all over. Each box reads how much fine detail the "
             "camera resolves there; the terms compare the edges with the centre.",
}

#: Which pot corrects which term (the CKC021's shading and dynamic-focus pots).
POTS: dict[str, dict[str, str]] = {
    "shading_red": {"H saw": "RT313", "H para": "RT314", "V saw": "RT315", "V para": "RT316"},
    "shading_blue": {"H saw": "RT309", "H para": "RT310", "V saw": "RT311", "V para": "RT312"},
    "focus": {"H saw": "RT305", "H para": "RT306", "V saw": "RT307", "V para": "RT308"},
}

#: Frames averaged: about 3 s at 29.97 fps.  Analog noise averages away; a pot
#: you've just turned shows its full effect within 3 s.
WINDOW = 90


def regions(height: int, width: int) -> dict[str, tuple[int, int, int, int]]:
    """The five sample boxes as (x0, y0, x1, y1) pixel bounds, x1 and y1 excluded (pot_metrics' geometry)."""
    m, b = MARGIN, BOX
    corners = {"C": (0.5 - b / 2, 0.5 - b / 2), "L": (m, 0.5 - b / 2), "R": (1 - m - b, 0.5 - b / 2),
               "T": (0.5 - b / 2, m), "B": (0.5 - b / 2, 1 - m - b)}
    return {name: (int(fx * width), int(fy * height), int((fx + b) * width), int((fy + b) * height))
            for name, (fx, fy) in corners.items()}


def chroma_boxes(uyvy: np.ndarray, channel: str) -> np.ndarray:
    """Each box's average colour difference from neutral, in BOX_NAMES order.

    ``channel`` "red" reads Cr − 128, "blue" Cb − 128.  4:2:2 stores one Cb and
    one Cr per pair of pixels, so each pixel of a box counts its pair's sample.
    """
    _, cb, cr = split_uyvy(uyvy)
    plane = cr if channel == "red" else cb
    boxes = regions(uyvy.shape[0], uyvy.shape[1] // 2)
    out = np.empty(len(BOX_NAMES))
    for i, name in enumerate(BOX_NAMES):
        x0, y0, x1, y1 = boxes[name]
        out[i] = plane[y0:y1, np.arange(x0, x1) // 2].mean() - NEUTRAL_C
    return out


def _blur3(a: np.ndarray) -> np.ndarray:
    """A 3 × 3 Gaussian blur, [1 2 1]/4 each way (OpenCV's GaussianBlur((3, 3), 0)).  Two smaller each way."""
    a = (a[:-2] + 2 * a[1:-1] + a[2:]) * 0.25
    return (a[:, :-2] + 2 * a[:, 1:-1] + a[:, 2:]) * 0.25


def _laplacian3(a: np.ndarray) -> np.ndarray:
    """OpenCV's 3 × 3 Laplacian (ksize=3): [[2, 0, 2], [0, −8, 0], [2, 0, 2]].  Two smaller each way."""
    return 2 * (a[:-2, :-2] + a[:-2, 2:] + a[2:, :-2] + a[2:, 2:]) - 8 * a[1:-1, 1:-1]


def sharpness_boxes(luma: np.ndarray) -> np.ndarray:
    """Fine-detail energy in each box, in BOX_NAMES order: the variance of the Laplacian (pot_metrics' measure).

    The Laplacian responds to rapid change from pixel to pixel, so a sharply
    focused chart gives large values of both signs and a big variance; a blurry
    one gives small values.  The light blur first takes out single-pixel noise.
    Its [1 2 1] shape also exactly cancels anything that alternates line by
    line, so if the camera's two interlaced fields don't quite match (in
    brightness, or in where their lines fall), that isn't mistaken for detail.
    """
    boxes = regions(*luma.shape)
    out = np.empty(len(BOX_NAMES))
    for i, name in enumerate(BOX_NAMES):
        x0, y0, x1, y1 = boxes[name]
        patch = luma[y0 - 2:y1 + 2, x0 - 2:x1 + 2].astype(np.float64)  # 2 spare pixels for the two filters
        out[i] = _laplacian3(_blur3(patch)).var()
    return out


def decompose(boxes: Mapping[str, float]) -> dict[str, float]:
    """Five box values → the four terms, one per pot."""
    return {"H saw": boxes["R"] - boxes["L"],
            "H para": (boxes["R"] + boxes["L"]) / 2 - boxes["C"],
            "V saw": boxes["B"] - boxes["T"],
            "V para": (boxes["B"] + boxes["T"]) / 2 - boxes["C"]}


@dataclass(frozen=True)
class Term:
    name: str
    pot: str
    error: float
    ok: bool
    """Inside the tolerance."""
    direction: str
    """"turn CW", "turn CCW", "OK", or "learn" (the pot's direction isn't known yet)."""


@dataclass(frozen=True)
class Reading:
    mode: str
    terms: tuple[Term, ...]
    boxes: dict[str, float]
    converged: bool
    """Every term inside the tolerance."""
    next_term: str | None
    next_pot: str | None
    frames: int
    """How many frames the reading averages."""
    deadband: float | None
    """The tolerance, or None if the noise hasn't been measured (then nothing counts as OK)."""


def turn_direction(error: float, polarity: int | None, ok: bool) -> str:
    """Which way to turn a pot to bring its term to zero.  ``polarity`` +1: clockwise raises the term."""
    if polarity is None:
        return "learn"
    if ok:
        return "OK"
    return "turn CCW" if (error > 0) == (polarity > 0) else "turn CW"


def next_term(terms: Sequence[Term]) -> str | None:
    """The term to work on next: the worst one outside tolerance, but a para only once its axis's saw is in."""
    ok = {t.name: t.ok for t in terms}
    candidates = [t for t in terms if not t.ok and (t.name.endswith("saw") or ok[t.name.replace("para", "saw")])]
    return max(candidates, key=lambda t: abs(t.error)).name if candidates else None


def deadband_from(first: Mapping[str, float], second: Mapping[str, float]) -> float:
    """Two readings taken with nothing touched → the tolerance: twice their biggest difference."""
    return max(2.0 * max(abs(first[t] - second[t]) for t in ORDER), 1e-6)


class PotAssist:
    """Averages the last ``window`` frames and turns them into readings.  Use it from one thread only."""

    def __init__(self, mode: str = "shading_red", window: int = WINDOW, deadband: float | None = None,
                 polarity: Mapping[str, int] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.window = int(window)
        self.deadband = deadband
        self.polarity: dict[str, int] = dict(polarity or {})
        """Pot → +1 if turning it clockwise raises its term, −1 if it lowers it."""
        self._boxes: deque[np.ndarray] = deque(maxlen=self.window)  # shading: five box values per frame
        self._lumas: deque[np.ndarray] = deque()  # focus: the luma pictures in the window…
        self._luma_sum: np.ndarray | None = None  # …and their running total (exact: integers)

    @property
    def pots(self) -> dict[str, str]:
        return POTS[self.mode]

    def set_mode(self, mode: str, deadband: float | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode, self.deadband = mode, deadband
        self.clear()

    def clear(self) -> None:
        """Forget the frames averaged so far (e.g. after turning a pot)."""
        self._boxes.clear()
        self._lumas.clear()
        self._luma_sum = None

    @property
    def frames(self) -> int:
        return len(self._lumas) if self.mode == "focus" else len(self._boxes)

    @property
    def ready(self) -> bool:
        """Enough frames for a first reading: a quarter of the window, at least 8 (or all of it, if smaller)."""
        return self.frames >= min(self.window, max(8, self.window // 4))

    def add(self, uyvy: np.ndarray) -> None:
        """Take one frame: the card's raw UYVY bytes (``CapturedFrame.uyvy``)."""
        if self.mode != "focus":
            self._boxes.append(chroma_boxes(uyvy, "red" if self.mode == "shading_red" else "blue"))
            return
        luma = np.ascontiguousarray(uyvy[:, 1::2])
        if self._luma_sum is None or self._luma_sum.shape != luma.shape:
            self.clear()
            self._luma_sum = np.zeros(luma.shape, np.int32)
        self._lumas.append(luma)
        self._luma_sum += luma
        if len(self._lumas) > self.window:
            self._luma_sum -= self._lumas.popleft()

    def boxes(self) -> dict[str, float] | None:
        """The five box values, averaged over the window; None until ready."""
        if not self.ready:
            return None
        if self.mode == "focus":
            values = sharpness_boxes(self._luma_sum / len(self._lumas))
        else:
            values = np.mean(self._boxes, axis=0)
        return dict(zip(BOX_NAMES, map(float, values)))

    def raw_terms(self) -> dict[str, float] | None:
        boxes = self.boxes()
        return None if boxes is None else decompose(boxes)

    def read(self) -> Reading | None:
        boxes = self.boxes()
        if boxes is None:
            return None
        errors = decompose(boxes)
        dead = self.deadband if self.deadband is not None else 0.0
        terms = []
        for name in ORDER:
            pot, error = self.pots[name], errors[name]
            ok = abs(error) <= dead
            terms.append(Term(name, pot, error, ok, turn_direction(error, self.polarity.get(pot), ok)))
        chosen = next_term(terms)
        return Reading(self.mode, tuple(terms), boxes, chosen is None, chosen, self.pots[chosen] if chosen else None,
                       self.frames, self.deadband)

    def set_polarity(self, pot: str, clockwise_raises_error: bool) -> None:
        self.polarity[pot] = 1 if clockwise_raises_error else -1
