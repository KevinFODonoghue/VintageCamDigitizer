"""The real main window, off-screen and without hardware: shortcuts, overlays and drawing."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no visible window needed

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vintagecam import color  # noqa: E402
from vintagecam.config import Settings  # noqa: E402
from vintagecam.frames import frame_from_uyvy  # noqa: E402
from vintagecam.ui.log_panel import QtLogHandler  # noqa: E402
from vintagecam.ui.main_window import MainWindow  # noqa: E402
from vintagecam.video_format import NTSC  # noqa: E402

app = QApplication.instance() or QApplication([])


class MainWindowTests(unittest.TestCase):
    def setUp(self):
        # No capture device in unit tests, and never overwrite the real settings.json.
        for patch in (mock.patch.object(MainWindow, "_startup", lambda self: None),
                      mock.patch("vintagecam.ui.main_window.save_settings")):
            patch.start()
            self.addCleanup(patch.stop)
        settings = Settings(show_grid=False, show_crosshair=False, output_dir=tempfile.gettempdir())
        self.win = MainWindow(settings, [], QtLogHandler(), Path(tempfile.gettempdir()))
        self.addCleanup(self.win.close)
        self.win.show()
        QTest.qWaitForWindowActive(self.win, 2000)
        self.win.preview.setFocus()

    def press(self, key: Qt.Key) -> None:
        QTest.keyClick(self.win.preview, key)
        app.processEvents()

    def test_overlay_shortcuts_toggle(self):
        cases = ((Qt.Key.Key_G, "grid", "show_grid"), (Qt.Key.Key_C, "crosshair", "show_crosshair"),
                 (Qt.Key.Key_S, "safe_areas", "show_safe_areas"), (Qt.Key.Key_L, "staircase", "show_staircase"))
        for key, overlay, setting in cases:
            before = getattr(self.win.preview.overlays, overlay)
            self.press(key)
            self.assertEqual(getattr(self.win.preview.overlays, overlay), not before, f"{key.name} didn't toggle")
            self.assertEqual(getattr(self.win.settings, setting), not before)
            self.press(key)
            self.assertEqual(getattr(self.win.preview.overlays, overlay), before, f"{key.name} didn't toggle back")

    def test_deinterlace_fit_and_freeze_keys(self):
        modes = []
        for _ in range(3):
            self.press(Qt.Key.Key_D)
            modes.append(self.win.settings.deinterlace)
        self.assertEqual(modes, ["bob", "blend", "off"])
        self.press(Qt.Key.Key_F)
        self.assertFalse(self.win.settings.fit_to_window)
        self.press(Qt.Key.Key_F)
        self.assertTrue(self.win.settings.fit_to_window)
        self.press(Qt.Key.Key_Space)
        self.assertTrue(self.win.preview.frozen)
        self.press(Qt.Key.Key_Space)
        self.assertFalse(self.win.preview.frozen)

    def test_record_key_without_video_does_not_start_a_recording(self):
        self.press(Qt.Key.Key_R)
        self.assertIsNone(self.win.recorder)

    def test_a_frame_is_drawn_in_true_colour(self):
        rgb = np.zeros((480, 720, 3), np.uint8)
        rgb[:] = (191, 0, 0)  # 75% red
        self.win._display(frame_from_uyvy(color.rgb_to_uyvy(rgb), NTSC))
        shot = self.win.preview.grab().toImage()
        pixel = shot.pixelColor(shot.width() // 2, shot.height() // 2)
        self.assertLessEqual(abs(pixel.red() - 191) + pixel.green() + pixel.blue(), 9,
                             f"centre of the picture is {pixel.getRgb()}, expected about (191, 0, 0)")


if __name__ == "__main__":
    unittest.main()
