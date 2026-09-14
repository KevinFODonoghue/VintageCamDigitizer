"""
The analysis thread (Phase 2): runs the instruments on the newest frames.

It's the third consumer of frames beside the recorder and the preview (see
frames.py).  It reads the capture thread's ``analysis_slot``, a mailbox that
only ever holds the newest frame: if the analysis falls behind, it skips frames
rather than queueing them, so it can never slow the capture, the recording or
the preview down.  Nothing here runs on the GUI thread; results go to the GUI
through a callback (the main window turns it into a Qt signal).

For now it runs one instrument, Pot Assist (pot_assist.py), plus the two
measurements that take several readings in a row:

* **Noise.**  Two readings, each over a fresh window, with nothing touched.
  Twice their biggest difference becomes the tolerance: inside it, a term
  counts as OK.
* **Learning a pot's direction.**  A reading; you turn the pot a little
  clockwise and press Done; a fresh reading.  Whether the term went up or down
  tells which way the pot works, and that's remembered.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .frames import CapturedFrame, LatestSlot
from .pot_assist import WINDOW, PotAssist, Reading, deadband_from

log = logging.getLogger(__name__)

#: Seconds between updates to the GUI.  Five a second is plenty to read.
STATUS_INTERVAL = 0.2


@dataclass(frozen=True)
class PotStatus:
    mode: str
    reading: Reading | None
    filled: float
    """How full the averaging window is, 0 to 1."""
    task: str = ""
    """A measurement in progress: "noise", "learn", or "" for none."""
    prompt: str = ""
    """What to do now (during a measurement), or how the last one ended."""
    waiting_for_user: bool = False
    """The measurement is waiting for Done (after you've turned the pot)."""
    learned: tuple[str, int] | None = None
    """(pot, +1 or −1): a direction just learned.  Sent once, for saving."""
    deadband: float | None = None
    measured_deadband: float | None = None
    """A tolerance just measured.  Sent once, for saving."""


class AnalysisThread(threading.Thread):
    """Takes the newest frames and runs Pot Assist on them, off the GUI thread."""

    def __init__(self, on_status: Callable[[PotStatus], None], *, mode: str = "shading_red", window: int = WINDOW,
                 deadband: float | None = None, polarity: dict[str, int] | None = None) -> None:
        super().__init__(name="AnalysisThread", daemon=True)
        self.assist = PotAssist(mode, window, deadband, polarity)
        self.processed = 0
        """Frames analysed so far."""
        self._on_status = on_status
        self._source: LatestSlot[CapturedFrame] | None = None
        self._enabled = False
        self._commands: queue.SimpleQueue[tuple[str, tuple[Any, ...]]] = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._task: dict[str, Any] | None = None
        self._message = ""
        self._learned: tuple[str, int] | None = None
        self._measured: float | None = None
        self._next_status = 0.0

    # -- called from the GUI thread ------------------------------------------------

    def set_source(self, slot: LatestSlot[CapturedFrame] | None) -> None:
        """Where frames come from: the running capture's ``analysis_slot`` (None while there's no capture)."""
        self._source = slot  # a single attribute assignment is atomic in Python

    def set_enabled(self, on: bool) -> None:
        """Analyse only while someone's looking (the Pot Assist panel is showing)."""
        self._enabled = on

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_mode(self, mode: str, deadband: float | None, polarity: dict[str, int]) -> None:
        self._commands.put(("mode", (mode, deadband, dict(polarity))))

    def measure_noise(self) -> None:
        self._commands.put(("noise", ()))

    def learn(self, pot: str) -> None:
        self._commands.put(("learn", (pot,)))

    def done(self) -> None:
        """The pot has been turned (the middle step of learning its direction)."""
        self._commands.put(("done", ()))

    def cancel(self) -> None:
        self._commands.put(("cancel", ()))

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
                self.assist.add(frame.uyvy)
                self.processed += 1
                self._advance_task()
                self._report()
            except Exception:  # keep going, but never silently
                log.exception("Pot Assist couldn't analyse a frame")
                self._stop_event.wait(1.0)  # and don't flood the log

    def _handle_commands(self) -> None:
        while True:
            try:
                command, args = self._commands.get_nowait()
            except queue.Empty:
                return
            assist, task = self.assist, self._task
            if command == "mode":
                mode, deadband, polarity = args
                assist.set_mode(mode, deadband)
                assist.polarity = polarity
                self._task, self._message = None, ""
            elif command == "noise":
                assist.clear()
                self._start({"kind": "noise", "first": None})
            elif command == "learn":
                pot = args[0]
                term = next((name for name, p in assist.pots.items() if p == pot), None)
                if term is not None:
                    self._start({"kind": "learn", "pot": pot, "term": term, "stage": "before", "before": 0.0})
            elif command == "done" and task and task["kind"] == "learn" and task["stage"] == "turn":
                assist.clear()  # the reading after the turn must contain only frames from after it
                task["stage"] = "after"
            elif command == "cancel" and task:
                self._task, self._message = None, "Cancelled."
            self._report(force=True)

    def _start(self, task: dict[str, Any]) -> None:
        self._task, self._message = task, ""

    def _advance_task(self) -> None:
        """Move a measurement on once a full window of fresh frames is in."""
        task, assist = self._task, self.assist
        if task is None or assist.frames < assist.window:
            return
        if task["kind"] == "noise":
            terms = assist.raw_terms()
            if task["first"] is None:
                task["first"] = terms
                assist.clear()
            else:
                assist.deadband = deadband_from(task["first"], terms)
                self._measured = assist.deadband
                self._task = None
                self._message = "Tolerance measured: readings inside it count as OK."
        elif task["stage"] == "before":
            task["before"] = assist.raw_terms()[task["term"]]
            task["stage"] = "turn"
        elif task["stage"] == "after":
            pot, term = task["pot"], task["term"]
            change = assist.raw_terms()[term] - task["before"]
            self._task = None
            if abs(change) <= (assist.deadband or 1e-9):
                self._message = (f"{term} didn't change by more than the noise. Turn {pot} a little further, "
                                 "then press Learn again.")
            else:
                sign = 1 if change > 0 else -1
                assist.polarity[pot] = sign
                self._learned = (pot, sign)
                self._message = f"Learned: turning {pot} clockwise {'raises' if sign > 0 else 'lowers'} {term}."
        else:
            return  # waiting for Done
        self._report(force=True)

    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_status:
            return
        self._next_status = now + STATUS_INTERVAL
        assist, task = self.assist, self._task
        prompt, waiting = self._message, False
        if task is not None:
            if task["kind"] == "noise":
                prompt = f"Measuring the noise ({1 if task['first'] is None else 2} of 2): don't touch anything…"
            elif task["stage"] == "before":
                prompt = f"Reading {task['term']} before you turn {task['pot']}: hold still…"
            elif task["stage"] == "turn":
                prompt = (f"Now turn {task['pot']} a little clockwise, let the picture settle, then press Done.")
                waiting = True
            else:
                prompt = f"Reading {task['term']} again: hold still…"
        status = PotStatus(assist.mode, assist.read(), min(1.0, assist.frames / assist.window),
                           task["kind"] if task else "", prompt, waiting, self._learned, assist.deadband,
                           self._measured)
        self._learned = self._measured = None  # each is reported once
        try:
            self._on_status(status)
        except Exception:
            log.exception("Pot Assist's status update failed")
