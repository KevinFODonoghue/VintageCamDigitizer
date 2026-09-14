"""Pot Assist (the port of pot_metrics.py), checked with synthetic frames: no camera needed."""

import time
import unittest

import numpy as np

from vintagecam import pot_assist as pa
from vintagecam.analysis import AnalysisThread
from vintagecam.frames import LatestSlot, frame_from_uyvy
from vintagecam.video_format import NTSC

H, W = 480, 720
X = np.tile(np.arange(W, dtype=np.float64), (H, 1))


def frame(y=None, cb=None, cr=None) -> np.ndarray:
    """UYVY bytes from full-size Y, Cb and Cr pictures (Cb and Cr taken at every other pixel, as 4:2:2 does)."""
    y, cb, cr = (np.full((H, W), 128.0) if p is None else p for p in (y, cb, cr))
    out = np.empty((H, W * 2), np.uint8)
    out[:, 1::2] = np.clip(np.rint(y), 0, 255)
    out[:, 0::4] = np.clip(np.rint(cb[:, 0::2]), 0, 255)
    out[:, 2::4] = np.clip(np.rint(cr[:, 0::2]), 0, 255)
    return out


def texture(seed: int = 1) -> np.ndarray:
    """Fine detail everywhere, like a detailed chart."""
    return np.random.default_rng(seed).integers(60, 196, (H, W)).astype(np.float64)


def soften(img: np.ndarray, columns: slice, radius: int = 2) -> np.ndarray:
    """Blur the given columns (a (2r+1)² box average): less fine detail there, like a soft focus."""
    total = np.zeros_like(img)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            total += np.roll(np.roll(img, dy, 0), dx, 1)
    out = img.copy()
    out[:, columns] = total[:, columns] / (2 * radius + 1) ** 2
    return out


def fed(mode: str, *frames: np.ndarray, window: int = 8) -> pa.PotAssist:
    assist = pa.PotAssist(mode, window=window)
    for f in frames:
        assist.add(f)
    return assist


