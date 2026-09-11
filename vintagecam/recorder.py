"""
RecordThread — lossless FFV1 video, plus optional PCM audio, into Matroska (.mkv).

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

**Audio.**  When an audio input is running (see ``audio.py``), uncompressed
16-bit PCM is stored next to the video.  Keeping it in step with the picture is
the fiddly part; see ``_write_audio``.

**Never block capture.**  Frames and audio blocks arrive through one bounded
queue, and ``offer()`` never waits.  If the disk falls more than
``QUEUE_SECONDS`` behind, new items are dropped *and counted*, and the UI shows
the count in red.  (FFV1 SD needs only ~12 MB/s, so on an SSD this shouldn't
happen.)
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from av.video.reformatter import ColorRange

from .color import uyvy_to_planar
from .frames import CapturedFrame
from .video_format import VideoStandard

log = logging.getLogger(__name__)

#: How many seconds of video may queue up in RAM if the disk is slow (~20 MB/s of RAM).
QUEUE_SECONDS = 10.0

#: The proven encoder settings, as FFmpeg option strings.
FFV1_OPTIONS = {"level": "3", "g": "1", "slices": "16", "slicecrc": "1"}

#: Roughly how long after the middle of a frame's scan it reaches the capture
#: thread: half a frame (the rest of the scan) plus ~30 ms of USB and driver.
#: Used to line audio up with the picture; settings.json's av_sync_offset_ms
#: fine-tunes it.
VIDEO_CAPTURE_DELAY = 0.045

#: Seconds of frames over which the driver's clock is matched to the PC's (see _write).
VIDEO_CLOCK_WINDOW = 5.0

#: Audio is held within this many seconds of where the video says it belongs.
AUDIO_SYNC_TOLERANCE = 0.010

#: A jump bigger than this means sound was really lost; the hole becomes silence.
AUDIO_GAP_THRESHOLD = 0.25

#: Queue room for audio blocks, which arrive some 50–100 times a second.
_AUDIO_BLOCKS_PER_SECOND = 100

#: After Stop, how long to wait for sound that's still in the audio buffers (it
#: arrives up to ~0.4 s after it was captured) before closing the file anyway.
AUDIO_DRAIN_TIMEOUT = 1.5

#: Queued by stop(): everything before it in the queue was offered before Stop.
_STOP_MARKER = ("stop",)


@dataclass(frozen=True)
class RecordingResult:
    path: Path
    frames_written: int
    frames_dropped: int
    device_gaps: int
    duration: float
    error: str | None
    """None if the recording finished cleanly, otherwise what went wrong."""
    audio_seconds: float | None = None
    """Seconds of sound in the file, or None if it has no audio track."""
    audio_adjustments: int = 0
    """Single samples dropped or repeated to hold sync (a few per minute is normal)."""
    audio_gaps: int = 0
    """Places where sound was lost and replaced by silence (should be 0)."""


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
    """Writes every frame (and audio block) it's offered to one MKV file, then finalises it."""

    def __init__(
        self,
        path: Path,
        standard: VideoStandard,
        *,
        on_finished: Callable[[RecordingResult], None] | None = None,
        comment: str = "",
        audio_rate: int | None = None,
        audio_channels: int = 2,
        video_delay: float = VIDEO_CAPTURE_DELAY,
        av_offset: float = 0.0,
    ) -> None:
        # Not a daemon thread: Python waits for it at exit, so a file is never
        # abandoned half-written just because the window closed.
        super().__init__(name="RecordThread", daemon=False)
        self.path = path
        self.standard = standard
        self.audio_rate = audio_rate
        """Sample rate of the audio track, or None for a video-only file."""
        self.audio_channels = audio_channels
        self.video_delay = video_delay
        self.av_offset = av_offset
        """Extra seconds to delay the sound by (positive) or advance it (negative)."""
        self._on_finished = on_finished
        self._comment = comment
        per_second = standard.fps + (_AUDIO_BLOCKS_PER_SECOND if audio_rate else 0)
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=max(1, int(QUEUE_SECONDS * per_second)))
        self._accepting = True
        self._video_open = True  # False once stop() is called: no more frames
        self._video_done = False  # every frame offered before stop() has been written
        self._drain_deadline: float | None = None
        self._stop_requested = threading.Event()
        self.opened = threading.Event()
        """Set once the output file is open — or has failed to open (see ``error``)."""

        self.frames_written = 0
        self.frames_dropped = 0
        self.device_gaps = 0
        self.audio_samples_written = 0
        self.audio_blocks_dropped = 0
        self.audio_adjustments = 0
        self.audio_gaps = 0
        self.error: str | None = None

        self._container: av.container.OutputContainer | None = None
        self._stream: av.video.stream.VideoStream | None = None
        self._astream: av.audio.stream.AudioStream | None = None
        self._last_time: float | None = None
        self._last_index = -1
        self._video_ref: tuple[int, float] | None = None  # (latest frame's index, its capture time)
        self._clock_offsets: deque[tuple[float, float]] = deque()  # (device time, arrival - device time)
        self._audio_next: int | None = None  # file position of the next audio sample
        self._drift = 0.0  # smoothed distance between where audio is and where it belongs

    # -- called from other threads ------------------------------------------------

    def offer(self, frame: CapturedFrame) -> bool:
        """Queue a frame for writing.  Never blocks (it's called by the capture thread)."""
        if not self._accepting or not self._video_open:
            return False
        try:
            self._queue.put_nowait(("v", frame))
            return True
        except queue.Full:
            self.frames_dropped += 1
            return False

    def offer_audio(self, samples: np.ndarray, captured: float) -> bool:
        """Queue an audio block.  Never blocks (it's called on PortAudio's thread)."""
        if not self._accepting or not self.audio_rate:
            return False
        try:
            self._queue.put_nowait(("a", samples, captured))
            return True
        except queue.Full:
            self.audio_blocks_dropped += 1
            return False

    def stop(self) -> None:
        """Stop taking frames, write whatever is queued, then finalise the file.

        Sound runs a little behind the picture — it waits in the audio buffers for
        up to ~0.4 s — so audio is still accepted until it has caught up with the
        last frame (or AUDIO_DRAIN_TIMEOUT passes), and is then trimmed to end
        exactly with the picture.  Keep the audio input running until the
        recorder has finished.
        """
        self._video_open = False
        self._stop_requested.set()
        if not self.audio_rate:
            self._accepting = False
        try:
            self._queue.put(_STOP_MARKER, timeout=1.0)
        except queue.Full:
            pass  # the writer notices the stop request once the queue empties

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    @property
    def duration(self) -> float:
        """Seconds of video in the file so far (gaps included)."""
        return (self._last_index + 1) * self.standard.frame_duration

    @property
    def audio_seconds(self) -> float | None:
        return self.audio_samples_written / self.audio_rate if self.audio_rate else None

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
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._stop_requested.is_set():
                        self._video_done = True
                        if self._drained():
                            break
                    continue
                if item[0] == "v":
                    self._write(item[1])
                elif item[0] == "a":
                    self._write_audio(item[1], item[2])
                else:  # the stop marker: every frame from before stop() is written
                    self._video_done = True
                    if self._drained() and self._queue.empty():
                        break
            assert self._stream is not None and self._container is not None
            for packet in self._stream.encode(None):  # flush the encoders
                self._container.mux(packet)
            if self._astream is not None:
                for packet in self._astream.encode(None):
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
            if self.audio_rate:
                layout = "stereo" if self.audio_channels == 2 else "mono"
                astream = container.add_stream("pcm_s16le", rate=self.audio_rate, layout=layout)
                astream.codec_context.time_base = Fraction(1, self.audio_rate)
                self._astream = astream
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

        # When was this frame captured, on the perf_counter clock the audio uses?
        # arrival_time can be late if the PC was busy; device_time is the driver's
        # stamp (DirectShow's clock) and isn't.  The two clocks differ by a fixed
        # offset plus each frame's delivery delay, so the smallest difference seen
        # recently is the offset itself — a late arrival can't move it.
        self._clock_offsets.append((frame.device_time, frame.arrival_time - frame.device_time))
        while frame.device_time - self._clock_offsets[0][0] > VIDEO_CLOCK_WINDOW:
            self._clock_offsets.popleft()
        offset = min(o for _, o in self._clock_offsets)
        self._video_ref = (index, frame.device_time + offset - self.video_delay)

        video_frame = av.VideoFrame.from_ndarray(uyvy_to_planar(frame.uyvy), format="yuv422p")
        video_frame.pts = index
        video_frame.time_base = self._stream.codec_context.time_base
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)
        self.frames_written += 1

    def _write_audio(self, samples: np.ndarray, captured: float) -> None:
        """Place an audio block on the video's timeline and encode it.

        The video timeline counts frames at exactly 29.97 per second, but the
        camera's real frame rate and the card's audio clock both differ from
        their nominal values by tens of parts per million.  Left alone, the sound
        would slide out of step by a few tenths of a second per hour.

        So each block's rightful position is worked out from *when it was
        captured*, measured from the latest video frame.  If the running sample
        count wanders more than AUDIO_SYNC_TOLERANCE from that (smoothed, so the
        jitter of callback timing is ignored), one sample per block is dropped or
        repeated until it's back — far too small to hear.  A real hole (lost
        samples) is filled with silence instead.
        """
        if self._video_ref is None or self._astream is None or not len(samples):
            return  # no picture to line up with yet
        if self._video_done and self._audio_next is not None:
            # Stop was pressed and the last frame is written: just top the sound up to
            # the end of the picture (_encode_audio trims the rest).  There's no more
            # picture to follow, so no sync logic — and no false "lost sound" alarms.
            self._encode_audio(samples)
            return
        rate = self.audio_rate
        assert rate is not None
        ref_index, ref_captured = self._video_ref
        seconds = ref_index * self.standard.frame_duration + (captured - ref_captured) + self.av_offset
        rightful = round(seconds * rate)

        if self._audio_next is None:  # first block: start exactly where it belongs
            if rightful < 0:  # it began before the first frame; drop that part
                samples = samples[-rightful:]
                rightful = 0
                if not len(samples):
                    return
            self._audio_next = rightful
            self._drift = 0.0
        else:
            error = rightful - self._audio_next  # > 0: sound is behind the picture
            if abs(error) > AUDIO_GAP_THRESHOLD * rate:
                self.audio_gaps += 1
                self._drift = 0.0
                if error < 0:
                    log.warning("Audio ran %.2f s ahead of the picture; skipped a block", -error / rate)
                    return
                log.warning("%.2f s of sound was lost; filled with silence", error / rate)
                self._encode_audio(np.zeros((error, samples.shape[1]), dtype=np.int16))
            else:
                self._drift += 0.05 * (error - self._drift)
                limit = AUDIO_SYNC_TOLERANCE * rate
                # At most one sample in a thousand (0.1%) per block: far more than
                # real clock errors need (tens to a few hundred ppm), far too little
                # to hear.  Scaled with the block, because blocks can be large.
                most = max(1, len(samples) // 1000)
                if self._drift > limit:  # behind: repeat the last samples
                    samples = np.concatenate([samples, np.repeat(samples[-1:], most, axis=0)])
                    self.audio_adjustments += most
                elif self._drift < -limit and len(samples) > most:  # ahead: drop a few
                    samples = samples[:-most]
                    self.audio_adjustments += most
        self._encode_audio(samples)

    def _video_end_sample(self) -> int:
        """Where the picture ends, as an audio sample position."""
        assert self.audio_rate is not None
        return round((self._last_index + 1) * self.standard.frame_duration * self.audio_rate)

    def _drained(self) -> bool:
        """After stop(): has the sound caught up with the last frame (or waited long enough)?"""
        if self._astream is None or self._audio_next is None or self._last_index < 0:
            self._accepting = False
            return True
        if self._drain_deadline is None:
            self._drain_deadline = time.monotonic() + AUDIO_DRAIN_TIMEOUT
        if self._audio_next >= self._video_end_sample() or time.monotonic() >= self._drain_deadline:
            self._accepting = False
            return True
        return False

    def _encode_audio(self, samples: np.ndarray) -> None:
        assert self._astream is not None and self._container is not None and self._audio_next is not None
        if self._video_done:  # the picture has ended: sound stops with it
            room = self._video_end_sample() - self._audio_next
            if room <= 0:
                return
            samples = samples[:room]
        layout = "stereo" if samples.shape[1] == 2 else "mono"
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples).reshape(1, -1), format="s16", layout=layout)
        frame.sample_rate = self.audio_rate
        frame.pts = self._audio_next
        frame.time_base = Fraction(1, self.audio_rate)
        for packet in self._astream.encode(frame):
            self._container.mux(packet)
        self._audio_next += len(samples)
        self.audio_samples_written += len(samples)

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
            self.path, self.frames_written, self.frames_dropped, self.device_gaps, self.duration, self.error,
            self.audio_seconds, self.audio_adjustments, self.audio_gaps + self.audio_blocks_dropped,
        )
        if self._on_finished is not None:
            try:
                self._on_finished(result)
            except Exception:
                log.exception("Recording-finished callback failed")
