"""
Hardware check: runs the app's real capture, proc-amp and recording code against
the capture card and prints PASS/FAIL for each step.  Use it to tell app bugs
from hardware problems (alongside the known-good ffmpeg commands in docs/SETUP.md).

    .venv\\Scripts\\python tools\\hardware_check.py                 (about 30 seconds)
    .venv\\Scripts\\python tools\\hardware_check.py --no-procamp     (never touch the proc amp)

Close anything else using the card first (ffplay, OBS, the app itself…).

The proc amp step briefly lowers the card's brightness, several times, to time
how long a change takes to reach the software — a direct measurement of capture
latency.  It always puts brightness back to where it was.
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import numpy as np  # noqa: E402

from vintagecam import dshow  # noqa: E402
from vintagecam.capture import CaptureRequest, CaptureState, CaptureThread  # noqa: E402
from vintagecam.color import uyvy_to_planar  # noqa: E402
from vintagecam.config import DEFAULT_VIDEO_DEVICE  # noqa: E402
from vintagecam.dshow import ProcAmp  # noqa: E402
from vintagecam.frames import CapturedFrame  # noqa: E402
from vintagecam.recorder import RecordThread, make_recording_path  # noqa: E402
from vintagecam.video_format import NTSC  # noqa: E402

_failures = 0


def check(name: str, ok: bool, detail: str) -> bool:
    global _failures
    _failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def skip(name: str, detail: str) -> None:
    print(f"[SKIP] {name}: {detail}", flush=True)


class Probe:
    """A record sink that notes every frame, and can forward frames to a recorder."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: list[tuple[int, float, float, float]] = []  # index, device time, arrival, centre luma
        self.recorder: RecordThread | None = None
        self.sent: list[CapturedFrame] = []  # first frames given to the recorder (for a byte comparison)

    def offer(self, frame: CapturedFrame) -> bool:
        luma = float(frame.uyvy[200:280, 601:841:2].mean())  # odd bytes = Y samples of a centre patch
        with self.lock:
            self.samples.append((frame.index, frame.device_time, frame.arrival_time, luma))
        recorder = self.recorder
        if recorder is None:
            return True
        if len(self.sent) < 5:
            self.sent.append(frame)
        return recorder.offer(frame)

    def since(self, t0: float) -> list[tuple[int, float, float, float]]:
        with self.lock:
            return [s for s in self.samples if s[2] >= t0]


def measure_procamp(probe: Probe, controls: dshow.VideoDeviceControls, rng: dshow.ProcAmpRange,
                    base: int) -> None:
    low = max(rng.minimum, base - 1500)
    latencies: list[float] = []
    steps: list[float] = []
    try:
        for target in [low, base] * 4:
            time.sleep(0.7)
            before = [s[3] for s in probe.since(time.perf_counter() - 0.3)]
            t_set = time.perf_counter()
            controls.set_proc_amp(ProcAmp.BRIGHTNESS, target)
            time.sleep(0.6)
            after = probe.since(t_set)
            settled = [s[3] for s in after if s[2] > t_set + 0.35]
            if not before or not settled:
                continue
            b, a = float(np.mean(before)), float(np.mean(settled))
            steps.append(a - b)
            mid = (a + b) / 2
            first = next((s for s in after if (s[3] - mid) * (a - b) > 0), None)
            if first is not None and abs(a - b) > 5:
                latencies.append((first[2] - t_set) * 1000)
    finally:
        controls.set_proc_amp(ProcAmp.BRIGHTNESS, base)
        restored = controls.get_proc_amp(ProcAmp.BRIGHTNESS)
    check("proc amp restored", restored == base, f"brightness back to {restored}")
    check("proc amp acts on the live stream", bool(steps) and all(abs(d) > 5 for d in steps),
          "centre-luma change per step: " + ", ".join(f"{d:+.1f}" for d in steps))
    if latencies:
        med = statistics.median(latencies)
        check("capture latency", med < 100,
              f"brightness change -> frame in software: median {med:.0f} ms "
              f"(min {min(latencies):.0f}, max {max(latencies):.0f}; includes up to one frame of "
              "waiting for the decoder to apply the change)")


