"""
Luma and colour math for 8-bit Y'CbCr video (ITU-R BT.601, standard definition).

Everything here is plain numpy so it can be tested without a capture card.

**Y', Cb and Cr.**  Video doesn't store red, green and blue.  It stores *luma*
(Y', the black-and-white picture) and two *colour-difference* signals: Cb (about
"blue minus luma") and Cr ("red minus luma").  The split dates from 1953: old
black-and-white sets could ignore the colour signals and still show a correct
picture.  It's also why the Phase 2 vectorscope plots Cb against Cr.  Anything
truly grey has Cb = Cr = 128 ("no colour"), so a colour cast in the camera shows
up as the trace wandering away from the centre.

**Limited ("studio") range.**  In 8-bit video black is Y' = 16 and white is
Y' = 235, not 0 and 255.  The room above 235 and below 16 is there so analog
overshoot and ringing aren't chopped off.  Cb and Cr run 16–240, and 128 means
"no colour".  Values stuck at exactly 16 or 235 across an area usually mean the
signal was clipped somewhere upstream.

**4:2:2 chroma subsampling.**  Your eye sees fine detail in brightness much better
than in colour.  BT.601 exploits that by storing one Cb and one Cr sample for
every *two* luma samples along a line ("4:2:2"), so colour has half the
horizontal resolution of luma.  Composite video is coarser still (about 1.3 MHz
of colour bandwidth against 4.2 MHz of luma), so the card isn't discarding
anything your camera actually sent.

**UYVY byte order.**  The Elgato packs each pair of pixels into 4 bytes::

      byte:   0    1    2    3    4    5    6    7   ...
      value:  Cb0  Y0   Cr0  Y1   Cb1  Y2   Cr1  Y3  ...
             \\____ pixels 0+1 ____/ \\____ pixels 2+3 ____/

  so for a row of bytes:  Y = row[1::2],  Cb = row[0::4],  Cr = row[2::4].

**Cross-colour.**  Composite video carries colour on a 3.58 MHz subcarrier mixed
into the luma.  Fine luma detail near 3.58 MHz (the wedges and the star on your
test chart) fools the decoder into seeing colour that isn't there: the rainbow
shimmer on fine stripes.  That's cross-colour, and it's why colour readings taken
over detailed areas are noisy.  Measure colour on flat grey patches.
"""

from __future__ import annotations

import numpy as np

# Limited-range code values (BT.601, 8-bit).
BLACK_Y = 16
WHITE_Y = 235
NEUTRAL_C = 128
MIN_C = 16
MAX_C = 240

# BT.601 luma weights: Y' = 0.299 R' + 0.587 G' + 0.114 B'.  Green dominates
# because the eye is most sensitive to it.
KR, KB = 0.299, 0.114


