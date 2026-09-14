"""
MP4 viewing copies: turn a lossless recording into a file that plays anywhere.

**Why a second file.**  Recordings are FFV1, a lossless *archival* codec.  VLC
plays it, but Windows' Media Player, phones, TVs and browsers don't ("encoded in
Unknown format").  The export writes a separate, much smaller copy in the most
widely supported format there is: H.264 video and AAC sound in an .mp4.  The
.mkv is only read, never changed: it stays the lossless original.

**What happens to the picture, and why:**

* *Deinterlacing.*  An analog video frame is two *fields* woven together: the
  even lines and the odd lines, captured 1/59.94 s apart.  On a TV that's
  invisible; on a computer screen a moving edge shows as a comb.  The ``bwdif``
  filter turns every field into a whole frame, filling in the missing lines from
  its neighbours in space and time, so the copy has 59.94 progressive frames a
  second: motion as smooth as on the TV.
* *Field order.*  For that, bwdif must know which field of each frame came
  first.  The export measures it with FFmpeg's ``idet`` (interlace detector),
  which needs movement to tell.  In a still picture it can't, but then the order
  makes no visible difference; the TV standard's usual order is used.  Choosing
  an order in View → Bob field order overrides the measurement.
* *Square pixels.*  NTSC pixels are 10/11 as wide as they are tall, and players
  assume square ones, so the picture is scaled to 654 × 480: what the preview
  shows with "Correct pixel aspect".
* *Colour.*  4:2:0 chroma (half the colour resolution vertically too, which is
  what players expect), BT.601 colours, limited range: tagged, so players show
  the colours as recorded.

**Quality.**  H.264 High profile at CRF 18: constant *quality* rather than a
fixed bit rate, set around the point where the copy is hard to tell from the
original.  Sound is AAC at 192 kb/s (stereo) or 128 kb/s (mono).

The app runs each export as a separate process at below-normal priority
(``main.py --export``, see ui/export_queue.py), so a live capture or recording
always gets the computer first.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import av.filter
from av.video.reformatter import ColorRange

from .config import FIELD_ORDERS, app_dir
from .units import format_bytes, format_duration
from .video_format import STANDARDS, VideoStandard

#: x264's constant-quality setting: lower is better and bigger.  At 18 the copy
#: is hard to tell from the original.
CRF = 18
#: How hard x264 searches for a compact encoding; slower presets make smaller files.
PRESET = "medium"
AUDIO_BIT_RATES = {1: 128_000, 2: 192_000}
AAC_FRAME = 1024  # samples per AAC frame

#: How much of a recording the field-order measurement looks at.
DETECT_SECONDS = 30.0
#: Its verdict counts only when it's clear: enough frames decided, and one order
#: this many times more common than the other.
DETECT_MIN_FRAMES = 20
DETECT_MARGIN = 3.0


class ExportError(Exception):
    """The copy couldn't be made."""


class ExportCancelled(ExportError):
    """Stopped on request.  Nothing is left behind."""


@dataclass(frozen=True)
class ExportResult:
    path: Path
    frames: int
    """Frames in the copy: two for every recorded frame (one per field)."""
    duration: float
    field_order: str
    field_order_reason: str
    size: int
    took: float
    """Seconds the export took."""


def output_path(source: Path) -> Path:
    """The copy sits next to the recording, under the same name: capture_….mp4."""
    return Path(source).with_suffix(".mp4")


def partial_path(destination: Path) -> Path:
    """Where the copy is written until it's complete."""
    return destination.with_name(destination.name + ".part")


def square_pixel_width(std: VideoStandard) -> int:
    """Width that shows the picture undistorted with square pixels (even, as 4:2:0 needs)."""
    return round(std.width * std.pixel_aspect / 2) * 2


def _standard_for(width: int, height: int) -> VideoStandard:
    for std in STANDARDS.values():
        if (std.width, std.height) == (width, height):
            return std
    raise ExportError(f"{width}×{height} isn't an NTSC or PAL recording")


def _pull(graph: av.filter.Graph) -> Iterator[av.VideoFrame]:
    """Every frame the filter graph has ready."""
    while True:
        try:
            yield graph.pull()
        except (BlockingIOError, EOFError):  # it needs more input, or it's finished
            return


