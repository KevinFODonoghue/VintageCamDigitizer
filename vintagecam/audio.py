"""
Audio from the Elgato's line input, through Windows kernel streaming (WDM-KS).

**Why not the DirectShow audio input the brief planned?**  On this PC every normal
Windows audio route refuses the Elgato's audio input.  DirectShow, waveIn (MME),
DirectSound and WASAPI all go through the Windows audio engine, and the engine has
no usable format for this endpoint: the driver turns down every stream it's asked
for (``AUDCLNT_E_UNSUPPORTED_FORMAT``).  *Kernel streaming* talks to the driver's
audio filter directly, underneath the engine, and that works.  PortAudio
implements it; the ``sounddevice`` package wraps PortAudio for Python.

**Two quirks, both measured on the target machine:**

* The chip always delivers **48 000 samples per second**, whatever rate is asked
  for.  Asking for 44 100 still yields about 48 000 a second, silently mislabelled,
  so the recording would play 9% slow.  We always ask for exactly 48 000.
* Real sound only flows **while the video stream is running**: the capture thread
  routes the crossbar's "Audio Line" input to the card's audio decoder when it
  opens the device.  With video closed the stream still runs, but every sample is
  zero.  So audio is started after video is live, and stopped first.

**Plugs.**  The input is stereo: the white RCA plug is the left channel, the red
plug the right.  Both are always opened; ``AudioCapture`` passes on both (stereo,
the default) or just one plug's channel, as mono (``config.AUDIO_PLUGS``).

**Timing.**  PortAudio calls ``_callback`` on its own thread with each block of
samples.  The recorder needs to know when every sample was *captured*, on the
``time.perf_counter()`` clock the video uses.  This host API doesn't report capture
times, and callbacks can arrive late when the PC is busy (Python runs them, and
another thread may be holding Python's lock), so callback times can't be used
directly.  Instead the samples are counted: sample *n* was captured at
T0 + n / 48000, where T0 is found from the callbacks in a way a late callback
can't disturb (see ``_callback``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import AUDIO_PLUGS

log = logging.getLogger(__name__)

AUDIO_RATE = 48_000
#: The card's input is stereo; both channels are always opened (see AUDIO_PLUGS).
AUDIO_CHANNELS = 2

#: How much sound PortAudio may hold while Python is busy elsewhere.  Enough to
#: ride out a long pause of the program without losing samples.
AUDIO_BUFFER_SECONDS = 0.4

#: Seconds of callbacks the sample clock is anchored over (see _callback).
_ANCHOR_WINDOW = 5.0

AUTO = "auto"
"""Setting value meaning "the Elgato's line input, found automatically"."""

#: Host APIs worth offering.  MME and DirectSound are older layers over the same
#: engine as WASAPI and add nothing; WDM-KS is the one that reaches the Elgato.
_HOST_APIS = ("Windows WDM-KS", "Windows WASAPI")
_ELGATO_NAME = "analog audio in"
_scanned = False


class AudioError(Exception):
    """The audio input could not be opened."""


def _sd() -> Any:
    """Import sounddevice on first use.

    Importing it starts PortAudio, which briefly opens every audio driver to list
    its devices — the Elgato's included.  Doing that while the video device is
    being opened has made the card report "in use", so it only happens when the
    app asks for it (at start-up, before video opens).
    """
    import sounddevice

    return sounddevice


@dataclass(frozen=True)
class AudioInput:
    index: int
    """PortAudio's device number (valid until the next re-scan)."""
    name: str
    host_api: str

    @property
    def key(self) -> str:
        """Stable identifier stored in settings.json."""
        return f"{self.host_api}::{self.name}"

    @property
    def is_elgato(self) -> bool:
        return self.host_api == "Windows WDM-KS" and _ELGATO_NAME in self.name.lower()

    @property
    def label(self) -> str:
        if self.is_elgato:
            return "Elgato line input (kernel streaming)"
        return f"{self.name}  ({self.host_api.replace('Windows ', '')})"


def list_inputs(refresh: bool = False) -> list[AudioInput]:
    """Inputs the recorder can use — kernel-streaming and WASAPI devices, Elgato first.

    ``refresh`` makes PortAudio re-scan (e.g. after the card was replugged).  Don't
    use it while an audio stream is open.
    """
    global _scanned
    sd = _sd()
    if refresh and _scanned:
        # sounddevice has no public "re-scan"; restarting PortAudio is how it's done.
        sd._terminate()
        sd._initialize()
    _scanned = True
    apis = sd.query_hostapis()
    inputs = [
        AudioInput(index, dev["name"], apis[dev["hostapi"]]["name"])
        for index, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0 and apis[dev["hostapi"]]["name"] in _HOST_APIS
    ]
    inputs.sort(key=lambda d: (not d.is_elgato, d.host_api != "Windows WDM-KS", d.name.lower()))
    return inputs


