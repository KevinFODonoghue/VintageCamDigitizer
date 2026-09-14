"""MP4 viewing copies: recordings written by the app's own recorder, exported, and checked."""

import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no visible window needed

import av  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vintagecam import export  # noqa: E402
from vintagecam.frames import frame_from_uyvy  # noqa: E402
from vintagecam.recorder import RecordThread  # noqa: E402
from vintagecam.ui.export_queue import ExportQueue  # noqa: E402
from vintagecam.video_format import NTSC  # noqa: E402

app = QApplication.instance() or QApplication([])


def grey_uyvy(luma: np.ndarray) -> np.ndarray:
    """A colourless UYVY frame (U = V = 128) with the given luma."""
    uyvy = np.full((luma.shape[0], luma.shape[1] * 2), 128, np.uint8)
    uyvy[:, 1::2] = luma
    return uyvy


def moving_bar(frames: int, bottom_first: bool = True, step: int = 6) -> list[np.ndarray]:
    """Interlaced frames of a bright bar moving right ``step`` pixels per field.

    Each frame weaves two moments: the field captured first shows the bar at
    100 + step*2k, the other at 100 + step*(2k+1).  ``bottom_first`` says which
    field (the odd lines are the bottom field) holds the earlier moment.
    """
    lumas = []
    for k in range(frames):
        y = np.full((480, 720), 40, np.uint8)
        odd, even = slice(1, None, 2), slice(0, None, 2)
        early, late = (odd, even) if bottom_first else (even, odd)
        for rows, moment in ((early, 2 * k), (late, 2 * k + 1)):
            x = 100 + step * moment
            y[rows, x:x + 24] = 220
        lumas.append(y)
    return lumas


def record(path: Path, lumas: list[np.ndarray], tone_hz: float | None = None, channels: int = 2) -> None:
    """Write a recording exactly as the app does: 29.97 fps, and sound (a tone) if asked."""
    base, fd, rate = 5000.0, NTSC.frame_duration, 48000
    events = [(base + i * fd, 0, i) for i in range(len(lumas))]
    if tone_hz:
        t, n = base, 0
        while t < base + len(lumas) * fd:
            wave = (8000 * np.sin(2 * np.pi * tone_hz * np.arange(n, n + 480) / rate)).astype(np.int16)
            events.append((t, 1, np.repeat(wave[:, None], channels, axis=1)))
            t, n = t + 480 / rate, n + 480
    events.sort(key=lambda e: (e[0], e[1]))
    last_frame = max(i for i, e in enumerate(events) if e[1] == 0)
    with mock.patch("vintagecam.recorder.AUDIO_DRAIN_TIMEOUT", 0.2):  # keep the tests quick
        recorder = RecordThread(path, NTSC, audio_rate=rate if tone_hz else None, audio_channels=channels,
                                video_delay=0.0)
        recorder.start()
        assert recorder.opened.wait(10) and recorder.error is None
        for i, (t, kind, payload) in enumerate(events):
            if kind == 0:
                recorder.offer(frame_from_uyvy(grey_uyvy(lumas[payload]), NTSC, payload, t, arrival_time=t))
            else:
                recorder.offer_audio(payload, t)
            if i == last_frame:
                recorder.stop()  # as when Stop is pressed: the last of the sound still arrives
        recorder.join(30)