def detect_field_order(source: Path, seconds: float = DETECT_SECONDS) -> tuple[str | None, dict[str, float]]:
    """Which field comes first, measured from motion: ("tff" or "bff", counts), or (None, counts).

    FFmpeg's idet filter compares each field with its neighbours: in the right
    order a moving edge moves steadily; in the wrong one it jumps back and forth.
    ``counts`` are its verdicts over several frames: tff, bff, progressive and
    undetermined (a still picture is all undetermined).
    """
    counts: dict[str, float] = {}
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        graph = av.filter.Graph()
        src, idet, sink = graph.add_buffer(template=stream), graph.add("idet"), graph.add("buffersink")
        src.link_to(idet)
        idet.link_to(sink)
        graph.configure()
        for frame in container.decode(stream):
            if frame.time is not None and frame.time > seconds:
                break
            graph.push(frame)
            for out in _pull(graph):
                counts = {key.rsplit(".", 1)[1]: float(value) for key, value in out.metadata.items()
                          if key.startswith("lavfi.idet.multiple.") and not key.endswith("current_frame")}
    tff, bff = counts.get("tff", 0.0), counts.get("bff", 0.0)
    if max(tff, bff) >= DETECT_MIN_FRAMES and max(tff, bff) >= DETECT_MARGIN * min(tff, bff):
        return ("tff" if tff > bff else "bff"), counts
    return None, counts


def choose_field_order(source: Path, setting: str, std: VideoStandard) -> tuple[str, str]:
    """The field order to deinterlace with, and why."""
    if setting in ("tff", "bff"):
        return setting, "chosen in View → Bob field order"
    try:
        detected, _ = detect_field_order(source)
    except (av.error.FFmpegError, ValueError):
        detected = None
    if detected:
        return detected, "detected from motion in the recording"
    return std.field_order, f"usual for {std.key}; too little motion to measure"


def export(source: Path, destination: Path | None = None, field_order: str = "auto",
           progress: Callable[[float], None] | None = None,
           cancel: threading.Event | None = None) -> ExportResult:
    """Write the MP4 viewing copy of ``source``.  Raises ExportError (ExportCancelled if cancelled).

    The copy is written as ``….mp4.part`` and renamed when it's complete, so a
    half-made file never looks like a finished one.
    """
    started = time.perf_counter()
    source = Path(source)
    destination = Path(destination) if destination is not None else output_path(source)
    if destination.resolve() == source.resolve():
        raise ExportError("the copy can't replace the recording")
    try:
        with av.open(str(source)) as probe:
            if not probe.streams.video:
                raise ExportError("there's no video in it")
            cc = probe.streams.video[0].codec_context
            std = _standard_for(cc.width, cc.height)
    except av.error.FFmpegError as exc:
        raise ExportError(f"couldn't read it: {exc}") from exc
    order, reason = choose_field_order(source, field_order, std)

    partial = partial_path(destination)
    try:
        with av.open(str(source)) as inp, \
                av.open(str(partial), "w", format="mp4", options={"movflags": "+faststart"}) as out:
            frames, duration = _transcode(inp, out, std, order, progress, cancel)
        os.replace(partial, destination)
    except BaseException as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, av.error.FFmpegError):
            raise ExportError(str(exc)) from exc
        raise
    return ExportResult(destination, frames, duration, order, reason, destination.stat().st_size,
                        time.perf_counter() - started)


