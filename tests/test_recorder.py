"""The recorder, end to end: synthetic frames in, a real FFV1/MKV file out, decoded and compared."""

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import av
import numpy as np

from vintagecam.color import uyvy_to_planar
from vintagecam.frames import frame_from_uyvy
from vintagecam.recorder import QUEUE_SECONDS, RecordThread, make_recording_path
from vintagecam.video_format import NTSC


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recording_is_bit_exact_ffv1_with_gaps_preserved(self):
        rng = np.random.default_rng(7)
        sources = [rng.integers(16, 236, (480, 1440), dtype=np.uint8) for _ in range(12)]
        fd = NTSC.frame_duration
        times = [5000.0 + i * fd for i in range(12)]
        times[8:] = [t + fd for t in times[8:]]  # the device "skipped" a frame before frame 8
        jitter = rng.uniform(-0.005, 0.005, len(times))  # DirectShow timestamps wobble ±5 ms

        results = []
        path = self.folder / "test.mkv"
        recorder = RecordThread(path, NTSC, on_finished=results.append, comment="unit test")
        recorder.start()
        self.assertTrue(recorder.opened.wait(10))
        self.assertIsNone(recorder.error)
        for i, (uyvy, t, j) in enumerate(zip(sources, times, jitter)):
            self.assertTrue(recorder.offer(frame_from_uyvy(uyvy, NTSC, i, t + j)))
        recorder.stop()
        recorder.join(30)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsNone(result.error)
        self.assertEqual((result.frames_written, result.frames_dropped, result.device_gaps), (12, 0, 1))

        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            cc = stream.codec_context
            self.assertEqual(cc.name, "ffv1")
            self.assertEqual(cc.format.name, "yuv422p")
            self.assertEqual((cc.width, cc.height), (720, 480))
            self.assertEqual(stream.average_rate, Fraction(30000, 1001))
            self.assertEqual((cc.color_range, cc.colorspace), (1, 6))  # limited range, BT.601 (SMPTE 170M)
            self.assertIn("unit test", container.metadata.get("COMMENT", container.metadata.get("comment", "")))
            decoded = list(container.decode(stream))

        self.assertEqual(len(decoded), 12)
        for i, (src, frame) in enumerate(zip(sources, decoded)):
            self.assertTrue(frame.key_frame, f"frame {i} is not a keyframe")
            np.testing.assert_array_equal(frame.to_ndarray(), uyvy_to_planar(src), f"frame {i} not bit-exact")
        # Frame 8 lands one frame late: the gap the device left is kept.
        self.assertAlmostEqual(decoded[7].time, 7 * fd, delta=0.002)
        self.assertAlmostEqual(decoded[8].time, 9 * fd, delta=0.002)

    def _record_with_audio(self, **kwargs):
        with mock.patch("vintagecam.recorder.AUDIO_DRAIN_TIMEOUT", 0.2):  # keep the tests quick
            return self._record_with_audio_inner(**kwargs)

    def _record_with_audio_inner(self, *, seconds=2.0, audio_start=0.1, skew=0.0, freeze=None, audio_until=None,
                                 block=480, channels=2):
        """Synthetic frames and 10 ms audio blocks, offered in time order, through a real recorder.

        ``skew`` > 0 makes the audio clock run fast: each 480-sample block covers
        slightly less than 10 ms of real time.  ``freeze=(start, length)`` makes
        everything captured in that window reach the recorder only when it ends,
        as when the PC stalls: frames keep their true device timestamps.
        """
        rate, base = 48000, 5000.0
        fd = NTSC.frame_duration
        rng = np.random.default_rng(11)
        uyvy = rng.integers(16, 236, (480, 1440), dtype=np.uint8)

        def reaches_recorder(captured):
            if freeze and freeze[0] <= captured - base < freeze[0] + freeze[1]:
                return base + freeze[0] + freeze[1]
            return captured

        events = [(reaches_recorder(base + i * fd), base + i * fd, 0, i) for i in range(int(seconds / fd))]
        blocks = []
        t = base + audio_start
        until = base + (audio_until if audio_until is not None else seconds - 0.1)
        while t < until:
            samples = rng.integers(-3000, 3000, (block, channels), dtype=np.int16)
            blocks.append(samples)
            events.append((reaches_recorder(t), t, 1, samples))
            t += block / rate * (1 - skew)
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        results = []
        path = self.folder / "av.mkv"
        recorder = RecordThread(path, NTSC, on_finished=results.append, audio_rate=rate, audio_channels=channels,
                                video_delay=0.0)
        recorder.start()
        self.assertTrue(recorder.opened.wait(10))
        last_frame = max(n for n, e in enumerate(events) if e[2] == 0)
        for n, (when, captured, kind, payload) in enumerate(events):
            if kind == 0:
                frame = frame_from_uyvy(uyvy, NTSC, payload, captured, arrival_time=when)
                self.assertTrue(recorder.offer(frame))
            else:
                accepted = recorder.offer_audio(payload, captured)
                if n < last_frame:
                    self.assertTrue(accepted)
            if n == last_frame:
                recorder.stop()  # Stop is pressed after the last frame; sound keeps arriving
        recorder.join(30)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].error)
        return path, blocks, results[0]

    def test_audio_is_bit_exact_and_starts_in_step_with_the_video(self):
        path, blocks, result = self._record_with_audio(audio_start=0.1)
        self.assertEqual((result.audio_adjustments, result.audio_gaps), (0, 0))
        with av.open(str(path)) as container:
            self.assertEqual(len(container.streams.video), 1)
            self.assertEqual(len(container.streams.audio), 1)
            stream = container.streams.audio[0]
            self.assertEqual((stream.codec_context.name, stream.rate), ("pcm_s16le", 48000))
            frames = list(container.decode(stream))
        self.assertAlmostEqual(frames[0].time, 0.1, delta=0.002)  # sound begins 0.1 s into the picture
        decoded = np.concatenate([f.to_ndarray().reshape(-1, 2) for f in frames])
        np.testing.assert_array_equal(decoded, np.concatenate(blocks))

    def test_one_plug_is_recorded_as_a_bit_exact_mono_track(self):
        path, blocks, result = self._record_with_audio(audio_start=0.1, channels=1)
        self.assertEqual((result.audio_adjustments, result.audio_gaps), (0, 0))
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            self.assertEqual((stream.codec_context.name, stream.rate), ("pcm_s16le", 48000))
            self.assertEqual(len(stream.codec_context.layout.channels), 1)  # MKV keeps the count, not a layout name
            frames = list(container.decode(stream))
        decoded = np.concatenate([f.to_ndarray().reshape(-1, 1) for f in frames])
        np.testing.assert_array_equal(decoded, np.concatenate(blocks))

    def test_sound_ends_exactly_with_the_picture(self):
        """After Stop, sound still in the buffers is written, then cut at the last frame."""
        path, _, result = self._record_with_audio(seconds=2.0, audio_start=0.0, audio_until=2.5)
        self.assertIsNone(result.error)
        with av.open(str(path)) as container:
            video_end = [f.time for f in container.decode(container.streams.video[0])][-1] + NTSC.frame_duration
        with av.open(str(path)) as container:
            frames = list(container.decode(container.streams.audio[0]))
        audio_end = frames[0].time + sum(f.samples for f in frames) / 48000
        self.assertAlmostEqual(audio_end, video_end, delta=0.002)

    def test_a_frozen_pc_is_not_mistaken_for_lost_sound(self):
        """Frames that reach the recorder late (the PC froze for 0.3 s) must not look like a gap."""
        path, blocks, result = self._record_with_audio(seconds=2.0, audio_start=0.05, freeze=(1.0, 0.3))
        self.assertEqual((result.audio_gaps, result.audio_adjustments), (0, 0))
        with av.open(str(path)) as container:
            frames = list(container.decode(container.streams.audio[0]))
        decoded = np.concatenate([f.to_ndarray().reshape(-1, 2) for f in frames])
        np.testing.assert_array_equal(decoded, np.concatenate(blocks))

    def test_audio_follows_the_video_when_its_clock_runs_fast(self):
        skew = 0.001  # the audio clock runs 0.1% fast: far worse than real hardware, to see it act
        with mock.patch("vintagecam.recorder.AUDIO_SYNC_TOLERANCE", 0.001):
            _, blocks, result = self._record_with_audio(seconds=4.0, audio_start=0.0, skew=skew)
        self.assertGreater(result.audio_adjustments, 0)
        written = round(result.audio_seconds * 48000)
        needed = len(blocks) * 480 * (1 - skew)  # samples the real elapsed time calls for
        self.assertLess(abs(written - needed), 0.003 * 48000, "sound drifted more than 3 ms from the picture")

    def test_sync_keeps_up_with_large_audio_blocks(self):
        """Blocks of 4096 samples, audio clock 0.08% fast: one sample per block wouldn't keep up."""
        skew, block = 0.0008, 4096
        with mock.patch("vintagecam.recorder.AUDIO_SYNC_TOLERANCE", 0.001):
            _, blocks, result = self._record_with_audio(seconds=8.0, audio_start=0.0, skew=skew, block=block)
        written = round(result.audio_seconds * 48000)
        needed = len(blocks) * block * (1 - skew)
        self.assertLess(abs(written - needed), 0.002 * 48000, "sound drifted more than 2 ms from the picture")

    def test_offer_never_blocks_and_counts_overflow(self):
        recorder = RecordThread(self.folder / "never-started.mkv", NTSC)  # not started: nothing drains the queue
        frame = frame_from_uyvy(np.zeros((480, 1440), np.uint8), NTSC)
        capacity = int(QUEUE_SECONDS * NTSC.fps)
        self.assertTrue(all(recorder.offer(frame) for _ in range(capacity)))
        self.assertFalse(recorder.offer(frame))
        self.assertEqual(recorder.frames_dropped, 1)

    def test_unwritable_location_is_reported(self):
        blocker = self.folder / "blocker"
        blocker.write_text("a file where a folder should be")
        results = []
        recorder = RecordThread(blocker / "x.mkv", NTSC, on_finished=results.append)
        recorder.start()
        recorder.join(10)
        self.assertTrue(recorder.opened.is_set())
        self.assertIsNotNone(recorder.error)
        self.assertIsNotNone(results[0].error)

    def test_filenames_are_timestamped_and_never_overwrite(self):
        first = make_recording_path(self.folder, "cam:1")
        self.assertRegex(first.name, r"^cam_1_\d{8}_\d{6}\.mkv$")
        first.write_bytes(b"")
        second = make_recording_path(self.folder, "cam:1", when=None)
        if second.stem.startswith(first.stem):
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
