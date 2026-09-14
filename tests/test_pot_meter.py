"""The pot meter, checked with synthetic white cards and a simulated pot: no camera needed."""

import time
import unittest

import numpy as np

from vintagecam import pot_meter as pm
from vintagecam.analysis import AnalysisThread
from vintagecam.color import rgb_to_uyvy
from vintagecam.frames import LatestSlot, frame_from_uyvy
from vintagecam.video_format import NTSC

H, W = 480, 720
X, Y = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
RAW = ((X - 360) / 360) ** 2 + ((Y - 240) / 240) ** 2  # 0 in the centre, 2 in the corners
PATTERN = RAW - RAW.mean()
RNG = np.random.default_rng(7)


def solid(r: float, g: float, b: float) -> np.ndarray:
    return rgb_to_uyvy(np.broadcast_to(np.array([r, g, b], np.float64), (H, W, 3)))


def pot_frame(p: float, best: float = 0.3, base: float = 240.0, amount: float = 12.0,
              room_tint: float = 0.0) -> np.ndarray:
    """A white card through a camera with one pot at position ``p`` (0 = fully anticlockwise, 1 = clockwise).

    At ``best`` the camera shows the card as it really is; either side of it the
    corners turn pink or green, in proportion to how far the pot is from there.
    ``room_tint`` > 0: the room's own light makes the corners a little pink.  A
    little noise, as from a real camera, stops rounding to whole code values
    from hiding small changes.
    """
    shift = (amount * (p - best) + room_tint) * PATTERN
    rgb = np.stack([base + shift, base - shift, np.full_like(shift, base)], axis=-1)
    return rgb_to_uyvy(rgb + RNG.normal(0, 2.0, rgb.shape))


def phone_photo(room_tint: float, width: int = 668, exposure: float = 0.8, base: float = 240.0) -> np.ndarray:
    """The same card as a phone sees it: square pixels, its own exposure, framed on the grid's area."""
    height = round(width / pm.GRID_ASPECT)
    u = np.linspace((pm.GRID_LEFT * W - 360) / 360, (pm.GRID_RIGHT * W - 360) / 360, width)
    v = np.linspace((pm.GRID_TOP * H - 240) / 240, (pm.GRID_BOTTOM * H - 240) / 240, height)
    uu, vv = np.meshgrid(u, v)
    pattern = uu ** 2 + vv ** 2 - RAW.mean()  # the same places on the card as the camera's pattern
    return exposure * np.stack([base + room_tint * pattern, base - room_tint * pattern,
                                np.full_like(pattern, base)], axis=-1)


def measured(*frames: np.ndarray) -> np.ndarray:
    return np.mean([pm.cell_colours(f) for f in frames], axis=0)


class Grid(unittest.TestCase):
    def test_the_grid_covers_the_picture_but_not_the_blanking(self):
        xs, ys = pm.grid_edges(480, 720)
        self.assertEqual((len(xs), len(ys)), (pm.GRID_COLUMNS + 1, pm.GRID_ROWS + 1))
        self.assertEqual((xs[0], xs[-1], ys[0], ys[-1]), (22, 716, 0, 480))
        cell_w_on_screen = (xs[-1] - xs[0]) / pm.GRID_COLUMNS * NTSC.pixel_aspect  # NTSC pixels are 10/11 wide
        cell_h = (ys[-1] - ys[0]) / pm.GRID_ROWS
        self.assertAlmostEqual(cell_w_on_screen / cell_h, 1.0, delta=0.02)  # square cells on the screen

    def test_the_elgatos_blanking_never_gets_into_a_cell(self):
        card = solid(230, 230, 230)
        framed = card.copy()
        luma = framed[:, 1::2]  # the Elgato's line, as measured in every recording:
        luma[:, :18] = 1  # blanking, about 0 (below video black)…
        luma[:, 18] = 30  # …the picture's edge rising…
        luma[:, 19] = 250  # …and overshooting,
        luma[:, 718], luma[:, 719] = 200, 40  # and a spike at the end of the line
        np.testing.assert_array_equal(pm.cell_colours(framed), pm.cell_colours(card))

    def test_each_cell_holds_its_average_colour(self):
        colours = pm.cell_colours(solid(200, 150, 100))
        self.assertEqual(colours.shape, (pm.GRID_ROWS, pm.GRID_COLUMNS, 3))
        np.testing.assert_allclose(colours, np.broadcast_to([200, 150, 100], colours.shape), atol=1.5)

    def test_tint_and_unevenness_count_but_brightness_does_not(self):
        self.assertLess(pm.distance_from_target(pm.cell_colours(solid(255, 255, 255))), 1.0)
        self.assertLess(pm.distance_from_target(pm.cell_colours(solid(150, 150, 150))), 1.0)  # even grey: white, dimmer
        self.assertGreater(pm.distance_from_target(pm.cell_colours(solid(200, 150, 100))), 30)  # a tint isn't
        self.assertGreater(pm.distance_from_target(measured(pot_frame(0.9))),
                           pm.distance_from_target(measured(pot_frame(0.3))) + 2)  # pinker corners: further off


