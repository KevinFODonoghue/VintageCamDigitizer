"""
Handing frames between threads: the captured-frame record and newest-wins slots.

The capture card delivers a frame every 33.4 ms whether or not anyone is ready.
Three consumers want those frames, and they want them differently:

* The **recorder** needs *every* frame, in order.  It gets a queue (see
  ``recorder.py``).  If the disk is briefly slow, frames wait in RAM.

* The **preview** (and, in Phase 2, the scopes) need only the *newest* frame.
  If the screen is busy for 100 ms, showing the three frames it missed — late —
  is worse than useless when your hand is on a trimmer pot: you'd be watching
  the past.  So they get a ``LatestSlot``: a mailbox that holds exactly one
  frame.  Posting a new frame throws away the old one if nobody collected it.
  That's what "drop-on-late" means, and it's why the preview can never lag.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import numpy as np

from .video_format import VideoStandard

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One frame exactly as the card delivered it, plus when it arrived.

    Shared read-only between threads: the recorder, the preview and (later) the
    analysis thread all look at the same bytes.  Nobody may modify ``uyvy`` —
    it's marked non-writeable to enforce that.
    """

    index: int
    """0, 1, 2, … counted since the device was opened."""

    device_time: float
    """DirectShow's timestamp for this frame, in seconds on the system reference
    clock.  Jitters by about ±5 ms on this card (it's quantised to ~10 ms), so
    use it for spotting dropped frames, not for precise timing."""

    arrival_time: float
    """``time.perf_counter()`` when the capture thread received the frame."""

    uyvy: np.ndarray
    """Raw packed 4:2:2 bytes, shape ``(height, width * 2)``, dtype uint8.
    See ``color.py`` for what the bytes mean."""

    av_frame: Any
    """The PyAV ``VideoFrame`` the bytes live in (kept so the preview can use
    FFmpeg's fast colour converter on it without a copy)."""

    standard: VideoStandard

    @property
    def width(self) -> int:
        return self.uyvy.shape[1] // 2

    @property
    def height(self) -> int:
        return self.uyvy.shape[0]


class LatestSlot(Generic[T]):
    """A single-item mailbox that always holds the newest item.

    ``put`` never blocks and never grows: it replaces whatever was there.
    ``take`` empties the slot.  Thread-safe.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self.replaced = 0
        """How many items were overwritten before anyone took them.  For the
        preview this is normal (the screen can't show every frame); a steadily
        climbing number just means the consumer is slower than 29.97 fps."""

    def put(self, item: T) -> bool:
        """Store ``item``, replacing any uncollected one.

        Returns True if the slot was empty — i.e. the consumer has caught up
        and should be told a new item is waiting.  The capture thread uses this
        to send at most one "new frame" signal at a time to the GUI, so Qt's
        event queue can't fill up with stale notifications.
        """
        with self._cond:
            was_empty = self._item is None
            if not was_empty:
                self.replaced += 1
            self._item = item
            self._cond.notify()
        return was_empty

    def take(self) -> T | None:
        """Remove and return the newest item, or None if the slot is empty."""
        with self._cond:
            item, self._item = self._item, None
        return item

    def wait_take(self, timeout: float | None = None) -> T | None:
        """Like ``take`` but waits up to ``timeout`` seconds for an item.
        (For worker threads, e.g. the Phase 2 analysis thread.)"""
        with self._cond:
            if self._item is None:
                self._cond.wait(timeout)
            item, self._item = self._item, None
        return item

    def clear(self) -> None:
        with self._cond:
            self._item = None


def frame_from_uyvy(
    uyvy: np.ndarray, standard: VideoStandard, index: int = 0, device_time: float = 0.0,
    arrival_time: float | None = None,
) -> CapturedFrame:
    """Wrap raw UYVY bytes in a CapturedFrame, exactly as if the card had sent them.

    Used by the tests (synthetic frames through the real recorder and renderer).
    """
    import av  # local import: keeps this module importable without PyAV for simple tools

    height, row = uyvy.shape
    video_frame = av.VideoFrame(row // 2, height, "uyvy422")
    plane = video_frame.planes[0]
    dst = np.frombuffer(plane, dtype=np.uint8)[: plane.line_size * height].reshape(height, plane.line_size)
    dst[:, :row] = uyvy
    view = dst[:, :row]
    view.flags.writeable = False
    arrival = time.perf_counter() if arrival_time is None else arrival_time
    return CapturedFrame(index, device_time, arrival, view, video_frame, standard)