class Measurements(unittest.TestCase):
    def test_the_boxes_are_where_pot_metrics_put_them(self):
        self.assertEqual(pa.regions(480, 720), {
            "C": (295, 196, 424, 283), "L": (57, 196, 187, 283), "R": (532, 196, 662, 283),
            "T": (295, 38, 424, 124), "B": (295, 355, 424, 441)})

    def test_a_left_to_right_red_tilt_is_h_saw(self):
        terms = fed("shading_red", *[frame(cr=128 + 0.05 * (X - 360))] * 8).raw_terms()
        # Right box centre x = 596.5, left 121.5: 0.05 × 475 = 23.75 code values.
        self.assertAlmostEqual(terms["H saw"], 23.75, delta=0.1)
        self.assertAlmostEqual(terms["H para"], 0.0, delta=0.1)
        self.assertEqual((terms["V saw"], terms["V para"]), (0.0, 0.0))  # nothing changes from top to bottom

    def test_red_reads_cr_and_blue_reads_cb(self):
        bluish = frame(cb=128 + 0.05 * (X - 360))
        self.assertAlmostEqual(fed("shading_blue", *[bluish] * 8).raw_terms()["H saw"], 23.75, delta=0.1)
        self.assertEqual(fed("shading_red", *[bluish] * 8).raw_terms()["H saw"], 0.0)

    def test_redder_sides_are_h_para(self):
        terms = fed("shading_red", *[frame(cr=128 + 30 * ((X - 360) / 360) ** 2)] * 8).raw_terms()
        self.assertGreater(terms["H para"], 10)
        self.assertLess(abs(terms["H saw"]), 1)

    def test_averaging_keeps_fractions_of_a_code_value(self):
        # The right box alternates between 128 and 129: half a code value redder on average.  pot_metrics
        # rounded the averaged frame back to whole numbers, which would read 0 here.
        frames = []
        for i in range(8):
            cr = np.full((H, W), 128.0)
            cr[:, 532:662] += i % 2
            frames.append(frame(cr=cr))
        self.assertAlmostEqual(fed("shading_red", *frames).raw_terms()["H saw"], 0.5, places=9)

    def test_soft_focus_on_the_right_reads_as_h_saw_and_h_para(self):
        sharp = texture()
        terms = fed("focus", *[frame(y=soften(sharp, slice(500, None)))] * 8).raw_terms()
        self.assertLess(terms["H saw"], 0)  # right less detailed than left
        self.assertLess(terms["H para"], 0)  # the sides, on average, less detailed than the centre
        self.assertAlmostEqual(terms["V saw"], 0, delta=0.05 * abs(terms["H saw"]))

    def test_focus_ignores_a_mismatch_between_the_two_fields(self):
        sharp = texture()
        mismatched = sharp.copy()
        mismatched[1::2] += 12  # one interlaced field a little brighter than the other
        plain = fed("focus", *[frame(y=sharp)] * 8).raw_terms()
        shifted = fed("focus", *[frame(y=mismatched)] * 8).raw_terms()
        for name in pa.ORDER:
            self.assertAlmostEqual(plain[name], shifted[name], places=6)

    def test_focus_keeps_only_the_window(self):
        assist = fed("focus", *[frame(y=texture(i)) for i in range(12)], window=5)
        self.assertEqual(assist.frames, 5)
        expected = pa.sharpness_boxes(np.mean([frame(y=texture(i))[:, 1::2] for i in range(7, 12)], axis=0))
        np.testing.assert_allclose(list(assist.boxes().values()), expected, rtol=1e-9)

    def test_not_ready_until_a_quarter_of_the_window(self):
        assist = fed("shading_red", *[frame()] * 7, window=90)
        self.assertIsNone(assist.read())
        assist.add(frame())
        self.assertIsNone(assist.read())  # needs 22 of 90
        for _ in range(14):
            assist.add(frame())
        self.assertIsNotNone(assist.read())


class Decisions(unittest.TestCase):
    def test_which_way_to_turn(self):
        self.assertEqual(pa.turn_direction(+2.0, +1, False), "turn CCW")  # clockwise raises it: go the other way
        self.assertEqual(pa.turn_direction(+2.0, -1, False), "turn CW")
        self.assertEqual(pa.turn_direction(-2.0, +1, False), "turn CW")
        self.assertEqual(pa.turn_direction(-2.0, -1, False), "turn CCW")
        self.assertEqual(pa.turn_direction(-2.0, None, False), "learn")
        self.assertEqual(pa.turn_direction(0.1, +1, True), "OK")

    def test_saw_before_para_then_the_worst(self):
        def terms(*errors_and_ok):
            return [pa.Term(name, "RT3xx", e, ok, "") for name, (e, ok) in zip(pa.ORDER, errors_and_ok)]
        # H para is the worst, but H saw is still out: H para waits.  Of H saw and V para, V para is worse.
        self.assertEqual(pa.next_term(terms((1.0, False), (5.0, False), (0.1, True), (3.0, False))), "V para")
        self.assertEqual(pa.next_term(terms((0.1, True), (5.0, False), (0.1, True), (3.0, False))), "H para")
        self.assertIsNone(pa.next_term(terms((0.1, True), (0.1, True), (0.1, True), (0.1, True))))

    def test_a_reading_uses_the_tolerance_and_the_learned_directions(self):
        assist = fed("shading_red", *[frame(cr=128 + 0.05 * (X - 360))] * 8)
        assist.deadband = 0.5
        assist.set_polarity("RT313", clockwise_raises_error=True)
        reading = assist.read()
        by = {t.name: t for t in reading.terms}
        self.assertEqual((by["H saw"].pot, by["H saw"].ok, by["H saw"].direction), ("RT313", False, "turn CCW"))
        self.assertEqual((by["V saw"].ok, by["V saw"].direction), (True, "learn"))  # RT315 not learned yet
        self.assertEqual((reading.next_pot, reading.converged), ("RT313", False))

    def test_tolerance_from_two_readings(self):
        first = {"H saw": 1.0, "H para": 0.0, "V saw": 0.0, "V para": 0.0}
        second = {"H saw": 1.25, "H para": -0.1, "V saw": 0.0, "V para": 0.0}
        self.assertAlmostEqual(pa.deadband_from(first, second), 0.5)


