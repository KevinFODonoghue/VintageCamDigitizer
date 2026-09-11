"""
Preview rendering: a raw captured frame -> a display-ready BGRA image.

Runs on the GUI thread, and only for frames that will actually be shown (frames
the screen has no time for are never converted).  It uses FFmpeg's swscale
converter, roughly ten times faster than the same maths in numpy, set up
explicitly rather than trusting defaults:

* **BT.601 matrix**, the standard for SD video.  HD uses BT.709; applying the
  wrong one tints the whole picture slightly — exactly the error a calibration
  preview must not have.
* **Limited input range** (16–235) stretched to the display's 0–255.
* **Full-resolution chroma interpolation** and accurate rounding.

``tests/test_color.py`` checks the result against the long-hand formula in
``color.py``; that's how we know the preview shows the camera's true colours.
"""

from __future__ import annotations

import numpy as np
from av.video.reformatter import ColorRange, Colorspace, Interpolation, VideoReformatter

from .color import bob_field, deinterlace_blend
from .frames import CapturedFrame

_INTERPOLATION = Interpolation.BILINEAR | Interpolation.ACCURATE_RND | Interpolation.FULL_CHR_H_INT


class PreviewRenderer:
    """Converts frames for display.  One per thread (it caches FFmpeg state)."""

    def __init__(self) -> None:
        self._reformatter = VideoReformatter()

    def to_bgra(self, frame: CapturedFrame) -> np.ndarray:
        """(height, width, 4) uint8, bytes in B, G, R, A order.

        BGRA is what Qt calls ``Format_RGB32`` on little-endian PCs, the format
        it can draw without any further conversion.
        """
        out = self._reformatter.reformat(
            frame.av_frame,
            format="bgra",
            src_colorspace=Colorspace.ITU601,
            dst_colorspace=Colorspace.ITU601,
            src_color_range=ColorRange.MPEG,
            dst_color_range=ColorRange.JPEG,
            interpolation=_INTERPOLATION,
        )
        return out.to_ndarray()

    def render(self, frame: CapturedFrame, deinterlace: str = "off", field: int = 0) -> np.ndarray:
        """Convert, then apply the viewing-only deinterlacer.

        ``deinterlace`` is "off", "blend" or "bob"; for "bob", ``field`` picks
        which field (0 = top, 1 = bottom) to show.
        """
        img = self.to_bgra(frame)
        if deinterlace == "blend":
            return deinterlace_blend(img)
        if deinterlace == "bob":
            return bob_field(img, field)
        return img