class Judging(unittest.TestCase):
    def setUp(self):
        self.ccw, self.cw = measured(pot_frame(0.0)), measured(pot_frame(1.0))

    def test_the_best_position_is_found_from_the_two_ends(self):
        at_best = pm.judge(measured(pot_frame(0.3)), self.ccw, self.cw)
        self.assertAlmostEqual(at_best.best, 0.3, delta=0.01)
        self.assertAlmostEqual(at_best.position, 0.3, delta=0.01)
        self.assertTrue(at_best.at_best)
        off = pm.judge(measured(pot_frame(0.6)), self.ccw, self.cw)
        self.assertAlmostEqual(off.position, 0.6, delta=0.01)
        self.assertFalse(off.at_best)

    def test_green_only_within_the_tolerance(self):
        self.assertTrue(pm.judge(measured(pot_frame(0.32)), self.ccw, self.cw).at_best)
        self.assertFalse(pm.judge(measured(pot_frame(0.36)), self.ccw, self.cw).at_best)

    def test_a_pot_that_also_brightens_the_picture_is_still_judged_on_whiteness(self):
        def brightening(p):  # the same pot, but it also lifts the whole picture as it turns
            return pot_frame(p, base=225 + 20 * p)
        verdict = pm.judge(measured(brightening(0.3)), measured(brightening(0.0)), measured(brightening(1.0)))
        self.assertAlmostEqual(verdict.best, 0.3, delta=0.01)
        self.assertTrue(verdict.at_best)

    def test_the_best_can_be_an_end(self):
        ccw, cw = measured(pot_frame(0.0, best=1.4)), measured(pot_frame(1.0, best=1.4))
        at_end = pm.judge(measured(pot_frame(1.0, best=1.4)), ccw, cw)
        self.assertEqual((at_end.best, at_end.at_best), (1.0, True))
        self.assertIn("fully clockwise", at_end.note)
        self.assertFalse(pm.judge(measured(pot_frame(0.5, best=1.4)), ccw, cw).at_best)

    def test_a_pot_that_changes_nothing_cannot_be_judged(self):
        same = measured(pot_frame(0.3))
        verdict = pm.judge(same, same, same)
        self.assertIsNone(verdict.at_best)
        self.assertIn("can't judge", verdict.note)

    def test_a_pot_that_only_changes_brightness_cannot_be_judged(self):
        dim, bright = pm.cell_colours(solid(200, 200, 200)), pm.cell_colours(solid(240, 240, 240))
        self.assertIsNone(pm.judge(dim, dim, bright).at_best)


class Zeroing(unittest.TestCase):
    """A phone photo of the card replaces plain white as the target."""

    def test_a_phone_photo_sets_how_the_card_should_look(self):
        tint = 3.6  # the room's light makes the corners a little pink: that's how the card really looks
        ccw, cw = measured(pot_frame(0.0, room_tint=tint)), measured(pot_frame(1.0, room_tint=tint))
        faithful = measured(pot_frame(0.3, room_tint=tint))  # the camera shows the card as it really is
        against_white = pm.judge(faithful, ccw, cw)
        self.assertLess(against_white.best, 0.1)  # plain white wants the room's pink taken out as well
        self.assertFalse(against_white.at_best)
        against_photo = pm.judge(faithful, ccw, cw, reference=pm.reference_from_rgb(phone_photo(tint)))
        self.assertAlmostEqual(against_photo.best, 0.3, delta=0.02)
        self.assertTrue(against_photo.at_best)

    def test_the_photo_is_cropped_to_the_camera_picture(self):
        wide = np.full((450, 800, 3), 230.0)  # 16:9, with red strips at the sides the camera can't see
        wide[:, :80] = wide[:, -80:] = (230.0, 60.0, 60.0)
        reference = pm.reference_from_rgb(wide)
        self.assertEqual(reference.shape, (pm.GRID_ROWS, pm.GRID_COLUMNS, 3))
        np.testing.assert_allclose(reference, 230.0, atol=1e-9)  # only the white middle is kept

    def test_unusable_photos_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(ValueError, "portrait"):
            pm.reference_from_rgb(np.full((800, 600, 3), 230.0))
        with self.assertRaisesRegex(ValueError, "almost black"):
            pm.reference_from_rgb(np.zeros((600, 800, 3)))

    def test_new_pot_keeps_the_photo(self):
        meter = pm.PotMeter()
        meter.reference = pm.reference_from_rgb(phone_photo(3.6))
        meter.reset()
        self.assertIsNotNone(meter.reference)