class AnalysisThreadTests(unittest.TestCase):
    """The thread the app runs: frames in through a newest-wins slot, statuses out."""

    def setUp(self):
        self.statuses = []
        self.slot = LatestSlot()
        self.thread = AnalysisThread(self.statuses.append, window=8)
        self.thread.set_source(self.slot)
        self.thread.set_enabled(True)
        self.thread.start()

    def tearDown(self):
        self.thread.stop()
        self.thread.join(5)

    def feed(self, uyvy: np.ndarray, frames: int, pace: float = 0.0) -> None:
        """Hand over frames one at a time, each once the thread has taken the last (``pace``: seconds apart)."""
        for i in range(frames):
            target = self.thread.processed + 1
            self.slot.put(frame_from_uyvy(uyvy, NTSC, i))
            deadline = time.monotonic() + 5
            while self.thread.processed < target and time.monotonic() < deadline:
                time.sleep(0.001)
            time.sleep(pace)

    def wait_for(self, condition, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [s for s in list(self.statuses) if condition(s)]
            if found:
                return found[-1]
            time.sleep(0.01)
        self.fail("no such status arrived")

    def test_readings_arrive(self):
        self.feed(frame(cr=128 + 0.05 * (X - 360)), 15, pace=0.03)  # half a second: several updates go out
        status = self.wait_for(lambda s: s.reading is not None)
        self.assertEqual(status.mode, "shading_red")
        self.assertAlmostEqual({t.name: t.error for t in status.reading.terms}["H saw"], 23.75, delta=0.1)

    def test_measuring_the_noise_sets_the_tolerance(self):
        rng = np.random.default_rng(3)
        self.thread.measure_noise()
        for _ in range(20):
            self.feed(frame(cr=128 + rng.normal(0, 3, (H, W))), 1)
        status = self.wait_for(lambda s: s.measured_deadband is not None)
        self.assertGreater(status.measured_deadband, 0)
        self.assertEqual(self.thread.assist.deadband, status.measured_deadband)

    def test_learning_which_way_a_pot_turns(self):
        self.feed(frame(cr=128 + 0.05 * (X - 360)), 8)
        self.thread.learn("RT313")
        self.feed(frame(cr=128 + 0.05 * (X - 360)), 2)
        self.wait_for(lambda s: s.waiting_for_user)
        self.thread.done()
        self.feed(frame(cr=128 + 0.06 * (X - 360)), 10)  # after the clockwise turn, H saw went up
        status = self.wait_for(lambda s: s.learned is not None)
        self.assertEqual(status.learned, ("RT313", 1))
        self.assertIn("raises H saw", status.prompt)

    def test_no_change_teaches_nothing(self):
        self.thread.assist.deadband = 0.5
        still = frame(cr=128 + 0.05 * (X - 360))
        self.feed(still, 8)
        self.thread.learn("RT313")
        self.feed(still, 2)
        self.wait_for(lambda s: s.waiting_for_user)
        self.thread.done()
        self.feed(still, 10)
        status = self.wait_for(lambda s: "didn't change" in s.prompt)
        self.assertNotIn("RT313", self.thread.assist.polarity)
        self.assertIsNone(status.learned)

    def test_nothing_is_analysed_while_switched_off(self):
        self.thread.set_enabled(False)
        time.sleep(0.1)
        self.slot.put(frame_from_uyvy(frame(), NTSC))
        time.sleep(0.3)
        self.assertEqual(self.thread.processed, 0)


if __name__ == "__main__":
    unittest.main()
