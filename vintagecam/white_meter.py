"""
The white meter: one live number for how white the picture is.

Point the camera at a white card and turn a pot: the number goes up as the
picture gets whiter.  100% is pure white (video white, with no colour at all),
0% is black.

**What it averages.**  The colour of the whole picture: every line, and every
column except the Elgato's black blanking band down the left edge and the spike
at the end of each line (``pot_meter.picture_area``, the area the pot meter's
grid covers; pot_meter.py explains the measurements).  Averaged in, the band
alone would hold a pure white picture below 98%.  The analysis thread takes a
reading ten times a second, each the average of the frames since the last.

**Percent white.**  White light is red, green and blue in equal, full amounts.
So the number is the average colour's brightness (the mean of its R, G and B,
out of 255) less how unequal the three are: √2 × their RMS spread about that
mean.  That makes it:

- 100% for pure white only, and 0% for black; a neutral grey reads its
  brightness (mid grey: 50%);
- never more than the weakest of R, G and B: a colour can't be whiter than its
  weakest primary.  When the two strongest are equal it is exactly the weakest
  (a card lit so red and green read 230 and blue 200 is 78% white), and any
  fully saturated colour (pure red, yellow…) reads 0%.  That's what the √2 is
  for;
- highest, as you turn up any one of R, G and B, where that one matches the
  strongest of the other two.  Up to there the number rises; past it, it falls,
  so pushing one colour past the others never helps.

Brightness counts as well as colour, so the iris, the lighting or a gain pot
move the number too, and an over-exposed picture reads 100% (colours above
video white count as white).  To find a pot's best position whatever the
lighting, use the pot meter.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .color import split_uyvy, ycbcr_to_rgb_float
from .pot_meter import picture_area


@dataclass(frozen=True)
class WhiteReading:
    percent: float
    """How white the picture is, 0–100 (percent_white)."""
    rgb: tuple[float, float, float]
    """The picture's average colour: full-range R, G and B (0–255)."""


def average_colour(uyvy: np.ndarray) -> np.ndarray:
    """The picture's average colour as full-range RGB: shape (3,), floats, not clipped.

    Averaging Y', Cb and Cr and then converting is the same as averaging RGB,
    since the conversion is a straight-line (affine) formula.  Cb and Cr come one
    per pair of pixels (4:2:2), so their columns are at half the x positions.
    """
    y, cb, cr = split_uyvy(uyvy)
    left, right, top, bottom = picture_area(uyvy.shape[0], uyvy.shape[1] // 2)
    return ycbcr_to_rgb_float(y[top:bottom, left:right].mean(), cb[top:bottom, left // 2:right // 2].mean(),
                              cr[top:bottom, left // 2:right // 2].mean())


def percent_white(rgb) -> float:
    """How white a colour is, 0–100: its brightness less how unequal its R, G and B are (see above).

    ``rgb`` is full-range (0–255); values beyond that are clipped first.
    """
    colour = np.clip(np.asarray(rgb, np.float64), 0.0, 255.0)
    brightness = colour.mean()
    spread = np.sqrt(np.mean((colour - brightness) ** 2))
    return float(np.clip(100.0 * (brightness - np.sqrt(2.0) * spread) / 255.0, 0.0, 100.0))


class WhiteMeter:
    """The picture's colour, averaged over the frames since the last reading.  Use it from one thread only."""

    def __init__(self) -> None:
        self._total = np.zeros(3)
        self._frames = 0

    def add(self, uyvy: np.ndarray) -> None:
        """Take one frame: the card's raw UYVY bytes (``CapturedFrame.uyvy``)."""
        self._total += average_colour(uyvy)
        self._frames += 1

    def reading(self) -> WhiteReading | None:
        """How white the frames since the last reading were (None if there were none); starts a new average."""
        if not self._frames:
            return None
        rgb = np.clip(self._total / self._frames, 0.0, 255.0)
        self.reset()
        return WhiteReading(percent_white(rgb), (float(rgb[0]), float(rgb[1]), float(rgb[2])))

    def reset(self) -> None:
        self._total[:] = 0.0
        self._frames = 0