def _transcode(inp: av.container.InputContainer, out: av.container.OutputContainer, std: VideoStandard,
               field_order: str, progress: Callable[[float], None] | None,
               cancel: threading.Event | None) -> tuple[int, float]:
    """Decode, deinterlace, scale and encode; returns (frames written, video duration)."""
    vin = inp.streams.video[0]
    ain = inp.streams.audio[0] if inp.streams.audio else None
    width, height = square_pixel_width(std), std.height
    rate = std.frame_rate * 2  # one frame per field
    tb = 1 / rate

    graph = av.filter.Graph()
    chain = [
        graph.add_buffer(template=vin),
        # deint=all: treat every frame as interlaced (FFV1 files don't carry the flag).
        graph.add("bwdif", f"mode=send_field:parity={field_order}:deint=all"),
        graph.add("scale", f"{width}:{height}:flags=lanczos"),
        graph.add("setsar", "1"),
        graph.add("format", "yuv420p"),
        graph.add("buffersink"),
    ]
    for a, b in zip(chain, chain[1:]):
        a.link_to(b)
    graph.configure()

    vout = out.add_stream("libx264", rate=rate)
    vout.width, vout.height, vout.pix_fmt = width, height, "yuv420p"
    vcc = vout.codec_context
    vcc.time_base = tb
    vcc.gop_size = round(rate * 2)  # a keyframe every 2 s, so seeking is quick
    vcc.sample_aspect_ratio = Fraction(1, 1)
    vcc.color_range = ColorRange.MPEG
    vcc.color_primaries, vcc.color_trc, vcc.colorspace = std.color_primaries, std.color_trc, std.colorspace
    vcc.options = {"crf": str(CRF), "preset": PRESET, "profile": "high"}

    aout = resampler = None
    if ain is not None:
        channels = len(ain.codec_context.layout.channels)
        layout = "stereo" if channels == 2 else "mono"
        aout = out.add_stream("aac", rate=ain.rate, layout=layout)
        aout.bit_rate = AUDIO_BIT_RATES.get(channels, AUDIO_BIT_RATES[2])
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=ain.rate, frame_size=AAC_FRAME)
    total = inp.duration / 1_000_000 if inp.duration else None  # the container counts microseconds

    state = {"last_pts": -1, "frames": 0, "audio_next": None, "reported": 0.0}

    def write_video(frame: av.VideoFrame) -> None:
        # The filter counts fields in the recording's time base; put them on an
        # exact 59.94/s grid.  (A gap the capture device left stays a gap.)
        pts = max(round(frame.time / tb), state["last_pts"] + 1)
        frame.pts, frame.time_base = pts, tb
        for packet in vout.encode(frame):
            out.mux(packet)
        state["last_pts"] = pts
        state["frames"] += 1

    def write_audio(frames: list[av.AudioFrame]) -> None:
        for af in frames:
            af.pts, af.time_base = state["audio_next"], Fraction(1, ain.rate)
            state["audio_next"] += af.samples
            for packet in aout.encode(af):
                out.mux(packet)

    for packet in inp.demux(*(s for s in (vin, ain) if s is not None)):
        if cancel is not None and cancel.is_set():
            raise ExportCancelled("cancelled")
        for frame in packet.decode():
            if packet.stream is vin:
                graph.push(frame)
                for filtered in _pull(graph):
                    write_video(filtered)
                if progress and total and time.perf_counter() - state["reported"] > 0.25:
                    state["reported"] = time.perf_counter()
                    progress(min(1.0, frame.time / total))
            else:
                if state["audio_next"] is None:  # where the sound starts, relative to the picture
                    state["audio_next"] = round(frame.time * ain.rate)
                write_audio(resampler.resample(frame))
    graph.push(None)  # the end: out come the last fields
    for filtered in _pull(graph):
        write_video(filtered)
    for packet in vout.encode(None):
        out.mux(packet)
    if aout is not None and state["audio_next"] is not None:
        write_audio(resampler.resample(None))
        for packet in aout.encode(None):
            out.mux(packet)
    if not state["frames"]:
        raise ExportError("there are no frames in it")
    if progress:
        progress(1.0)
    return state["frames"], float((state["last_pts"] + 1) * tb)


# -----------------------------------------------------------------------------
# Command line (the app runs this as a child process for each export)
# -----------------------------------------------------------------------------

def _lower_priority() -> None:
    """Let a live capture and recording have the computer first."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except (OSError, AttributeError):
        pass


def child_command(sources: list[Path], field_order: str) -> list[str]:
    """The command that exports in a separate process: ``main.py --export …``, or the packaged .exe."""
    head = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(app_dir() / "main.py")]
    return [*head, "--export", *(str(p) for p in sources), "--field-order", field_order]


def main(argv: list[str] | None = None) -> int:
    """``main.py --export RECORDING [RECORDING …]``: make viewing copies, printing progress lines."""
    parser = argparse.ArgumentParser(prog="main.py --export", description="Make an MP4 viewing copy of each recording.")
    parser.add_argument("sources", nargs="+", type=Path, metavar="RECORDING")
    parser.add_argument("--field-order", choices=FIELD_ORDERS, default="auto")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:  # whole lines, promptly, even through a pipe; never fail on an odd character
            stream.reconfigure(errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    _lower_priority()
    failed = 0
    for source in args.sources:
        print(f"exporting: {source}")
        try:
            result = export(source, field_order=args.field_order, progress=lambda f: print(f"progress: {f:.1%}"))
        except ExportError as exc:
            print(f"error: {source.name}: {exc}")
            failed += 1
            continue
        print(f"field order: {result.field_order}, {result.field_order_reason}")
        print(f"done: {result.path} ({format_bytes(result.size)}, {format_duration(result.duration)} of video, "
              f"made in {result.took:.1f} s)")
    return 1 if failed else 0
