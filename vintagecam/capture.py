"""
CaptureThread — the one and only owner of the capture device.

Why exactly one owner?  A DirectShow capture device streams to one place at a
time: a second ``av.open`` (or a second program, such as an ffplay window) gets
"Could not run graph".  So one thread opens the device and everyone else gets
frames from it::

    CaptureThread ──► record sink ───► RecordThread     every frame, in order
                  ├─► preview slot ──► GUI              newest only (drop-on-late)
                  └─► analysis slot ─► AnalysisThread   newest only (Phase 2)

This loop must never stall.  If it does, DirectShow queues frames in FFmpeg's
real-time buffer: the preview starts showing the past and, if the buffer fills,
frames are lost from the recording.  So per frame this thread does only cheap
bookkeeping: no colour conversion, no drawing, no disk I/O.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import av
import av.logging
import numpy as np

from . import dshow
from .config import CROSSBAR_AUDIO_LINE_PIN, CROSSBAR_VIDEO_PINS, VIDEO_INPUT_LABELS
from .errors import (
    CaptureError,
    DeviceLostError,
    DeviceNotFoundError,
    UnsupportedFormatError,
    classify_open_error,
)
from .frames import CapturedFrame, LatestSlot
from .video_format import VideoStandard

log = logging.getLogger(__name__)

#: How often to check for a new frame.  We poll rather than block (see
#: ``_read_loop``), so this is the most latency polling can add.  Python 3.11+
#: on Windows sleeps with sub-millisecond accuracy.
POLL_INTERVAL = 0.002

#: No frame for this long while streaming means the device stopped delivering.
STALL_TIMEOUT = 3.0

#: How often statistics are sent to the GUI.
STATS_INTERVAL = 0.5

#: libavformat's AVFMT_FLAG_NONBLOCK (avformat.h): reads return EAGAIN instead of waiting.
_AVFMT_FLAG_NONBLOCK = 0x0004

_open_lock = threading.Lock()


def open_dshow(url: str, options: dict[str, str], device: str, what: str) -> av.container.InputContainer:
    """``av.open`` a DirectShow device, turning failures into specific CaptureErrors.

    PyAV normally discards FFmpeg's log (level None).  We switch it on only while
    opening, so a failed open comes with FFmpeg's explanation ("Could not run
    graph…").  It's switched off again straight after, deliberately: with logging
    on, FFmpeg's own DirectShow threads call into Python for every log line,
    which costs time while streaming and can deadlock while the device closes.
    """
    with _open_lock:
        # PyAV hides a log line identical to the previous one; on a retry loop
        # that would swallow the very message we need to classify the failure.
        av.logging.set_skip_repeated(False)
        av.logging.set_level(av.logging.ERROR)
        try:
            with av.logging.Capture(local=True) as logs:
                try:
                    return av.open(url, format="dshow", options=options)
                except Exception as exc:
                    lines = [str(entry[-1]).strip() for entry in logs]
                    raise classify_open_error(exc, lines, device, what) from exc
        finally:
            av.logging.set_level(None)


class CaptureState(str, Enum):
    STOPPED = "stopped"
    OPENING = "opening"
    RUNNING = "running"
    WAITING = "waiting"  # device missing or busy; retrying automatically
    FAILED = "failed"  # needs the user to change something


@dataclass(frozen=True)
class CaptureRequest:
    """What to open.  A new request means closing and reopening the device."""

    device_name: str
    standard: VideoStandard
    video_input: str = "composite"
    rtbufsize: str = "512M"
    device_index: int = 0

    def dshow_options(self) -> dict[str, str]:
        std = self.standard
        return {
            "video_size": f"{std.width}x{std.height}",
            "pixel_format": "uyvy422",
            # "29.97", never "30000/1001" — see VideoStandard.dshow_framerate.
            "framerate": std.dshow_framerate,
            "rtbufsize": self.rtbufsize,
            "video_device_number": str(self.device_index),
            "crossbar_video_input_pin_number": str(CROSSBAR_VIDEO_PINS[self.video_input]),
            # Route "Audio Line" too: after a replug this card was found with its
            # audio decoder unrouted, which leaves the audio path dead.
            "crossbar_audio_input_pin_number": str(CROSSBAR_AUDIO_LINE_PIN),
        }

    def describe(self) -> str:
        return f"{VIDEO_INPUT_LABELS[self.video_input]} · {self.standard.describe()}"


@dataclass(frozen=True)
class CaptureStats:
    fps: float
    """Frames per second actually arriving (should read 29.97)."""
    frames: int
    """Frames received since the device was opened."""
    device_drops: int
    """Frames missing from the device's sequence (gaps in its timestamps)."""
    preview_skipped: int
    """Frames the screen didn't have time to show.  Harmless: the preview
    always jumps to the newest frame."""
    interval_jitter_ms: float
    """Spread of the time between frames, from DirectShow's timestamps."""


