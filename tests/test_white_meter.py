"""The white meter, checked with synthetic pictures: no camera needed."""

import time
import unittest

import numpy as np

from vintagecam import white_meter as wm
from vintagecam.analysis import AnalysisThread
from vintagecam.color import rgb_to_uyvy
from vintagecam.frames import LatestSlot, frame_from_uyvy
from vintagecam.video_format import NTSC

H, W = 480, 720


def solid(r: float, g: float, b: float) -> np.ndarray:
    return rgb_to_uyvy(np.broadcast_to(np.array([r, g, b], np.float64), (H, W, 3)))


class PercentWhite(unittest.TestCase):
    def test_white_black_and_grey(self):
        self.assertAlmostEqual(wm.percent_white((255, 255, 255)), 100.0)
        self.assertAlmostEqual(wm.percent_white((0, 0, 0)), 0.0)
        self.assertAlmostEqual(wm.percent_white((128, 128, 128)), 100 * 128 / 255)  # a grey reads its brightness

    def test_a_colour_is_as_white_as_its_weakest_primary(self):
        self.assertAlmostEqual(wm.percent_white((230, 230, 200)), 100 * 200 / 255)  # warm light: blue short
        rng = np.random.default_rng(4)
        for colour in rng.uniform(0, 255, (1000, 3)):
            self.assertLessEqual(wm.percent_white(colour), 100 * colour.min() / 255 + 1e-9)

    def test_one_colour_sticking_out_lowers_it(self):
        self.assertLess(wm.percent_white((240, 200, 200)), wm.percent_white((200, 200, 200)))

    def test_full_colours_have_no_white(self):
        for colour in ((255, 0, 0), (255, 255, 0), (0, 128, 255)):
            self.assertAlmostEqual(wm.percent_white(colour), 0.0, places=9)

    def test_above_video_white_counts_as_white(self):
        self.assertAlmostEqual(wm.percent_white((270, 266, 262)), 100.0)

    def test_turning_one_colour_up_peaks_where_it_matches_the_strongest_other(self):
        levels = np.arange(256.0)
        rng = np.random.default_rng(3)
        for _ in range(200):
            colour = rng.uniform(150, 250, 3)
            k = int(rng.integers(3))
            readings = []
            for level in levels:
                colour[k] = level
                readings.append(wm.percent_white(colour))
            self.assertAlmostEqual(levels[int(np.argmax(readings))], np.delete(colour, k).max(), delta=1.0)


class AverageColour(unittest.TestCase):
    def test_a_flat_picture(self):
        np.testing.assert_allclose(wm.average_colour(solid(200, 150, 100)), (200, 150, 100), atol=1.5)

    def test_the_elgatos_blanking_is_left_out(self):
        white = solid(255, 255, 255)
        framed = white.copy()
        luma = framed[:, 1::2]  # the Elgato's line, as measured in every recording (see test_pot_meter):
        luma[:, :18] = 1  # blanking…
        luma[:, 18], luma[:, 19] = 30, 250  # …the picture's edge and its overshoot,
        luma[:, 718], luma[:, 719] = 200, 40  # and the spike at the end of the line
        np.testing.assert_array_equal(wm.average_colour(framed), wm.average_colour(white))
        self.assertAlmostEqual(wm.percent_white(wm.average_colour(framed)), 100.0)


class Meter(unittest.TestCase):
    def test_a_reading_averages_the_frames_since_the_last_one(self):
        meter = wm.WhiteMeter()
        self.assertIsNone(meter.reading())
        meter.add(solid(255, 255, 255))
        meter.add(solid(155, 155, 155))
        reading = meter.reading()
        np.testing.assert_allclose(reading.rgb, (205, 205, 205), atol=1.0)
        self.assertAlmostEqual(reading.percent, 100 * 205 / 255, delta=0.5)
        self.assertIsNone(meter.reading())  # each reading starts a new average


class WhiteThread(unittest.TestCase):
    """The white meter in the thread the app runs, with its own switch."""

    def setUp(self):
        self.readings = []
        self.slot = LatestSlot()
        self.thread = AnalysisThread(lambda status: None, self.readings.append)
        self.thread.set_source(self.slot)
        self.thread.start()

    def tearDown(self):
        self.thread.stop()
        self.thread.join(5)

    def test_readings_arrive_while_it_is_switched_on(self):
        self.thread.set_white_enabled(True)
        frame = frame_from_uyvy(solid(230, 230, 200), NTSC)
        deadline = time.monotonic() + 5
        while len(self.readings) < 3 and time.monotonic() < deadline:
            self.slot.put(frame)
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.readings), 3)
        self.assertAlmostEqual(self.readings[-1].percent, 100 * 200 / 255, delta=1.0)
        self.assertIsNone(self.thread.meter.live())  # the pot meter has its own switch, and it's off

    def test_nothing_while_it_is_switched_off(self):
        self.slot.put(frame_from_uyvy(solid(255, 255, 255), NTSC))
        time.sleep(0.3)
        self.assertEqual((self.readings, self.thread.processed), ([], 0))


if __name__ == "__main__":
    unittest.main()