def find_input(key: str, inputs: list[AudioInput] | None = None) -> AudioInput | None:
    """Resolve a settings value to a device.  "auto", or a device that's gone, means the Elgato."""
    inputs = list_inputs() if inputs is None else inputs
    if key and key != AUTO:
        for dev in inputs:
            if dev.key == key:
                return dev
    return next((dev for dev in inputs if dev.is_elgato), None)


class AudioCapture:
    """Streams 16-bit PCM from one input to a sink (the recorder), with capture times."""

    def __init__(self, device: AudioInput, rate: int = AUDIO_RATE, plug: str = "both") -> None:
        self.device = device
        self.rate = rate
        self.plug = plug
        """Which plug(s) to pass on: a key of config.AUDIO_PLUGS."""
        self._columns = list(AUDIO_PLUGS[plug])
        self.channels = len(self._columns)
        """Channels in each block the sink gets: 1 for one plug, 2 for both."""
        self.sink: Callable[[np.ndarray, float], object] | None = None
        """Called with ``(samples, capture_time)`` for every block, on PortAudio's
        thread: ``samples`` is a (frames, channels) int16 array the sink may keep,
        ``capture_time`` is when its first sample was digitised (perf_counter
        clock).  It must return quickly.  Can be swapped at any time."""
        self.frames = 0
        self.overflows = 0
        self._stream: Any = None
        self._lock = threading.Lock()
        self._peak = 0
        self._peak_at = 0.0
        self._first_at: float | None = None
        self._last_at = 0.0
        self._counted = 0
        self._samples = 0  # samples received so far
        self._anchors: deque[tuple[float, float]] = deque()  # (callback time, upper bound for T0)

    def start(self, sink: Callable[[np.ndarray, float], object] | None = None) -> None:
        if sink is not None:
            self.sink = sink
        sd = _sd()
        stream = None
        try:
            stream = sd.InputStream(device=self.device.index, samplerate=self.rate, channels=AUDIO_CHANNELS,
                                    dtype="int16", latency=AUDIO_BUFFER_SECONDS, callback=self._callback)
            stream.start()
        except Exception as exc:  # PortAudioError, ValueError for a stale device number, …
            if stream is not None:
                stream.close()
            raise AudioError(f"couldn't open {self.device.label}: {exc}") from exc
        self._stream = stream
        plugs = "red + white plugs" if self.plug == "both" else f"{self.plug} plug"
        log.info("Audio input open: %s, %d Hz, recording the %s, %.0f ms of buffering",
                 self.device.label, self.rate, plugs, stream.latency * 1000)

    def stop(self) -> None:
        self.sink = None
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()  # don't wait for queued buffers; nobody wants them now
            stream.close()
        except Exception as exc:
            log.warning("Closing the audio input raised: %s", exc)

    @property
    def running(self) -> bool:
        return self._stream is not None

    def level_dbfs(self) -> float:
        """Loudest sample of roughly the last half second, in dB below full scale.

        0 dB is the loudest a 16-bit sample can be (clipping); −inf is digital silence.
        """
        with self._lock:
            peak = self._peak if time.perf_counter() - self._peak_at < 0.6 else 0
        return 20 * math.log10(peak / 32768) if peak else float("-inf")

    def measured_rate(self) -> float | None:
        """Samples per second actually arriving, once there are ~2 s to judge by."""
        if self._first_at is None or self._last_at - self._first_at < 2.0:
            return None
        return self._counted / (self._last_at - self._first_at)

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        now = time.perf_counter()
        if status.input_overflow:  # the driver had to throw samples away
            self.overflows += 1
            self._anchors.clear()  # the count no longer matches the clock: re-anchor
        first = self._samples
        self._samples += frames
        # Sample n was captured at T0 + n/rate.  A callback can only arrive *after*
        # its last sample was captured, so each one gives an upper bound for T0:
        # arrival - (samples so far)/rate.  A busy PC makes callbacks late, which only
        # raises the bound, so the smallest bound of the last few seconds is T0 —
        # hiccups can't move it.  (Recent bounds only, so a slightly fast or slow
        # audio clock is followed too.)
        self._anchors.append((now, now - self._samples / self.rate))
        while now - self._anchors[0][0] > _ANCHOR_WINDOW:
            self._anchors.popleft()
        t0 = min(bound for _, bound in self._anchors)
        block = indata[:, self._columns]  # the chosen plug's channel(s), as a copy
        peak = max(int(block.max()), -int(block.min())) if block.size else 0
        with self._lock:
            if peak >= self._peak or now - self._peak_at > 0.5:
                self._peak, self._peak_at = peak, now
        if self._first_at is None:
            self._first_at = now
            log.debug("First audio block: %d frames, currentTime %.4f, inputBufferAdcTime %.4f",
                      frames, time_info.currentTime, time_info.inputBufferAdcTime)
        else:
            self._counted += frames
        self._last_at = now
        self.frames += frames
        sink = self.sink
        if sink is not None:
            sink(block, t0 + first / self.rate)
