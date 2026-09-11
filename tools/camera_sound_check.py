"""
Camera sound check: proves that sound reaching the camera is recorded correctly.

The camera's sound comes into the Elgato through its RCA audio plugs (its picture
through the yellow one).  This plays two tones out of the laptop's speakers, for
the camera's microphone to pick up, while the app's own capture and recorder code
records video and sound from the Elgato, then analyses the file it wrote:

    0–2 s   1000 Hz
    2–3 s   quiet
    3–5 s    440 Hz

Both tones must come back clearly above the surrounding sound, at the right
pitch — which also proves the recording's 48 kHz label is true: had the card
really run at another rate, they'd come back sharp or flat — and without
clipping.  It reports which plug (red, white or both) carried them.

    .venv\\Scripts\\python tools\\camera_sound_check.py

Close the app first (the card can only stream to one program), switch the camera
on, put it near the laptop, and set the laptop's volume to about half.  The tones
are audible: that's the point.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import numpy as np  # noqa: E402

from vintagecam import audio as audio_io  # noqa: E402
from vintagecam.capture import CaptureRequest, CaptureState, CaptureThread  # noqa: E402
from vintagecam.config import DEFAULT_VIDEO_DEVICE  # noqa: E402
from vintagecam.recorder import RecordThread, make_recording_path  # noqa: E402
from vintagecam.video_format import NTSC  # noqa: E402

RATE = 48_000
TONES = (1000.0, 440.0)
TONE_SECONDS, GAP_SECONDS = 2.0, 1.0
AMPLITUDE = 0.25  # -12 dBFS before Windows' volume control
PRESENT_DB = -70.0  # a tone at least this loud (dBFS)...
CLEAR_DB = 15.0  # ...and this far above the sound around it counts as heard
PLUGS = (("white", 0), ("red", 1))  # the Elgato's stereo input: white = left, red = right

_failures = 0


def check(name: str, ok: bool, detail: str) -> bool:
    global _failures
    _failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def tone_sequence() -> np.ndarray:
    """The tones, the same on both speakers, with 10 ms fades so they don't click."""
    t = np.arange(int(TONE_SECONDS * RATE)) / RATE
    fade = np.minimum(1.0, np.minimum(t, TONE_SECONDS - t) / 0.01)
    first, second = (AMPLITUDE * fade * np.sin(2 * np.pi * f * t) for f in TONES)
    mono = np.concatenate([first, np.zeros(int(GAP_SECONDS * RATE)), second])
    return np.column_stack([mono, mono]).astype(np.float32)


def default_output(sd) -> tuple[int | None, str, object]:
    """Windows' default speakers, through WASAPI where possible: (device, name, extra settings)."""
    for api in sd.query_hostapis():
        if api["name"] == "Windows WASAPI" and api["default_output_device"] >= 0:
            index = api["default_output_device"]
            return index, sd.query_devices(index)["name"], sd.WasapiSettings(auto_convert=True)
    return None, sd.query_devices(kind="output")["name"], None


def spectrum(x: np.ndarray) -> np.ndarray:
    return np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x))))


def strongest(spec: np.ndarray, n: int, lo_hz: float, hi_hz: float) -> tuple[float, float]:
    """Level (dBFS) and exact frequency of the strongest sine between two frequencies.

    ``spec`` is the ``spectrum()`` of ``n`` samples.
    """
    lo = max(int(lo_hz * n / RATE), 1)
    hi = min(int(np.ceil(hi_hz * n / RATE)) + 1, len(spec) - 1)
    j = lo + int(np.argmax(spec[lo:hi]))
    a, b, c = np.log(spec[j - 1:j + 2] + 1e-12)
    shift = 0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c else 0.0  # parabolic peak interpolation
    amplitude = 4 * spec[j] / n  # a sine of amplitude A peaks at A·n/4 under a Hann window
    return 20 * np.log10(max(amplitude, 1e-9) / 32768), (j + shift) * RATE / n