class RecordSink(Protocol):
    def offer(self, frame: CapturedFrame) -> bool: ...


def _uyvy_view(frame: av.VideoFrame) -> np.ndarray:
    """The frame's bytes as a (height, width*2) array — a view, not a copy.

    The array keeps the PyAV frame alive, so the bytes stay valid for as long
    as anyone holds it.  Marked read-only because three threads share it.
    """
    plane = frame.planes[0]
    height, row = frame.height, frame.width * 2
    buf = np.frombuffer(plane, dtype=np.uint8)
    arr = buf[: plane.line_size * height].reshape(height, plane.line_size)[:, :row]
    arr.flags.writeable = False
    return arr


class CaptureThread(threading.Thread):
    """Opens the device, reads frames forever, fans them out, and reconnects on its own.

    Callbacks run *on this thread*; the GUI turns them into Qt signals.
    """

    def __init__(
        self,
        request: CaptureRequest,
        *,
        on_state: Callable[[CaptureState, str, CaptureError | None], None],
        on_stats: Callable[[CaptureStats], None],
        on_frame: Callable[[], None],
    ) -> None:
        super().__init__(name="CaptureThread", daemon=True)
        self.request = request
        self._on_state = on_state
        self._on_stats = on_stats
        self._on_frame = on_frame

        self.preview_slot: LatestSlot[CapturedFrame] = LatestSlot()
        self.analysis_slot: LatestSlot[CapturedFrame] = LatestSlot()
        self._record_sink: RecordSink | None = None

        self._stop_requested = threading.Event()
        self._wake = threading.Event()
        self.state = CaptureState.STOPPED

        self.frames = 0
        self.device_drops = 0
        self._last_time: float | None = None
        self._arrivals: deque[float] = deque(maxlen=90)
        self._intervals: deque[float] = deque(maxlen=90)
        self._next_stats = 0.0

    # -- called from other threads --------------------------------------------

    def set_record_sink(self, sink: RecordSink | None) -> None:
        """Start (or, with None, stop) handing every frame to a recorder."""
        self._record_sink = sink  # a single attribute assignment is atomic in Python

    def stop(self) -> None:
        self._stop_requested.set()
        self._wake.set()

    def retry_now(self) -> None:
        """Cut a retry wait short (e.g. the user closed the other program)."""
        self._wake.set()

    # -- thread body ------------------------------------------------------------

    def run(self) -> None:
        try:
            with dshow.com_initialized():
                self._run()
        except Exception as exc:  # a crash here must be visible, not silent
            log.exception("Capture thread crashed")
            self._set_state(CaptureState.FAILED, f"Capture stopped by an internal error: {exc}", CaptureError(str(exc)))
            return
        self._set_state(CaptureState.STOPPED, "Capture stopped.")

    def _run(self) -> None:
        req = self.request
        while not self._stop_requested.is_set():
            if not self._device_present():
                err = DeviceNotFoundError(
                    f"“{req.device_name}” is not connected. Plug it in — capture starts automatically."
                )
                self._set_state(CaptureState.WAITING, err.message, err)
                self._sleep(err.retry_after)
                continue

            if self.state not in (CaptureState.WAITING, CaptureState.FAILED):
                # On a retry, keep showing *why* we're retrying instead of flashing "Opening…".
                self._set_state(CaptureState.OPENING, f"Opening “{req.device_name}”…")
            try:
                container = self._open()
            except CaptureError as err:
                failed = isinstance(err, UnsupportedFormatError) or err.retry_after is None
                self._set_state(CaptureState.FAILED if failed else CaptureState.WAITING, err.message, err)
                self._sleep(err.retry_after)
                continue

            try:
                self._set_state(CaptureState.RUNNING, f"Live — {req.describe()}")
                self._read_loop(container)
            except CaptureError as err:
                self._set_state(CaptureState.WAITING, err.message, err)
                self._sleep(err.retry_after)
            finally:
                self._close(container)

    def _device_present(self) -> bool:
        """Cheap presence check before trying a (slow, noisy) av.open."""
        try:
            devices = dshow.list_video_devices()
        except dshow.DShowError as exc:
            log.debug("Device enumeration failed (%s); trying to open anyway", exc)
            return True
        return any(d.name == self.request.device_name and d.index == self.request.device_index for d in devices)

    def _open(self) -> av.container.InputContainer:
        req, std = self.request, self.request.standard
        container = open_dshow(f"video={req.device_name}", req.dshow_options(), req.device_name, "video")
        try:
            cc = container.streams.video[0].codec_context
            fmt = cc.format.name if cc.format is not None else "?"
            if (cc.width, cc.height, fmt) != (std.width, std.height, "uyvy422"):
                raise UnsupportedFormatError(
                    f"“{req.device_name}” delivered {cc.width}×{cc.height} {fmt}, "
                    f"not {std.width}×{std.height} uyvy422."
                )
            # Non-blocking reads: see _read_loop for why.  (PyAV exposes the
            # flags as a plain int, so OR in libavformat's AVFMT_FLAG_NONBLOCK.)
            container.flags = container.flags | _AVFMT_FLAG_NONBLOCK
        except BaseException:
            self._close(container)
            raise
        return container

    def _read_loop(self, container: av.container.InputContainer) -> None:
        """Read frames until stopped, or raise DeviceLostError.

        Why poll in non-blocking mode instead of simply blocking on each read?
        FFmpeg's DirectShow reader waits *forever* for the next frame and ignores
        PyAV's timeout.  A device that silently stops sending (flaky USB, driver
        hiccup) would freeze this thread for good, and "Stop" would hang.  In
        non-blocking mode an empty read returns immediately (BlockingIOError),
        so we can notice a stall, honour a stop request, and try again 2 ms later.
        """
        stream = container.streams.video[0]
        self.frames = self.device_drops = 0
        self._last_time = None
        self._arrivals.clear()
        self._intervals.clear()
        self.preview_slot.replaced = 0
        last_frame = time.perf_counter()

        while not self._stop_requested.is_set():
            try:
                for packet in container.demux(stream):
                    for frame in packet.decode():  # rawvideo "decoding" just wraps the bytes
                        self._deliver(frame)
                        last_frame = time.perf_counter()
                    if self._stop_requested.is_set():
                        return
                raise DeviceLostError(f"“{self.request.device_name}” stopped sending video (end of stream).")
            except av.error.BlockingIOError:
                now = time.perf_counter()
                if now - last_frame > STALL_TIMEOUT:
                    raise DeviceLostError(
                        f"No video from “{self.request.device_name}” for {STALL_TIMEOUT:.0f} s. "
                        "Was it unplugged? Reconnecting…"
                    ) from None
                self._maybe_emit_stats(now)
                time.sleep(POLL_INTERVAL)
            except av.error.FFmpegError as exc:
                # DirectShow reports the driver's "device lost" event as an I/O error.
                raise DeviceLostError(
                    f"“{self.request.device_name}” stopped responding — was it unplugged? Reconnecting…",
                    str(exc),
                ) from exc

    def _deliver(self, frame: av.VideoFrame) -> None:
        now = time.perf_counter()
        std = self.request.standard
        if (frame.width, frame.height, frame.format.name) != (std.width, std.height, "uyvy422"):
            raise UnsupportedFormatError(
                f"The device switched to {frame.width}×{frame.height} {frame.format.name} mid-stream."
            )

        t = frame.time if frame.time is not None else now
        if self._last_time is not None:
            interval = t - self._last_time
            self._intervals.append(interval)
            # ±5 ms timestamp jitter is far below half a frame (16.7 ms), so
            # rounding tells a normal frame (1) from a skipped one (2+) reliably.
            missing = round(interval / std.frame_duration) - 1
            if missing > 0:
                self.device_drops += missing
        self._last_time = t

        captured = CapturedFrame(self.frames, t, now, _uyvy_view(frame), frame, std)
        self.frames += 1
        self._arrivals.append(now)

        sink = self._record_sink
        if sink is not None:
            sink.offer(captured)  # never blocks; the recorder counts its own overflow
        self.analysis_slot.put(captured)
        if self.preview_slot.put(captured):
            self._on_frame()
        self._maybe_emit_stats(now)

    def _maybe_emit_stats(self, now: float) -> None:
        if now < self._next_stats:
            return
        self._next_stats = now + STATS_INTERVAL
        fps = 0.0
        if len(self._arrivals) >= 2 and self._arrivals[-1] > self._arrivals[0]:
            fps = (len(self._arrivals) - 1) / (self._arrivals[-1] - self._arrivals[0])
            if now - self._arrivals[-1] > 1.0:  # nothing arrived for a while
                fps = 0.0
        jitter = float(np.std(self._intervals) * 1000) if len(self._intervals) >= 2 else 0.0
        self._on_stats(CaptureStats(fps, self.frames, self.device_drops, self.preview_slot.replaced, jitter))

    def _close(self, container: av.container.InputContainer) -> None:
        try:
            container.close()
        except Exception as exc:
            log.warning("Closing the capture device raised: %s", exc)

    def _sleep(self, seconds: float | None) -> None:
        """Wait before retrying; returns early on stop() or retry_now()."""
        self._wake.wait(seconds)
        if not self._stop_requested.is_set():
            self._wake.clear()

    def _set_state(self, state: CaptureState, message: str, error: CaptureError | None = None) -> None:
        self.state = state
        if error is not None and error.detail:  # FFmpeg's raw wording: log file only
            log.debug("%s — %s", message, error.detail.replace("\n", " | "))
        try:
            self._on_state(state, message, error)
        except Exception:
            log.exception("Capture state callback failed")
