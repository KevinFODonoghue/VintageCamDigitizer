"""The recorder, end to end: synthetic frames in, a real FFV1/MKV file out, decoded and compared."""

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

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