def split_uyvy(uyvy: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(Y, Cb, Cr)`` views of a UYVY frame without copying.

    ``uyvy`` has shape ``(height, width*2)``.  Y comes back as ``(height, width)``;
    Cb and Cr as ``(height, width/2)`` — half as many, that's 4:2:2.
    """
    return uyvy[:, 1::2], uyvy[:, 0::4], uyvy[:, 2::4]


def uyvy_to_planar(uyvy: np.ndarray) -> np.ndarray:
    """Repack UYVY into planar yuv422p, laid out the way PyAV expects.

    FFV1 can't store packed UYVY, so the recorder converts to planar: all Y
    samples, then all Cb, then all Cr.  (The ffmpeg command line does the same
    thing silently.)  It's a pure byte shuffle — no arithmetic, no rounding —
    so the recording stays bit-exact.

    Returns a ``(2*height, width)`` uint8 array: rows ``0..height-1`` are the Y
    plane; the remaining rows hold the Cb plane followed by the Cr plane, each
    ``height × width/2`` flattened.  That's the layout
    ``av.VideoFrame.from_ndarray(..., format="yuv422p")`` takes.
    """
    height, row_bytes = uyvy.shape
    width = row_bytes // 2
    out = np.empty((2 * height, width), dtype=np.uint8)
    out[:height] = uyvy[:, 1::2]
    chroma = out[height:].reshape(2, height, width // 2)
    chroma[0] = uyvy[:, 0::4]
    chroma[1] = uyvy[:, 2::4]
    return out


def ycbcr_to_rgb(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    """Reference BT.601 conversion: limited-range Y'CbCr -> full-range R'G'B' (uint8).

    Written out long-hand so you can check it against the standard.  The live
    preview uses FFmpeg's optimised converter instead; the tests prove the two
    agree, which is how we know the preview shows true colours.
    """
    yf = (y.astype(np.float32) - BLACK_Y) * (255.0 / (WHITE_Y - BLACK_Y))
    pb = (cb.astype(np.float32) - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    pr = (cr.astype(np.float32) - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    r = yf + 2 * (1 - KR) * pr
    b = yf + 2 * (1 - KB) * pb
    g = (yf - KR * r - KB * b) / (1 - KR - KB)
    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)


def ycbcr_to_rgb_float(y, cb, cr) -> np.ndarray:
    """The same conversion without rounding or clipping, as floats: for measurements.

    It's a straight-line (affine) formula, so it can convert averages: the
    average Y', Cb and Cr of an area give that area's average R, G and B.  Takes
    arrays or plain numbers; R, G and B come back along the last axis.
    """
    yf = (np.asarray(y, np.float64) - BLACK_Y) * (255.0 / (WHITE_Y - BLACK_Y))
    pb = (np.asarray(cb, np.float64) - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    pr = (np.asarray(cr, np.float64) - NEUTRAL_C) * (255.0 / (MAX_C - MIN_C))
    r = yf + 2 * (1 - KR) * pr
    b = yf + 2 * (1 - KB) * pb
    g = (yf - KR * r - KB * b) / (1 - KR - KB)
    return np.stack([r, g, b], axis=-1)


def uyvy_to_rgb(uyvy: np.ndarray) -> np.ndarray:
    """Reference UYVY -> RGB (height, width, 3), each chroma sample shared by 2 pixels."""
    y, cb, cr = split_uyvy(uyvy)
    return ycbcr_to_rgb(y, np.repeat(cb, 2, axis=1), np.repeat(cr, 2, axis=1))


def rgb_to_uyvy(rgb: np.ndarray) -> np.ndarray:
    """Inverse of ``uyvy_to_rgb`` (for building synthetic test frames).

    Chroma for each pixel pair is the average of the two pixels.
    """
    rgbf = rgb.astype(np.float32) / 255.0
    r, g, b = rgbf[..., 0], rgbf[..., 1], rgbf[..., 2]
    yl = KR * r + (1 - KR - KB) * g + KB * b
    pb = (b - yl) / (2 * (1 - KB))
    pr = (r - yl) / (2 * (1 - KR))
    y = BLACK_Y + yl * (WHITE_Y - BLACK_Y)
    cb = NEUTRAL_C + pb * (MAX_C - MIN_C)
    cr = NEUTRAL_C + pr * (MAX_C - MIN_C)
    cb = (cb[:, 0::2] + cb[:, 1::2]) / 2
    cr = (cr[:, 0::2] + cr[:, 1::2]) / 2
    h, w = y.shape
    out = np.empty((h, w * 2), dtype=np.uint8)
    out[:, 1::2] = np.clip(np.rint(y), 0, 255)
    out[:, 0::4] = np.clip(np.rint(cb), 0, 255)
    out[:, 2::4] = np.clip(np.rint(cr), 0, 255)
    return out


# ---------------------------------------------------------------------------
# Viewing-only deinterlacers.  These only ever touch the *preview* image; the
# recording keeps both fields exactly as captured.
# ---------------------------------------------------------------------------


def deinterlace_blend(img: np.ndarray) -> np.ndarray:
    """"Linear blend": each line becomes (line above + 2 × itself + line below) / 4.

    The lines above and below belong to the *other* field, so this averages the
    two moments in time together.  Combing turns into a soft double image —
    steady and easy to look at, but it blurs vertical detail slightly.  Good for
    a still test chart.  Works on any (height, width[, channels]) uint8 image.
    """
    src = img.astype(np.uint16)
    out = src.copy()
    out[1:-1] = (src[:-2] + 2 * src[1:-1] + src[2:] + 2) >> 2
    return out.astype(np.uint8)


def bob_field(img: np.ndarray, field: int) -> np.ndarray:
    """"Bob": show one field on its own, stretched back to full height.

    ``field`` 0 is the top field (lines 0, 2, 4, …); 1 is the bottom field
    (lines 1, 3, 5, …).  The missing lines are filled with the average of the
    lines above and below from the *same* field, so there's no combing at all —
    at the cost of half the vertical resolution.  Showing the two fields one
    after the other (59.94 per second) gives smooth, full-rate motion.  Height
    must be even (480 and 576 are).
    """
    if field not in (0, 1):
        raise ValueError("field must be 0 (top) or 1 (bottom)")
    out = np.empty_like(img)
    lines = img[field::2]
    out[field::2] = lines
    between = ((lines[:-1].astype(np.uint16) + lines[1:] + 1) >> 1).astype(np.uint8)
    if field == 0:
        out[1:-1:2] = between  # lines 1, 3, … , h-3
        out[-1] = lines[-1]  # bottom line: nothing below to average with
    else:
        out[2::2] = between  # lines 2, 4, … , h-2
        out[0] = lines[0]  # top line: nothing above to average with
    return out


# ---------------------------------------------------------------------------
# Luma staircase reference
# ---------------------------------------------------------------------------


def staircase_levels(steps: int = 16) -> list[int]:
    """Y' code values of an evenly spaced grey staircase from black (16) to white (235)."""
    return [round(BLACK_Y + (WHITE_Y - BLACK_Y) * i / (steps - 1)) for i in range(steps)]


def luma_to_display(y: float) -> int:
    """The 0–255 grey a correctly set up display shows for luma code ``y``.

    This is the same limited->full range expansion the preview applies, so a
    patch drawn in this grey matches video pixels of the same Y' (with no colour).
    """
    return int(np.clip(round((y - BLACK_Y) * 255 / (WHITE_Y - BLACK_Y)), 0, 255))
