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

    def test_audio_plug_choice_is_remembered(self):
        combo = self.win.device_panel.audio_plug_combo
        self.assertEqual(combo.currentData(), "both")  # stereo unless chosen otherwise
        combo.activated.emit(combo.findData("red"))
        self.assertEqual(self.win.settings.audio_plug, "red")

    def test_recordings_take_sound_from_the_chosen_plug(self):
        self.win.settings.audio_plug = "white"
        device = mock.Mock(key="Windows WDM-KS::Analog Audio In ()")
        with mock.patch("vintagecam.ui.main_window.audio_io.find_input", return_value=device), \
                mock.patch("vintagecam.ui.main_window.AudioCapture") as capture:
            self.win._open_audio()
        capture.assert_called_once_with(device, plug="white")

    def test_a_finished_recording_gets_an_mp4_viewing_copy(self):
        path = Path(tempfile.gettempdir()) / "vintagecam_test_recording.mkv"
        result = mock.Mock(path=path, frames_written=10, frames_dropped=0, device_gaps=0, duration=0.3, error=None,
                           audio_seconds=None)
        with mock.patch.object(self.win.exports, "add") as add:
            self.win._on_recording_finished(result)
            add.assert_called_once_with([path], "auto")
            add.reset_mock()
            self.win.record_panel.export_after_check.setChecked(False)  # the user turns it off
            self.assertFalse(self.win.settings.export_after_recording)
            self.win._on_recording_finished(result)
            add.assert_not_called()

    def test_pot_meter_measures_only_while_its_panel_is_showing(self):
        self.assertFalse(self.win.analysis.enabled)  # the panel starts behind the Recording tab
        self.win._toggle_pot_dock()
        app.processEvents()
        self.assertFalse(self.win.pot_dock.visibleRegion().isEmpty())  # really on screen now
        self.assertTrue(self.win.analysis.enabled)
        self.assertTrue(self.win.preview.overlays.pot_grid)
        self.win.record_dock.raise_()  # as clicking the Recording tab does: the pot meter goes behind it
        app.processEvents()
        self.assertFalse(self.win.analysis.enabled)
        self.assertFalse(self.win.preview.overlays.pot_grid)
        self.win._toggle_pot_dock()  # back in front…
        app.processEvents()
        self.assertTrue(self.win.analysis.enabled)
        self.win.pot_panel.grid_check.setChecked(False)
        self.assertFalse(self.win.preview.overlays.pot_grid)
        self.win._toggle_pot_dock()  # …and closed
        app.processEvents()
        self.assertFalse(self.win.analysis.enabled)

    def test_the_pot_meter_light(self):
        from vintagecam.analysis import PotStatus

        panel = self.win.pot_panel
        self.assertEqual(panel.light_state, "grey")  # nothing measured yet
        panel.show_status(PotStatus("", 0.0, True, True, True, 0.31, 0.3))
        self.assertEqual((panel.light_state, panel.light_text.text()), ("green", "Best position"))
        panel.show_status(PotStatus("", 0.0, True, True, False, 0.6, 0.3))
        self.assertEqual(panel.light_state, "red")
        panel.show_status(PotStatus("cw", 0.4, False, True, None))
        self.assertEqual(panel.light_state, "grey")
        self.assertFalse(panel.cw_button.isEnabled())  # no second measurement while one runs

    def test_the_measure_buttons_start_a_measurement(self):
        import time

        self.win.pot_panel.cw_button.click()
        deadline = time.monotonic() + 2
        while self.win.analysis.meter.measuring != "cw" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.win.analysis.meter.measuring, "cw")

    def test_a_phone_photo_becomes_the_pot_meters_target(self):
        import time

        from PySide6.QtGui import QImage

        folder = Path(tempfile.mkdtemp())
        photo, portrait = folder / "card.png", folder / "portrait.png"
        for path, (w, h) in ((photo, (450, 330)), (portrait, (330, 450))):
            rgb = np.ascontiguousarray(np.full((h, w, 3), 230, np.uint8))
            QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).save(str(path))

        self.win._use_reference_photo(photo)
        self.assertEqual(self.win.settings.pot_reference_photo, str(photo))
        self.assertIn("card.png", self.win.pot_panel.reference_label.text())
        deadline = time.monotonic() + 2
        while self.win.analysis.meter.reference is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.win.analysis.meter.reference)

        self.win._use_reference_photo(portrait)  # refused, with a message; back to plain white
        self.assertEqual(self.win.settings.pot_reference_photo, "")
        self.assertIn("Plain white", self.win.pot_panel.reference_label.text())

    def test_record_key_without_video_does_not_start_a_recording(self):
        self.press(Qt.Key.Key_R)
        self.assertIsNone(self.win.recorder)

    def test_stuck_driver_waits_for_replug_then_restarts(self):
        """A capture thread stuck in the driver must not be piled on; restart once it's freed."""

        class StuckCapture:
            alive = True

            def set_record_sink(self, sink): pass
            def stop(self): pass
            def join(self, timeout=None): pass
            def is_alive(self): return self.alive

        stuck = StuckCapture()
        started = []
        self.win.capture = stuck
        with mock.patch.object(self.win, "_apply_decoder_standard"), \
                mock.patch.object(MainWindow, "_start_capture", lambda win: started.append(True)):
            self.win._restart_capture("test")
            self.assertIs(self.win._stuck_capture, stuck)
            self.assertIn("Unplug", self.win._capture_message)
            self.win._check_stuck_capture()
            self.assertEqual(started, [], "must not reopen while the driver still holds the old stream")
            stuck.alive = False  # the user replugged the card; the driver let go
            self.win._check_stuck_capture()
            self.assertEqual(started, [True])
            self.assertIsNone(self.win._stuck_capture)

    def test_switching_away_from_a_locked_picture_asks_first(self):
        from vintagecam.capture import CaptureState
        from PySide6.QtWidgets import QMessageBox

        self.win._capture_state, self.win._signal_locked = CaptureState.RUNNING, True
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No), \
                mock.patch.object(self.win, "_restart_capture") as restart:
            self.win._on_standard_selected("PAL")
        restart.assert_not_called()
        self.assertEqual(self.win.settings.video_standard, "NTSC")

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