class Meter(unittest.TestCase):
    def test_measuring_an_end_takes_its_frames_then_stops(self):
        meter = pm.PotMeter(live_frames=3, measure_frames=4)
        meter.measure("cw")
        frames = [pot_frame(1.0) for _ in range(4)]
        for i, frame in enumerate(frames[:3]):
            meter.add(frame)
            self.assertEqual((meter.measuring, meter.progress), ("cw", (i + 1) / 4))
        meter.add(frames[3])
        self.assertEqual(meter.measuring, "")
        np.testing.assert_allclose(meter.ends["cw"], measured(*frames), atol=1e-9)
        self.assertIsNone(meter.judge())  # the other end isn't measured yet

    def test_new_pot_forgets_both_ends(self):
        meter = pm.PotMeter(live_frames=3, measure_frames=2)
        for end, p in (("ccw", 0.0), ("cw", 1.0)):
            meter.measure(end)
            meter.add(pot_frame(p))
            meter.add(pot_frame(p))
        self.assertIsNotNone(meter.judge())
        meter.reset()
        self.assertEqual(meter.ends, {"cw": None, "ccw": None})
        self.assertIsNone(meter.judge())


class AnalysisThreadTests(unittest.TestCase):
    """The thread the app runs: frames in through a newest-wins slot, statuses out."""

    def setUp(self):
        self.statuses = []
        self.slot = LatestSlot()
        self.thread = AnalysisThread(self.statuses.append, live_frames=3, measure_frames=5)
        self.thread.set_source(self.slot)
        self.thread.set_enabled(True)
        self.thread.start()

    def tearDown(self):
        self.thread.stop()
        self.thread.join(5)

    def feed(self, make_frame, frames: int, pace: float = 0.0) -> None:
        """Hand over frames one at a time, each once the thread has taken the last (``pace``: seconds apart)."""
        for i in range(frames):
            target = self.thread.processed + 1
            self.slot.put(frame_from_uyvy(make_frame(), NTSC, i))
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

    def measure_both_ends(self, **scene):
        for end, p in (("ccw", 0.0), ("cw", 1.0)):
            self.thread.measure(end)
            self.feed(lambda p=p: pot_frame(p, **scene), 7)
            self.wait_for(lambda s, e=end: getattr(s, f"has_{e}") and not s.measuring)

    def test_green_at_the_best_position_and_red_elsewhere(self):
        self.measure_both_ends()
        self.feed(lambda: pot_frame(0.3), 12, pace=0.03)
        green = self.wait_for(lambda s: s.at_best is True)
        self.assertAlmostEqual(green.best, 0.3, delta=0.02)
        self.statuses.clear()
        self.feed(lambda: pot_frame(0.7), 12, pace=0.03)
        red = self.wait_for(lambda s: s.at_best is False)
        self.assertAlmostEqual(red.position, 0.7, delta=0.03)

    def test_a_phone_photo_moves_the_green_spot_to_how_the_card_really_looks(self):
        self.thread.set_reference(pm.reference_from_rgb(phone_photo(3.6)))
        self.measure_both_ends(room_tint=3.6)
        self.feed(lambda: pot_frame(0.3, room_tint=3.6), 12, pace=0.03)
        green = self.wait_for(lambda s: s.at_best is True)
        self.assertAlmostEqual(green.best, 0.3, delta=0.02)

    def test_new_pot_turns_the_light_grey(self):
        self.measure_both_ends()
        self.thread.reset()
        status = self.wait_for(lambda s: not s.has_cw and not s.has_ccw)
        self.assertIsNone(status.at_best)

    def test_a_dark_picture_is_pointed_out(self):
        self.feed(lambda: solid(60, 60, 60), 12, pace=0.03)
        self.wait_for(lambda s: "picture is dark" in s.note)

    def test_nothing_is_analysed_while_switched_off(self):
        self.thread.set_enabled(False)
        time.sleep(0.1)
        self.slot.put(frame_from_uyvy(solid(255, 255, 255), NTSC))
        time.sleep(0.3)
        self.assertEqual(self.thread.processed, 0)


if __name__ == "__main__":
    unittest.main()
