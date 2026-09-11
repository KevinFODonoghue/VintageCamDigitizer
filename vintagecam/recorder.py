"""
RecordThread — lossless FFV1 recording into Matroska (.mkv).

**FFV1** is a lossless, intra-frame video codec built for archiving.  It's an IETF
standard (RFC 9043), used by national film archives.  Lossless means decoding
the file gives back exactly the bytes the capture card delivered, including every
bit of noise and every artefact.  That's what a digitizer should keep: any
clean-up can be done later, from a perfect copy.

The encoder settings are the proven command line's
(``-c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1``):

  level 3     FFV1 version 3, which adds slices and checksums.
  g 1         every frame is a keyframe, so damage to one frame can't spread
              into the next.
  slices 16   each frame is split into 16 independently coded pieces.  The
              encoder works on them in parallel on several CPU cores, and any
              corruption stays inside 1/16 of one frame.
  slicecrc 1  a CRC checksum per slice, so the file can be proven intact years
              from now (``ffmpeg -i file.mkv -f null -`` reports damaged slices).

**UYVY → planar.**  FFV1 can't store packed UYVY, so each frame is repacked to
planar yuv422p (``color.uyvy_to_planar``).  That's a pure byte shuffle — the
same thing the ffmpeg command line does silently — so nothing is rounded.

**Never block capture.**  Frames arrive through a bounded queue and ``offer()``
never waits.  If the disk falls more than ``QUEUE_SECONDS`` behind, new frames
are dropped *and counted*, and the UI shows the count in red.  (FFV1 SD needs
only ~12 MB/s, so on an SSD this shouldn't happen.)
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import av
from av.video.reformatter import ColorRange

from .color import uyvy_to_planar
from .frames import CapturedFrame
from .video_format import VideoStandard

log = logging.getLogger(__name__)

#: How many seconds of video may queue up in RAM if the disk is slow (~20 MB/s of RAM).
QUEUE_SECONDS = 10.0

#: The proven encoder settings, as FFmpeg option strings.
FFV1_OPTIONS = {"level": "3", "g": "1", "slices": "16", "slicecrc": "1"}


@dataclass(frozen=True)
class RecordingResult:
    path: Path
    frames_written: int
    frames_dropped: int
    device_gaps: int
    duration: float
    error: str | None
    """None if the recording finished cleanly, otherwise what went wrong."""


def make_recording_path(folder: str | Path, prefix: str, when: datetime | None = None) -> Path:
    """``<folder>/<prefix>_YYYYMMDD_HHMMSS.mkv``, never overwriting an existing file."""
    prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", prefix).strip(" .") or "capture"
    stem = f"{prefix}_{(when or datetime.now()):%Y%m%d_%H%M%S}"
    folder = Path(folder)
    path, n = folder / f"{stem}.mkv", 2
    while path.exists():
        path, n = folder / f"{stem}_{n}.mkv", n + 1
    return path


def free_disk_bytes(folder: str | Path) -> int | None:
    """Free space on the drive holding ``folder`` (which may not exist yet)."""
    path = Path(folder)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


class RecordThread(threading.Thread):
    """Writes every frame it's offered to one FFV1/MKV file, then finalises it."""

    def __init__(
        self,
        path: Path,
        standard: VideoStandard,
        *,
        on_finished: Callable[[RecordingResult], None] | None = None,
        comment: str = "",
    ) -> None:
        # Not a daemon thread: Python waits for it at exit, so a file is never
        # abandoned half-written just because the window closed.
        super().__init__(name="RecordThread", daemon=False)
        self.path = path
        self.standard = standard
        self._on_finished = on_finished
        self._comment = comment
        self._queue: queue.Queue[CapturedFrame] = queue.Queue(maxsize=max(1, int(QUEUE_SECONDS * standard.fps)))
        self._accepting = True
        self._stop_requested = threading.Event()
        self.opened = threading.Event()
        """Set once the output file is open — or has failed to open (see ``error``)."""

        self.frames_written = 0
        self.frames_dropped = 0
        self.device_gaps = 0
        self.error: str | None = None

        self._container: av.container.OutputContainer | None = None
        self._stream: av.video.stream.VideoStream | None = None
        self._last_time: float | None = None
        self._last_index = -1

    # -- called from other threads ------------------------------------------------

    def offer(self, frame: CapturedFrame) -> bool:
        """Queue a frame for writing.  Never blocks (it's called by the capture thread)."""
        if not self._accepting:
            return False
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            self.frames_dropped += 1
            return False

    def stop(self) -> None:
        """Stop taking frames, write whatever is queued, then finalise the file."""
        self._accepting = False
        self._stop_requested.set()

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    @property
    def duration(self) -> float:
        """Seconds of video in the file so far (gaps included)."""
        return (self._last_index + 1) * self.standard.frame_duration

    def file_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    # -- thread body ----------------------------------------------------------------

    def run(self) -> None:
        try:
            self._open()
        except Exception as exc:
            log.exception("Could not create %s", self.path)
            self.error = f"Could not create {self.path}: {exc}"
            self._accepting = False
            self.opened.set()
            self._finish()
            return
        self.opened.set()
        log.info("Recording to %s", self.path)

        try:
            while True:
                try:
                    frame = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
                    continue
                self._write(frame)
            assert self._stream is not None and self._container is not None
            for packet in self._stream.encode(None):  # flush the encoder
                self._container.mux(packet)
        except Exception as exc:
            self._accepting = False
            self.error = self._describe(exc)
            log.error("Recording to %s failed: %s", self.path, exc)
        finally:
            self._close()
            self._finish()

    def _open(self) -> None:
        std = self.standard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        container = av.open(str(self.path), mode="w", format="matroska")
        try:
            stream = container.add_stream("ffv1", rate=std.frame_rate, options=dict(FFV1_OPTIONS))
            stream.width, stream.height = std.width, std.height
            stream.pix_fmt = "yuv422p"
            cc = stream.codec_context
            cc.time_base = 1 / std.frame_rate  # timestamps count frames: 0, 1, 2, …
            cc.thread_type = "SLICE"  # encode the 16 slices of a frame in parallel
            cc.thread_count = 0  # 0 = one thread per CPU core
            # Colour description (metadata only) so players pick the SD matrix.
            cc.color_range = ColorRange.MPEG
            cc.color_primaries = std.color_primaries
            cc.color_trc = std.color_trc
            cc.colorspace = std.colorspace
            if self._comment:
                container.metadata["comment"] = self._comment
        except BaseException:
            container.close()
            raise
        self._container, self._stream = container, stream

    def _write(self, frame: CapturedFrame) -> None:
        """Encode one frame.  Its timestamp is a frame count, not the clock.

        Timestamps count frames (0, 1, 2 …) so the file is exactly 29.97 fps.  If
        the device skipped frames (a gap in its own timestamps), we skip the same
        number of counts, so what follows stays in the right place in time.
        """
        assert self._stream is not None and self._container is not None
        if self._last_time is None:
            index = 0
        else:
            step = max(1, round((frame.device_time - self._last_time) / self.standard.frame_duration))
            self.device_gaps += step - 1
            index = self._last_index + step
        self._last_time, self._last_index = frame.device_time, index

        video_frame = av.VideoFrame.from_ndarray(uyvy_to_planar(frame.uyvy), format="yuv422p")
        video_frame.pts = index
        video_frame.time_base = self._stream.codec_context.time_base
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)
        self.frames_written += 1

    def _close(self) -> None:
        if self._container is None:
            return
        try:
            self._container.close()  # writes the index and duration at the end of the file
        except Exception as exc:
            if self.error is None:
                self.error = self._describe(exc)
            log.error("Finalising %s failed: %s", self.path, exc)
        self._container = None

    def _describe(self, exc: BaseException) -> str:
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            return (
                f"The disk is full. Recording stopped; everything up to that point is saved in {self.path.name}."
            )
        return f"Writing {self.path.name} failed: {exc}"

    def _finish(self) -> None:
        result = RecordingResult(
            self.path, self.frames_written, self.frames_dropped, self.device_gaps, self.duration, self.error
        )
        if self._on_finished is not None:
            try:
                self._on_finished(result)
            except Exception:
                log.exception("Recording-finished callback failed")