def record_with_audio(probe: Probe, device, args: argparse.Namespace) -> None:
    """Record video and sound together, then check the sound track."""
    from vintagecam.audio import AudioCapture, AudioError

    audio = AudioCapture(device)
    try:
        audio.start()
    except AudioError as exc:
        check("audio opens", False, str(exc))
        return
    check("audio opens", True, f"{device.label}, asked for {audio.rate} Hz")
    path = make_recording_path(args.out, "hwcheck_av")
    finished = []
    recorder = RecordThread(path, NTSC, on_finished=finished.append, comment="tools/hardware_check.py (audio)",
                            audio_rate=audio.rate, audio_channels=audio.channels)
    recorder.start()
    recorder.opened.wait(10)
    probe.recorder = recorder
    audio.sink = recorder.offer_audio
    time.sleep(args.seconds / 2)
    # Freeze Python for 0.3 s — a DLL call that keeps Python's lock, as a heavy GUI
    # redraw would.  Sound must neither be lost nor be mistaken for lost.
    ctypes.PyDLL("kernel32").Sleep(300)
    time.sleep(args.seconds / 2)
    measured, overflows = audio.measured_rate(), audio.overflows
    probe.recorder = None  # the picture ends here, as when you press Stop
    recorder.stop()  # the recorder still takes the sound waiting in the audio buffers…
    recorder.join(60)
    audio.stop()  # …so the input is only stopped once the file is closed
    result = finished[0]
    check("audio recorded through a 0.3 s freeze",
          result.error is None and bool(result.audio_seconds) and result.audio_gaps == 0 and overflows == 0,
          f"picture {result.duration:.2f} s, sound {result.audio_seconds or 0:.2f} s, "
          f"{result.audio_adjustments} sync adjustments, {result.audio_gaps} gaps, {overflows} input overflows")
    check("audio sample rate", measured is not None and abs(measured / audio.rate - 1) < 0.01,
          f"{measured:,.0f} samples/s arriving, {audio.rate:,} expected" if measured else "not measured")
    with av.open(str(path)) as container:
        frames = list(container.decode(container.streams.audio[0]))
    samples = (np.concatenate([f.to_ndarray().reshape(-1, audio.channels) for f in frames])
               if frames else np.zeros((0, audio.channels), np.int16))
    live = np.count_nonzero(samples) / max(1, samples.size)
    peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
    check("audio is live", live > 0.5, f"{live:.0%} of samples non-zero (digital silence would be 0%), peak {peak}/32767")
    start = frames[0].time if frames else 0.0
    end = start + len(samples) / audio.rate
    check("sound lines up with picture", bool(frames) and abs(end - result.duration) < 0.05,
          f"sound runs {start:.3f}-{end:.3f} s, picture ends at {result.duration:.3f} s")
    print(f"\n       recording with sound: {path}")


