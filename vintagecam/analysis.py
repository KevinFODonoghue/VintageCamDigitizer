"""
The analysis thread (Phase 2): runs the instruments on the newest frames.

It's the third consumer of frames beside the recorder and the preview (see
frames.py).  It reads the capture thread's ``analysis_slot``, a mailbox that
only ever holds the newest frame: if the analysis falls behind, it skips frames
rather than queueing them, so it can never slow the capture, the recording or
the preview down.  Nothing here runs on the GUI thread; results go to the GUI
through a callback (the main window turns it into a Qt signal).

For now it runs one instrument, the pot meter (pot_meter.py).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .frames import CapturedFrame, LatestSlot
from .pot_meter import DARK_WARNING, LIVE_FRAMES, MEASURE_FRAMES, PotMeter

log = logging.getLogger(__name__)

#: Seconds between updates to the GUI.  Five a second is plenty to read.
STATUS_INTERVAL = 0.2


@dataclass(frozen=True)
class PotStatus:
    measuring: str
    """"cw" or "ccw" while that end is being measured, otherwise ""."""
    progress: float
    """How far through that measurement, 0 to 1."""
    has_cw: bool
    has_ccw: bool
    at_best: bool | None
    """The light: True green, False red, None grey (an end not measured yet, or a pot that can't be judged)."""
    position: float | None = None
    """Where the pot seems to be: 0 = fully anticlockwise, 1 = fully clockwise."""
    best: float | None = None
    """Where the grid comes closest to its target, on the same scale."""
    note: str = ""


class AnalysisThread(threading.Thread):
    """Takes the newest frames and runs the pot meter on them, off the GUI thread."""

    def __init__(self, on_status: Callable[[PotStatus], None], *, live_frames: int = LIVE_FRAMES,
                 measure_frames: int = MEASURE_FRAMES) -> None:
        super().__init__(name="AnalysisThread", daemon=True)
        self.meter = PotMeter(live_frames, measure_frames)
        self.processed = 0
        """Frames analysed so far."""
        self._on_status = on_status
        self._source: LatestSlot[CapturedFrame] | None = None
        self._enabled = False
        self._commands: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._next_status = 0.0

    # -- called from the GUI thread ------------------------------------------------

    def set_source(self, slot: LatestSlot[CapturedFrame] | None) -> None:
        """Where frames come from: the running capture's ``analysis_slot`` (None while there's no capture)."""
        self._source = slot  # a single attribute assignment is atomic in Python

    def set_enabled(self, on: bool) -> None:
        """Analyse only while someone's looking (the pot meter panel is on screen)."""
        self._enabled = on

    @property
    def enabled(self) -> bool:
        return self._enabled

    def measure(self, end: str) -> None:
        """Measure one end of the pot's travel: "cw" (fully clockwise) or "ccw" (fully anticlockwise)."""
        self._commands.put(("measure", end))

    def reset(self) -> None:
        """Forget both ends, for the next pot."""
        self._commands.put(("reset", None))

    def set_reference(self, reference: np.ndarray | None) -> None:
        """What each grid cell should look like (pot_meter.reference_from_rgb), or None for plain white."""
        self._commands.put(("reference", reference))

    def stop(self) -> None:
        self._stop_event.set()

    # -- thread body -----------------------------------------------------------------

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._handle_commands()
            slot = self._source
            if slot is None or not self._enabled:
                self._stop_event.wait(0.05)
                continue
            frame = slot.wait_take(0.1)
            if frame is None or not self._enabled:  # switched off while it waited: drop the frame
                continue
            try:
                was_measuring = self.meter.measuring
                self.meter.add(frame.uyvy)
                self.processed += 1
                self._report(force=bool(was_measuring) and not self.meter.measuring)  # an end just finished
            except Exception:  # keep going, but never silently
                log.exception("The pot meter couldn't analyse a frame")
                self._stop_event.wait(1.0)  # and don't flood the log

    def _handle_commands(self) -> None:
        while True:
            try:
                command, value = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "measure":
                self.meter.measure(value)
            elif command == "reset":
                self.meter.reset()
            elif command == "reference":
                self.meter.reference = value
            self._report(force=True)

    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_status:
            return
        self._next_status = now + STATUS_INTERVAL
        meter = self.meter
        judgement = meter.judge()
        notes = []
        live = meter.live()
        if live is not None and float(live.mean()) < DARK_WARNING:
            notes.append("The picture is dark: is the camera pointed at a well-lit white card?")
        if judgement is not None and judgement.note:
            notes.append(judgement.note)
        status = PotStatus(meter.measuring, meter.progress, meter.ends["cw"] is not None,
                           meter.ends["ccw"] is not None, judgement.at_best if judgement else None,
                           judgement.position if judgement else None, judgement.best if judgement else None,
                           " ".join(notes))
        try:
            self._on_status(status)
        except Exception:
            log.exception("The pot meter's status update failed")
