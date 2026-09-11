"""The newest-wins slot, settings handling, error classification and capture options."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from vintagecam.capture import CaptureRequest
from vintagecam.config import Settings, load_settings, save_settings
from vintagecam.errors import DeviceBusyError, DeviceNotFoundError, UnsupportedFormatError, classify_open_error
from vintagecam.frames import LatestSlot
from vintagecam.video_format import NTSC, PAL


class LatestSlotTests(unittest.TestCase):
    def test_put_reports_empty_and_newest_wins(self):
        slot = LatestSlot()
        self.assertTrue(slot.put(1))  # was empty -> notify the consumer
        self.assertFalse(slot.put(2))  # 1 was never collected -> dropped
        self.assertEqual(slot.take(), 2)
        self.assertIsNone(slot.take())
        self.assertEqual(slot.replaced, 1)

    def test_wait_take(self):
        slot = LatestSlot()
        start = time.perf_counter()
        self.assertIsNone(slot.wait_take(0.05))
        self.assertGreaterEqual(time.perf_counter() - start, 0.04)
        threading.Timer(0.02, slot.put, args=("x",)).start()
        self.assertEqual(slot.wait_take(2.0), "x")

    def test_consumer_only_ever_sees_newer_items(self):
        slot, seen = LatestSlot(), []
        done = threading.Event()

        def consume():
            while not done.is_set():
                item = slot.wait_take(0.01)
                if item is not None:
                    seen.append(item)

        consumer = threading.Thread(target=consume)
        consumer.start()
        for i in range(20000):
            slot.put(i)
        time.sleep(0.05)
        done.set()
        consumer.join()
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 19999)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(warnings, [])

    def test_round_trip(self):
        original = Settings(show_grid=True, video_standard="PAL", low_disk_warning_gb=50.0, deinterlace="bob",
                            audio_plug="red")
        save_settings(original, self.path)
        loaded, warnings = load_settings(self.path)
        self.assertEqual(loaded, original)
        self.assertEqual(warnings, [])

    def test_corrupt_file_is_moved_aside(self):
        self.path.write_text("{ not json", encoding="utf-8")
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(len(warnings), 1)
        self.assertTrue(self.path.with_suffix(".json.bad").exists())

    def test_settings_from_before_audio_worked_are_migrated_quietly(self):
        self.path.write_text(json.dumps({
            "record_audio": False, "audio_device": "Analog Audio In (Elgato Video Capture)",
        }), encoding="utf-8")
        settings, warnings = load_settings(self.path)
        self.assertEqual(warnings, [])
        self.assertTrue(settings.audio_enabled)
        self.assertEqual(settings.audio_device, "auto")
        self.assertEqual(settings.audio_plug, "both")  # stereo unless chosen otherwise

    def test_bad_values_are_repaired_and_reported(self):
        self.path.write_text(json.dumps({
            "video_standard": "SECAM", "show_grid": "yes", "bogus": 1, "low_disk_warning_gb": 5, "deinterlace": "x",
            "audio_plug": "green",
        }), encoding="utf-8")
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings.video_standard, "NTSC")
        self.assertEqual(settings.show_grid, Settings().show_grid)  # "yes" isn't a bool -> default
        self.assertEqual(settings.low_disk_warning_gb, 5.0)  # ints are fine for floats
        self.assertEqual(settings.deinterlace, "off")
        self.assertEqual(settings.audio_plug, "both")
        self.assertEqual(len(warnings), 5)


class CaptureOptions(unittest.TestCase):
    def test_framerate_is_2997_never_the_rational_form(self):
        """Regression guard: the driver/FFmpeg combination rejects 30000/1001 (see video_format.py)."""
        options = CaptureRequest("Elgato Video Capture", NTSC).dshow_options()
        self.assertEqual(options["framerate"], "29.97")
        self.assertNotIn("30000/1001", options.values())
        self.assertEqual(options["video_size"], "720x480")
        self.assertEqual(options["pixel_format"], "uyvy422")
        self.assertEqual(options["crossbar_video_input_pin_number"], "0")
        self.assertEqual(options["crossbar_audio_input_pin_number"], "2")

    def test_svideo_and_pal(self):
        options = CaptureRequest("Elgato Video Capture", PAL, video_input="svideo").dshow_options()
        self.assertEqual((options["video_size"], options["framerate"]), ("720x576", "25"))
        self.assertEqual(options["crossbar_video_input_pin_number"], "1")


class ErrorClassification(unittest.TestCase):
    """The log lines below were observed on the target machine."""

    def test_busy(self):
        err = classify_open_error(OSError(5, "I/O error"), [
            "Could not run graph (sometimes caused by a device already in use by other application)"], "Elgato")
        self.assertIsInstance(err, DeviceBusyError)

    def test_not_found(self):
        err = classify_open_error(OSError(5, "I/O error"), [
            "Could not find video device with name [Elgato] among source devices of type video."], "Elgato")
        self.assertIsInstance(err, DeviceNotFoundError)

    def test_bad_format_is_retried_only_slowly(self):
        err = classify_open_error(OSError(5, "I/O error"), ["Could not set video options"], "Elgato")
        self.assertIsInstance(err, UnsupportedFormatError)
        self.assertGreaterEqual(err.retry_after, 10.0)

    def test_busy_detected_from_exception_text_alone(self):
        """PyAV may suppress a repeated log line; its exception text still carries the reason."""
        exc = OSError(5, "I/O error: 'video=Elgato'; last error log: [dshow] Could not run graph (sometimes "
                         "caused by a device already in use by other application)")
        self.assertIsInstance(classify_open_error(exc, [], "Elgato"), DeviceBusyError)

    def test_audio_pin_failure_is_not_reported_as_missing_device(self):
        err = classify_open_error(OSError(5, "I/O error"), [
            "Could not find output pin from audio only capture device.",
            "Could not find audio only device with name [X] among source devices of type video."], "X", "audio")
        self.assertIsInstance(err, UnsupportedFormatError)


if __name__ == "__main__":
    unittest.main()