def main() -> int:
    # A redirected Windows console uses a legacy code page that can't print σ or —.
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEFAULT_VIDEO_DEVICE)
    parser.add_argument("--seconds", type=float, default=10.0, help="length of the test recording")
    parser.add_argument("--out", type=Path, default=Path.home() / "Videos" / "VintageCam")
    parser.add_argument("--no-procamp", action="store_true", help="don't change the proc amp")
    args = parser.parse_args()

    # 1. DirectShow: device list, proc amp and decoder, via our own COM code.
    names = [d.name for d in dshow.list_video_devices()]
    if not check("device listed", args.device in names, f"video devices: {names}"):
        return 1
    controls = dshow.VideoDeviceControls(args.device)
    controls.open()
    ranges = {p: controls.proc_amp_range(p) for p in ProcAmp}
    values = {p: controls.get_proc_amp(p) for p, r in ranges.items() if r is not None}
    check("proc amp readable", bool(values),
          ", ".join(f"{p.label} {v} (neutral {ranges[p].default})" for p, v in values.items()))
    neutral = all(v == ranges[p].default for p, v in values.items())
    check("proc amp at neutral", neutral, "all at the driver's defaults" if neutral else "NOT at neutral")
    have_signal = controls.horizontal_locked()
    check("decoder status", True, f"TV format 0x{controls.tv_format():X}, {controls.number_of_lines()} lines, "
          f"signal {'locked' if have_signal else 'NOT locked'}")
    if not have_signal:
        print("       NO SIGNAL: is the camera on and connected? Checks that need a picture are skipped.")

    # Audio inputs are listed now, before video opens: PortAudio's scan briefly
    # opens the card's audio filter, which must not collide with opening video.
    elgato_audio = None
    try:
        from vintagecam import audio as audio_io

        elgato_audio = audio_io.find_input(audio_io.AUTO, audio_io.list_inputs())
    except Exception as exc:
        print(f"       (audio inputs couldn't be listed: {exc})")
    check("audio input listed", elgato_audio is not None,
          elgato_audio.label if elgato_audio else "no kernel-streaming 'Analog Audio In' input found")

    # 2. Streaming through the real CaptureThread.
    probe = Probe()
    running = threading.Event()

    def on_state(state: CaptureState, message: str, _error: object) -> None:
        print(f"       capture state: {state.value} — {message}", flush=True)
        if state == CaptureState.RUNNING:
            running.set()

    capture = CaptureThread(CaptureRequest(args.device, NTSC), on_state=on_state,
                            on_stats=lambda _s: None, on_frame=lambda: None)
    capture.set_record_sink(probe)
    t_open = time.perf_counter()
    capture.start()
    try:
        if not check("device opened", running.wait(20), f"{time.perf_counter() - t_open:.2f} s to open"):
            return 1
        time.sleep(6.0)
        samples = probe.since(0.0)
        device_t = np.array([s[1] for s in samples])
        arrival = np.array([s[2] for s in samples])
        intervals = np.diff(device_t)
        gaps = int(sum(max(0, round(i / NTSC.frame_duration) - 1) for i in intervals))
        fps_dev = (len(device_t) - 1) / (device_t[-1] - device_t[0])
        fps_arr = (len(arrival) - 1) / (arrival[-1] - arrival[0])
        detail = (f"{fps_dev:.3f} fps by device clock, {fps_arr:.3f} fps by arrival; timestamp jitter "
                  f"σ {np.std(intervals) * 1000:.1f} ms; largest arrival gap {np.max(np.diff(arrival)) * 1000:.0f} ms")
        if have_signal:
            check("frame rate", abs(fps_dev - NTSC.fps) < 0.05, detail)
        else:
            skip("frame rate", detail + " (without a signal the decoder free-runs)")
        check("no dropped frames", gaps == 0, f"{gaps} gaps in {len(samples)} frames")

        # 3. Proc amp on the live stream, and capture latency.
        if not args.no_procamp and ProcAmp.BRIGHTNESS in values:
            if have_signal:
                measure_procamp(probe, controls, ranges[ProcAmp.BRIGHTNESS], values[ProcAmp.BRIGHTNESS])
            else:
                skip("proc amp on the live stream / capture latency", "needs a picture to measure")

        # 4. A real recording, checked frame by frame.
        path = make_recording_path(args.out, "hwcheck")
        finished = []
        recorder = RecordThread(path, NTSC, on_finished=finished.append, comment="tools/hardware_check.py")
        recorder.start()
        recorder.opened.wait(10)
        if not check("recording started", recorder.error is None and recorder.opened.is_set(), str(path)):
            return 1
        probe.recorder = recorder
        time.sleep(args.seconds)
        probe.recorder = None
        recorder.stop()
        recorder.join(60)
        result = finished[0]
        size = path.stat().st_size
        check("recording finished", result.error is None,
              f"{result.frames_written} frames, {result.frames_dropped} lost, {result.device_gaps} skipped by "
              f"device, {size / 1e6:.1f} MB = {size / max(result.duration, 1e-9) * 3600 / 1024**3:.1f} GB/h")

        decoded, all_key, count = [], True, 0
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            cc = stream.codec_context
            for frame in container.decode(stream):
                if count < len(probe.sent):
                    decoded.append(frame.to_ndarray())
                all_key &= frame.key_frame
                count += 1
        check("file decodes", count == result.frames_written and all_key,
              f"{count} frames, {cc.name} {cc.format.name} {cc.width}x{cc.height} @ {stream.average_rate}, "
              f"all keyframes: {all_key}")
        exact = bool(decoded) and all(np.array_equal(d, uyvy_to_planar(s.uyvy)) for d, s in zip(decoded, probe.sent))
        check("bit-exact", exact, f"first {len(decoded)} recorded frames identical, byte for byte, to what the card sent")
        print(f"\n       recording: {path}")

        # 5. Sound: the Elgato's line input via kernel streaming, recorded with the picture.
        if elgato_audio is not None:
            record_with_audio(probe, elgato_audio, args)
    finally:
        capture.stop()
        capture.join(10)
        controls.close()

    print("\nALL CHECKS PASSED" if _failures == 0 else f"\n{_failures} CHECK(S) FAILED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
