"""
Video standards — what a frame from the capture card actually *is*.

Worth reading once if analog video is new to you:

**480i and 576i.**  An NTSC picture is 525 scan lines, of which about 480 carry
picture.  The rest are *vertical blanking*: time for a CRT's electron beam to fly
back to the top of the screen.  PAL uses 625 lines, 576 of them active.  The
card's decoder chip samples every active line 720 times.  That 720 comes from the
ITU-R BT.601 digital-video standard (13.5 MHz sampling), not from the camera: an
analog line has no pixels at all, just a continuously varying voltage.

**Interlacing — the "i" in 480i.**  Your tube camera doesn't scan 480 lines in
one pass.  It scans every other line (one *field*, taking 1/59.94 s), then goes
back and scans the lines in between (the second field).  The capture card weaves
the two fields into one 720×480 frame.  Anything that moved between the two
fields shows up as *combing*: alternate lines shifted sideways.  Recordings keep
both fields exactly as captured; the preview can hide combing (see
``color.deinterlace_blend`` and ``color.bob_field``).

**29.97 fps, not 30.**  When colour was added to NTSC in 1953 the frame rate was
lowered by 0.1% — to exactly 30000/1001 — so the new colour subcarrier wouldn't
beat visibly against the sound carrier.  Everything downstream still runs at
30000/1001, including this card.

**Non-square pixels.**  720 samples per line don't make square pixels.  NTSC
720×480 is shown 4:3 over its central 704 samples, so each pixel is 10/11 as wide
as it is tall (PAL: 12/11).  Shown with square pixels, a circle on your test
chart looks about 10% too wide.  The preview corrects this by default;
recordings store the raw samples untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# DirectShow AnalogVideoStandard flags (Windows SDK header strmif.h).
# IAMAnalogVideoDecoder::put_TVFormat takes one of these to switch the decoder chip.
ANALOG_NTSC_M = 0x0000_0001  # North America: 525 lines, 7.5 IRE setup
ANALOG_PAL_B = 0x0000_0010  # most of Europe (B/G/D/H/I are identical over composite)

# FFmpeg colour-description codes (libavutil/pixfmt.h).  Written into recordings
# so players use the right Y'CbCr->RGB matrix.  Metadata only: no pixel changes.
_PRI_BT470BG, _PRI_SMPTE170M = 5, 6
_TRC_GAMMA28, _TRC_SMPTE170M = 5, 6
_SPC_BT470BG, _SPC_SMPTE170M = 5, 6


@dataclass(frozen=True)
class VideoStandard:
    """One analog TV standard, as this capture card delivers it."""

    key: str
    """Short id stored in settings.json: "NTSC" or "PAL"."""

    name: str
    """Human-readable name for menus."""

    width: int
    """Active samples per line (BT.601: always 720)."""

    height: int
    """Active lines per frame, both fields together (480 or 576)."""

    dshow_framerate: str
    """The exact string handed to FFmpeg's dshow ``framerate`` option.

    NTSC must be ``"29.97"`` — never ``"30000/1001"``, even though that is the
    mathematically exact rate.  FFmpeg converts the requested rate into
    DirectShow's unit (100-nanosecond ticks per frame) using *integer* division::

        30000/1001 ->  1001 * 10_000_000 // 30000 = 333_666 ticks  (rounded down)
        29.97      ->   100 * 10_000_000 //  2997 = 333_667 ticks

    The Elgato advertises exactly 333_667 ticks per frame and nothing else (the
    verbose log shows ``fr:10000000/333667``), so the "exact" rate misses by one
    tick and FFmpeg fails with ``Could not set video options``.  "29.97" looks
    like a sloppy approximation, but it is the value that works.  Do not
    "fix" it.
    """

    frame_rate: Fraction
    """True nominal frame rate, used for recording timestamps."""

    pixel_aspect: Fraction
    """Width ÷ height of one pixel on a real 4:3 display (BT.601)."""

    total_lines: int
    """Lines per frame including vertical blanking (525 / 625)."""

    analog_flag: int
    """AnalogVideoStandard flag for IAMAnalogVideoDecoder::put_TVFormat."""

    field_order: str
    """Which field of a woven frame is *earlier in time*: "tff" (top field
    first) or "bff" (bottom field first).  Only the bob preview uses this.
    Not yet verified for this card: if motion judders back and forth in bob
    mode, flip it in the View menu."""

    color_primaries: int
    color_trc: int
    colorspace: int

    @property
    def fps(self) -> float:
        return float(self.frame_rate)

    @property
    def frame_duration(self) -> float:
        """Seconds per frame (1/29.97 ≈ 0.0333667 s for NTSC)."""
        return float(1 / self.frame_rate)

    @property
    def display_width(self) -> float:
        """Width in square pixels when shown with correct 4:3 geometry."""
        return self.width * float(self.pixel_aspect)

    def describe(self) -> str:
        return f"{self.width}×{self.height} · {self.fps:.2f} fps · interlaced"


NTSC = VideoStandard(
    key="NTSC",
    name="NTSC-M  (525 lines, 29.97 fps)",
    width=720,
    height=480,
    dshow_framerate="29.97",
    frame_rate=Fraction(30000, 1001),
    pixel_aspect=Fraction(10, 11),
    total_lines=525,
    analog_flag=ANALOG_NTSC_M,
    field_order="bff",
    color_primaries=_PRI_SMPTE170M,
    color_trc=_TRC_SMPTE170M,
    colorspace=_SPC_SMPTE170M,
)

PAL = VideoStandard(
    key="PAL",
    name="PAL-B/G  (625 lines, 25 fps)",
    width=720,
    height=576,
    dshow_framerate="25",
    frame_rate=Fraction(25, 1),
    pixel_aspect=Fraction(12, 11),
    total_lines=625,
    analog_flag=ANALOG_PAL_B,
    field_order="tff",
    color_primaries=_PRI_BT470BG,
    color_trc=_TRC_GAMMA28,
    colorspace=_SPC_BT470BG,
)

STANDARDS: dict[str, VideoStandard] = {s.key: s for s in (NTSC, PAL)}


def standard_for_analog_flag(flag: int) -> VideoStandard | None:
    """Map a decoder TV-format flag back to the standard we'd capture it with."""
    if flag & 0x0000_000F:  # any NTSC variant (M, M-J, 4.43)
        return NTSC
    if flag:  # PAL and SECAM variants are all 625-line / 25 fps
        return PAL
    return None