def background(spec: np.ndarray, n: int, freq: float) -> float:
    """Typical level (dBFS, same scale as strongest()) of the sound near ``freq``, leaving the tone out."""
    away = np.abs(np.arange(len(spec)) * RATE / n - freq)
    near = (away < 0.10 * freq) & (away > 0.03 * freq)
    return 20 * np.log10(max(4 * float(np.median(spec[near])) / n, 1e-9) / 32768)


def analyse(path: Path) -> None:
    with av.open(str(path)) as container:
        frames = list(container.decode(container.streams.audio[0]))
    s = np.concatenate([f.to_ndarray().reshape(-1, 2) for f in frames]).astype(np.float64)
    window, hop = RATE // 2, RATE // 4
    heard: dict[tuple[str, float], list[int]] = {(plug, f): [] for plug, _ in PLUGS for f in TONES}
    loudest = {plug: (-999.0, 0.0) for plug, _ in PLUGS}  # the strongest sound on each plug: (dBFS, Hz)
    for i in range(0, len(s) - window, hop):
        for plug, ch in PLUGS:
            spec = spectrum(s[i:i + window, ch])
            for f in TONES:
                level = strongest(spec, window, f * 0.99, f * 1.01)[0]
                if level >= PRESENT_DB and level - background(spec, window, f) >= CLEAR_DB:
                    heard[plug, f].append(i)
            loudest[plug] = max(loudest[plug], strongest(spec, window, 100, 5000))

    def measure(ch: int, starts: list[int], freq: float) -> tuple[float, float, float]:
        """Level (dBFS), pitch (Hz), and height above the sound around it (dB), over the tone's middle second."""
        middle = starts[len(starts) // 2] + window // 2
        x = s[max(0, middle - RATE // 2):middle + RATE // 2, ch]
        spec = spectrum(x)
        level, pitch = strongest(spec, len(x), freq * 0.99, freq * 1.01)
        return level, pitch, level - background(spec, len(x), freq)

    # Heard for a second or more; shorter finds are only a tone's edges, or chance.
    found = {key: len(starts) >= 4 for key, starts in heard.items()}
    carried = [plug for plug, _ in PLUGS if all(found[plug, f] for f in TONES)]
    if carried:
        where = "on both plugs, red and white" if len(carried) == 2 else f"on the {carried[0]} plug only"
    else:
        def described(level: float, freq: float) -> str:
            text = f"{freq:.0f} Hz at {level:.0f} dBFS"
            for tone in TONES:  # a loud sound near a tone: that tone, off pitch, so the rate label is wrong
                off = freq / tone - 1
                if level >= -50 and 0.01 < abs(off) < 0.15:
                    text += f" (the {tone:.0f} Hz tone, {abs(off):.1%} {'flat' if off < 0 else 'sharp'}?)"
            return text

        partly = [f"{f:.0f} Hz on {plug}" for (plug, f), ok in found.items() if ok]
        where = ((f"heard only {', '.join(partly)}; " if partly else "")
                 + "; ".join(f"loudest on {plug} (100 Hz-5 kHz): {described(*loud)}" for plug, loud in loudest.items())
                 + " - is the camera on and near the laptop, the laptop's volume up and not muted, "
                   "and the camera's audio lead in the Elgato?")
    if not check("both tones came back through the camera", bool(carried), where):
        return

    channel = dict(PLUGS)
    plug = max(carried, key=lambda p: measure(channel[p], heard[p, TONES[0]], TONES[0])[0])  # the louder plug
    results = [measure(channel[plug], heard[plug, f], f) for f in TONES]
    check("pitch (proves the 48 kHz label)", all(abs(p / f - 1) < 0.005 for (_, p, _), f in zip(results, TONES)),
          ", ".join(f"{p:.2f} Hz for {f:.0f}" for (_, p, _), f in zip(results, TONES)))
    clear = all(height >= 20 for _, _, height in results)
    check("clear of the other sound", clear,
          ", ".join(f"{f:.0f} Hz at {level:.0f} dBFS, {height:.0f} dB above the sound around it"
                    for (level, _, height), f in zip(results, TONES))
          + ("" if clear else "  - move the camera closer or turn the laptop up"))
    peaks = {p: 20 * np.log10(max(np.abs(s[:, c]).max(), 1) / 32768) for p, c in PLUGS}
    worst = max(peaks[p] for p in carried)
    check("no clipping", worst < -0.2, ", ".join(f"{p} plug peak {peaks[p]:z.1f} dBFS" for p in carried)
          + ("  - turn the laptop's volume down" if worst >= -0.2 else ""))
    if len(carried) == 2:
        other = "white" if plug == "red" else "red"
        quieter = results[0][0] - measure(channel[other], heard[other, TONES[0]], TONES[0])[0]
        print("       the red and white plugs carried them at the same level" if quieter < 1 else
              f"       the {other} plug carried them {quieter:.0f} dB quieter than the {plug} plug")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEFAULT_VIDEO_DEVICE)
    parser.add_argument("--out", type=Path, default=Path.home() / "Videos" / "VintageCam")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    # All PortAudio work (it scans every audio driver) happens before video opens.
    sd = audio_io._sd()
    out_index, out_name, out_settings = default_output(sd)
    elgato = audio_io.find_input(audio_io.AUTO, audio_io.list_inputs())
    if not check("Elgato audio input", elgato is not None,
                 elgato.label if elgato else "not found - is the Elgato plugged in?"):
        return 1

    running = threading.Event()
    last_state = [""]

    def on_state(state: CaptureState, message: str, _error) -> None:
        last_state[0] = message
        if state == CaptureState.RUNNING:
            running.set()

    capture = CaptureThread(CaptureRequest(args.device, NTSC), on_state=on_state,
                            on_stats=lambda _s: None, on_frame=lambda: None)
    capture.start()
    try:
        if not check("video running", running.wait(20),
                     "needed: the card's audio only runs while video streams" if running.is_set()
                     else f"not after 20 s: {last_state[0]}"):
            return 1
        time.sleep(0.5)
        frame = capture.preview_slot.take()
        spread = float(frame.uyvy[:, 1::2].std()) if frame is not None else 0.0
        if not check("camera picture", spread >= 2.0,
                     f"live (luma spread {spread:.0f}), so the camera is on" if spread >= 2.0 else
                     "none - switch the camera on (its microphone needs it) and check the yellow plug"):
            return 1
        audio = audio_io.AudioCapture(elgato, plug="both")  # both plugs, to see which one carries the sound
        audio.start()
        path = make_recording_path(args.out, "camera_sound")
        finished = []
        recorder = RecordThread(path, NTSC, on_finished=finished.append, audio_rate=audio.rate,
                                audio_channels=audio.channels, comment="tools/camera_sound_check.py")
        recorder.start()
        recorder.opened.wait(10)
        capture.set_record_sink(recorder)
        audio.sink = recorder.offer_audio
        time.sleep(0.7)
        played = ""
        try:
            sd.play(tone_sequence(), samplerate=RATE, device=out_index, blocking=True, extra_settings=out_settings)
        except Exception as exc:  # PortAudioError etc.
            played = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)  # the last of the sound is still on its way
        capture.set_record_sink(None)
        recorder.stop()
        recorder.join(60)
        audio.stop()
        if not check("tones played", not played, f"on {out_name}" if not played else played):
            return 1
        result = finished[0]
        if not check("recording", result.error is None and bool(result.audio_seconds),
                     f"{path.name}: picture {result.duration:.1f} s, sound {result.audio_seconds or 0:.1f} s"):
            return 1
        analyse(path)
    finally:
        capture.stop()
        capture.join(10)
    print("\nCAMERA SOUND VERIFIED" if _failures == 0 else f"\n{_failures} CHECK(S) FAILED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
