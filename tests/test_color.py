"""Colour maths, repacking and the preview converter.

These tests are the proof behind two claims the app makes:
  * recordings are bit-exact (UYVY -> planar is a pure shuffle), and
  * the live preview shows true BT.601 colours (swscale agrees with the formula).
"""

import unittest

import numpy as np

from vintagecam import color
from vintagecam.frames import frame_from_uyvy
from vintagecam.render import PreviewRenderer
from vintagecam.video_format import NTSC

# SMPTE 75% colour bars as 8-bit BT.601 Y'CbCr, and the R'G'B' they should show
# (75% of 255 = 191).  Values from the standard formulas, rounded.
BARS_75 = {
    "yellow": ((162, 44, 142), (191, 191, 0)),
    "cyan": ((131, 156, 44), (0, 191, 191)),
    "green": ((112, 72, 58), (0, 191, 0)),
    "magenta": ((84, 184, 198), (191, 0, 191)),
    "red": ((65, 100, 212), (191, 0, 0)),
    "blue": ((35, 212, 114), (0, 0, 191)),
}


class Layout(unittest.TestCase):
    def test_split_uyvy_byte_order(self):
        row = np.array([[10, 20, 30, 40, 50, 60, 70, 80]], np.uint8)  # Cb0 Y0 Cr0 Y1 Cb1 Y2 Cr1 Y3
        y, cb, cr = color.split_uyvy(row)
        self.assertEqual(y.tolist(), [[20, 40, 60, 80]])
        self.assertEqual(cb.tolist(), [[10, 50]])
        self.assertEqual(cr.tolist(), [[30, 70]])

    def test_uyvy_to_planar_is_a_lossless_shuffle(self):
        uyvy = np.random.default_rng(1).integers(0, 256, (480, 1440), dtype=np.uint8)
        planar = color.uyvy_to_planar(uyvy)
        self.assertEqual(planar.shape, (960, 720))
        y, cb, cr = color.split_uyvy(uyvy)
        chroma = planar[480:].reshape(2, 480, 360)
        np.testing.assert_array_equal(planar[:480], y)
        np.testing.assert_array_equal(chroma[0], cb)
        np.testing.assert_array_equal(chroma[1], cr)


class Bt601(unittest.TestCase):
    def test_black_white_grey(self):
        for luma, expected in ((16, 0), (235, 255), (126, 128)):
            rgb = color.ycbcr_to_rgb(np.array([luma]), np.array([128]), np.array([128]))
            self.assertEqual(rgb.tolist(), [[expected] * 3])

    def test_75_percent_bars(self):
        for name, ((y, cb, cr), expected) in BARS_75.items():
            got = color.ycbcr_to_rgb(np.array([y]), np.array([cb]), np.array([cr]))[0].astype(int)
            self.assertLessEqual(np.abs(got - expected).max(), 2, f"{name}: got {got}, expected {expected}")

    def test_rgb_uyvy_round_trip(self):
        rgb = np.zeros((4, 8, 3), np.uint8)
        rgb[:] = (150, 90, 40)
        back = color.uyvy_to_rgb(color.rgb_to_uyvy(rgb)).astype(int)
        self.assertLessEqual(np.abs(back - rgb).max(), 2)


class PreviewConverter(unittest.TestCase):
    def test_swscale_preview_matches_the_bt601_formula(self):
        """The fast converter the preview uses must agree with the long-hand formula."""
        patches = [rgb for _, rgb in BARS_75.values()] + [(0, 0, 0), (128, 128, 128), (255, 255, 255)]
        width = 720 // len(patches)
        rgb = np.zeros((480, 720, 3), np.uint8)
        for i, patch in enumerate(patches):
            rgb[:, i * width:(i + 1) * width] = patch
        uyvy = color.rgb_to_uyvy(rgb)
        reference = color.uyvy_to_rgb(uyvy).astype(int)
        bgra = PreviewRenderer().to_bgra(frame_from_uyvy(uyvy, NTSC))
        shown = bgra[..., [2, 1, 0]].astype(int)  # BGRA -> RGB
        for i in range(len(patches)):
            inner = slice(i * width + 6, (i + 1) * width - 6)  # skip edges, where chroma is interpolated
            diff = np.abs(shown[:, inner] - reference[:, inner]).max()
            self.assertLessEqual(diff, 2, f"patch {i} {patches[i]}: preview differs by {diff}")


class Deinterlace(unittest.TestCase):
    def setUp(self):
        # Pure combing: top field all 200, bottom field all 40.
        self.img = np.zeros((8, 4, 4), np.uint8)
        self.img[0::2] = 200
        self.img[1::2] = 40

    def test_bob_shows_one_field_only(self):
        self.assertTrue(np.all(color.bob_field(self.img, 0) == 200))
        self.assertTrue(np.all(color.bob_field(self.img, 1) == 40))

    def test_bob_interpolates_missing_lines(self):
        img = (np.arange(8, dtype=np.uint8) * 10).reshape(8, 1)
        self.assertEqual(color.bob_field(img, 0).ravel().tolist(), [0, 10, 20, 30, 40, 50, 60, 60])
        self.assertEqual(color.bob_field(img, 1).ravel().tolist(), [10, 10, 20, 30, 40, 50, 60, 70])

    def test_blend_removes_combing(self):
        out = color.deinterlace_blend(self.img)
        self.assertTrue(np.all(out[1:-1] == 120))

    def test_bad_field_rejected(self):
        with self.assertRaises(ValueError):
            color.bob_field(self.img, 2)


class Staircase(unittest.TestCase):
    def test_levels(self):
        levels = color.staircase_levels(16)
        self.assertEqual((len(levels), levels[0], levels[-1]), (16, 16, 235))
        self.assertTrue(all(b > a for a, b in zip(levels, levels[1:])))
        self.assertEqual([color.luma_to_display(v) for v in (16, 235, 0, 255)], [0, 255, 0, 255])


if __name__ == "__main__":
    unittest.main()
