"""Choosing the audio input (with a fake PortAudio, so no hardware is needed)."""

import types
import unittest
from unittest import mock

import numpy as np

from vintagecam import audio


class FakeSoundDevice:
    """Just enough of the sounddevice module for list_inputs()."""

    def __init__(self, devices, apis):
        self._devices, self._apis = devices, apis

    def query_devices(self):
        return self._devices

    def query_hostapis(self):
        return self._apis


APIS = [{"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
DEVICES = [
    {"name": "Analog Audio In (Elgato Video C", "hostapi": 0, "max_input_channels": 2},  # MME: the broken route
    {"name": "Microphone (Realtek)", "hostapi": 1, "max_input_channels": 2},
    {"name": "Speakers (Realtek)", "hostapi": 1, "max_input_channels": 0},  # output only
    {"name": "Analog Audio In ()", "hostapi": 2, "max_input_channels": 2},  # the Elgato via kernel streaming
]


class AudioInputChoice(unittest.TestCase):
    def setUp(self):
        patch = mock.patch("vintagecam.audio._sd", return_value=FakeSoundDevice(DEVICES, APIS))
        patch.start()
        self.addCleanup(patch.stop)
        self.inputs = audio.list_inputs()

    def test_only_usable_inputs_are_listed_and_the_elgato_comes_first(self):
        self.assertEqual([(d.index, d.host_api) for d in self.inputs],
                         [(3, "Windows WDM-KS"), (1, "Windows WASAPI")])
        self.assertTrue(self.inputs[0].is_elgato)
        self.assertEqual(self.inputs[0].label, "Elgato line input (kernel streaming)")

    def test_auto_picks_the_elgato(self):
        self.assertEqual(audio.find_input(audio.AUTO, self.inputs).index, 3)

    def test_a_saved_device_is_found_by_its_key(self):
        chosen = audio.find_input("Windows WASAPI::Microphone (Realtek)", self.inputs)
        self.assertEqual(chosen.name, "Microphone (Realtek)")

    def test_a_missing_saved_device_falls_back_to_the_elgato(self):
        self.assertTrue(audio.find_input("Windows WASAPI::USB Interface", self.inputs).is_elgato)

    def test_no_elgato_means_no_automatic_choice(self):
        others = [d for d in self.inputs if not d.is_elgato]
        self.assertIsNone(audio.find_input(audio.AUTO, others))


class PlugChoice(unittest.TestCase):
    """Only the chosen plug's channel is passed on: white = left, red = right."""

    ELGATO = audio.AudioInput(0, "Analog Audio In ()", "Windows WDM-KS")

    def capture(self, plug):
        cap = audio.AudioCapture(self.ELGATO, plug=plug)
        got = []
        cap.sink = lambda samples, captured: got.append(samples)
        white, red = np.full(480, -100, np.int16), np.full(480, 200, np.int16)
        cap._callback(np.column_stack([white, red]), 480, types.SimpleNamespace(currentTime=0.0, inputBufferAdcTime=0.0),
                      types.SimpleNamespace(input_overflow=False))
        return cap, got[0]

    def test_red_is_the_right_channel_recorded_as_mono(self):
        cap, samples = self.capture("red")
        self.assertEqual((cap.channels, samples.shape), (1, (480, 1)))
        self.assertTrue((samples == 200).all())

    def test_white_is_the_left_channel_recorded_as_mono(self):
        cap, samples = self.capture("white")
        self.assertEqual((cap.channels, samples.shape), (1, (480, 1)))
        self.assertTrue((samples == -100).all())

    def test_both_keeps_stereo_in_order(self):
        cap, samples = self.capture("both")
        self.assertEqual((cap.channels, samples.shape), (2, (480, 2)))
        np.testing.assert_array_equal(samples[0], [-100, 200])

    def test_the_level_meter_follows_the_chosen_plug(self):
        self.assertAlmostEqual(self.capture("red")[0].level_dbfs(), 20 * np.log10(200 / 32768), places=6)
        self.assertAlmostEqual(self.capture("white")[0].level_dbfs(), 20 * np.log10(100 / 32768), places=6)

    def test_without_a_choice_both_plugs_are_passed_on(self):
        self.assertEqual(audio.AudioCapture(self.ELGATO).channels, 2)


class SampleClock(unittest.TestCase):
    """Capture times come from counting samples, so late callbacks can't throw them off."""

    BLOCK = 480 / 48000  # 10 ms

    def setUp(self):
        self.now = 100.0
        patch = mock.patch("vintagecam.audio.time.perf_counter", side_effect=lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.cap = audio.AudioCapture(audio.AudioInput(0, "Analog Audio In ()", "Windows WDM-KS"))
        self.stamps = []
        self.cap.sink = lambda samples, captured: self.stamps.append(captured)
        self.info = types.SimpleNamespace(currentTime=0.0, inputBufferAdcTime=0.0)

    def feed(self, arrival, overflow=False):
        self.now = arrival
        self.cap._callback(np.zeros((480, 2), np.int16), 480, self.info,
                           types.SimpleNamespace(input_overflow=overflow))

    def test_a_frozen_pc_does_not_move_capture_times(self):
        for k in range(100):
            normal = 100.0 + (k + 1) * self.BLOCK + 0.002  # a callback 2 ms after its block completes
            self.feed(max(normal, 100.8) if k >= 50 else normal)  # Python froze from 0.5 s to 0.8 s
        expected = [100.002 + k * self.BLOCK for k in range(100)]
        np.testing.assert_allclose(self.stamps, expected, rtol=0, atol=1e-9)

    def test_sound_the_driver_threw_away_moves_the_clock_on(self):
        lost = 0.1
        for k in range(40):
            self.feed(100.0 + (k + 1) * self.BLOCK + 0.002 + (lost if k >= 20 else 0.0), overflow=(k == 20))
        self.assertEqual(self.cap.overflows, 1)
        self.assertAlmostEqual(self.stamps[10], 100.002 + 10 * self.BLOCK, places=9)
        self.assertAlmostEqual(self.stamps[25], 100.002 + 25 * self.BLOCK + lost, places=9)


if __name__ == "__main__":
    unittest.main()