def bar_positions(path: Path) -> list[float]:
    """Where the bar is in each frame of the copy, in the recording's (720-wide) pixels."""
    xs = []
    with av.open(str(path)) as container:
        for frame in container.decode(container.streams.video[0]):
            y = frame.to_ndarray()[:frame.height].astype(np.float64)  # yuv420p: the luma rows come first
            weight = np.clip(y[60:420].mean(axis=0) - 80, 0, None)
            xs.append(float((np.arange(y.shape[1]) * weight).sum() / weight.sum()) * 720 / y.shape[1])
    return xs


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_copy_is_h264_and_aac_in_mp4_with_square_pixels(self):
        source = self.folder / "rec.mkv"
        record(source, moving_bar(30), tone_hz=1000)
        result = export.export(source)
        self.assertEqual(result.path, self.folder / "rec.mp4")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["rec.mkv", "rec.mp4"])
        with av.open(str(result.path)) as container:
            self.assertIn("mp4", container.format.name)
            video, sound = container.streams.video[0], container.streams.audio[0]
            cc = video.codec_context
            self.assertEqual((cc.name, cc.format.name, cc.width, cc.height), ("h264", "yuv420p", 654, 480))
            self.assertEqual(video.average_rate, Fraction(60000, 1001))
            self.assertEqual((cc.color_range, cc.colorspace, cc.color_primaries, cc.color_trc), (1, 6, 6, 6))
            self.assertEqual((sound.codec_context.name, sound.rate, len(sound.codec_context.layout.channels)),
                             ("aac", 48000, 2))
            frames = list(container.decode(video))
        self.assertEqual((len(frames), result.frames), (60, 60))  # a frame for every field
        self.assertAlmostEqual(result.duration, 30 * NTSC.frame_duration, delta=0.02)
        with av.open(str(result.path)) as container:
            samples = np.concatenate([f.to_ndarray()[0] for f in container.decode(container.streams.audio[0])])
        self.assertAlmostEqual(len(samples) / 48000, 30 * NTSC.frame_duration, delta=0.06)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        self.assertAlmostEqual(np.argmax(spectrum) * 48000 / len(samples), 1000, delta=3)  # the tone came through

    def test_fields_come_out_in_the_order_they_were_captured(self):
        source = self.folder / "bar.mkv"
        record(source, moving_bar(20, bottom_first=True))
        right = bar_positions(export.export(source, self.folder / "right.mp4", field_order="bff").path)
        wrong = bar_positions(export.export(source, self.folder / "wrong.mp4", field_order="tff").path)
        steps_right, steps_wrong = np.diff(right[2:-2]), np.diff(wrong[2:-2])
        self.assertTrue((steps_right > 0).all(), f"the bar went backwards: {np.round(right, 1)}")
        self.assertAlmostEqual(float(np.median(steps_right)), 6, delta=2)  # 6 pixels per field
        self.assertTrue((steps_wrong < 0).any(), "the wrong order should make the bar jump back and forth")

    def test_the_field_order_is_measured_when_something_moves(self):
        bff, tff, still = (self.folder / name for name in ("bff.mkv", "tff.mkv", "still.mkv"))
        record(bff, moving_bar(40, bottom_first=True))
        record(tff, moving_bar(40, bottom_first=False))
        record(still, [np.full((480, 720), 100, np.uint8)] * 40)
        self.assertEqual(export.detect_field_order(bff)[0], "bff")
        self.assertEqual(export.detect_field_order(tff)[0], "tff")
        self.assertIsNone(export.detect_field_order(still)[0])
        result = export.export(tff)
        self.assertEqual((result.field_order, result.field_order_reason), ("tff", "detected from motion in the recording"))
        self.assertEqual(export.export(still).field_order, NTSC.field_order)  # nothing to measure: the usual order
        self.assertEqual(export.export(tff, self.folder / "chosen.mp4", field_order="bff").field_order, "bff")

    def test_a_mono_recording_gets_mono_sound(self):
        source = self.folder / "mono.mkv"
        record(source, moving_bar(15), tone_hz=440, channels=1)
        with av.open(str(export.export(source).path)) as container:
            self.assertEqual(len(container.streams.audio[0].codec_context.layout.channels), 1)

    def test_a_recording_without_sound_gets_a_silent_copy(self):
        source = self.folder / "quiet.mkv"
        record(source, moving_bar(10))
        with av.open(str(export.export(source).path)) as container:
            self.assertEqual((len(container.streams.video), len(container.streams.audio)), (1, 0))

    def test_cancelling_leaves_nothing_behind(self):
        source = self.folder / "rec.mkv"
        record(source, moving_bar(15))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(export.ExportCancelled):
            export.export(source, cancel=stop)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["rec.mkv"])

    def test_the_recording_itself_is_never_changed(self):
        source = self.folder / "rec.mkv"
        record(source, moving_bar(10))
        before = source.read_bytes()
        export.export(source)
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(export.ExportError):
            export.export(source, source)

    def test_unreadable_files_are_explained(self):
        broken = self.folder / "broken.mkv"
        broken.write_bytes(b"not a video")
        with self.assertRaisesRegex(export.ExportError, "couldn't read it"):
            export.export(broken)

    def test_command_line(self):
        source = self.folder / "rec.mkv"
        record(source, moving_bar(10))
        out = io.StringIO()
        with mock.patch("vintagecam.export._lower_priority"), contextlib.redirect_stdout(out):
            code = export.main([str(source)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("progress: 100.0%", out.getvalue())
        self.assertIn(f"done: {source.with_suffix('.mp4')}", out.getvalue())

    def test_the_child_command_runs_main_py(self):
        command = export.child_command([Path("a.mkv")], "auto")
        self.assertTrue(command[1].endswith("main.py"))
        self.assertEqual(command[2:], ["--export", "a.mkv", "--field-order", "auto"])


class ExportQueueTests(unittest.TestCase):
    """The queue the app uses: real child processes (main.py --export)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.queue = ExportQueue()
        self.done, self.statuses = [], []
        self.queue.finished_one.connect(lambda source, ok, message: self.done.append((source, ok, message)))
        self.queue.status_changed.connect(lambda text, fraction, running: self.statuses.append((text, running)))

    def tearDown(self):
        self.queue.cancel_all(wait=True)
        self.tmp.cleanup()

    def wait_until_done(self, count: int = 1, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.done) < count and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)

    def test_recordings_are_exported_one_after_another(self):
        first, second = self.folder / "one.mkv", self.folder / "two.mkv"
        record(first, moving_bar(10), tone_hz=440)
        record(second, moving_bar(10))
        self.queue.add([first, second, first], "auto")  # the repeat is ignored
        self.assertTrue(self.queue.running)
        self.wait_until_done(2)
        self.assertEqual(self.done, [(first, True, ""), (second, True, "")])
        self.assertTrue(first.with_suffix(".mp4").exists() and second.with_suffix(".mp4").exists())
        self.assertEqual(self.statuses[-1], ("Saved two.mp4", False))
        self.assertFalse(self.queue.running)

    def test_cancel_stops_the_export_and_deletes_the_unfinished_copy(self):
        source = self.folder / "rec.mkv"
        record(source, moving_bar(10))
        self.queue.add([source], "auto")
        self.queue.cancel_all(wait=True)
        self.wait_until_done(1, timeout=30)
        self.assertEqual(self.done, [(source, False, "")])
        self.assertEqual(self.statuses[-1], ("Export cancelled.", False))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["rec.mkv"])

    def test_a_failure_says_why(self):
        broken = self.folder / "broken.mkv"
        broken.write_bytes(b"not a video")
        self.queue.add([broken], "auto")
        self.wait_until_done(1)
        self.assertEqual(len(self.done), 1)
        source, ok, message = self.done[0]
        self.assertEqual((source, ok), (broken, False))
        self.assertIn("couldn't read it", message)


if __name__ == "__main__":
    unittest.main()
