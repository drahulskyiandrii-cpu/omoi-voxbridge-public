"""No physical audio capture: synthetic PCM, fake WASAPI devices only."""
import math
import threading
import unittest
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np = None

from voxbridge.wasapi_capture import (
    CaptureFailure, Downsample48To24, SOURCE_RATE,
    list_loopback_devices, capture_loopback,
)


class FakeMic:
    def __init__(self, device_id, isloopback, name, arrays=None):
        self.id = device_id
        self.isloopback = isloopback
        self.name = name
        self.arrays = arrays or []
        self.recorder_calls = 0

    def recorder(self, samplerate):
        assert samplerate == SOURCE_RATE
        self.recorder_calls += 1
        mic = self
        class Recorder:
            def __enter__(self):
                self.position = 0
                return self
            def __exit__(self, *args):
                return None
            def record(self, numframes):
                assert numframes <= 9600
                if self.position >= len(mic.arrays):
                    return np.zeros((numframes, 2), dtype="float32")
                data = mic.arrays[self.position]
                self.position += 1
                return data
        return Recorder()


class FakeSoundCard:
    def __init__(self, devices):
        self.devices = devices
    def all_microphones(self, include_loopback):
        assert include_loopback
        return self.devices
    def get_microphone(self, device_id, include_loopback):
        assert include_loopback
        return next(d for d in self.devices if d.id == device_id)


@unittest.skipUnless(np is not None, "NumPy required for SoundCard converter")
class LoopbackTests(unittest.TestCase):
    def test_discovery_filters_regular_microphones(self):
        fake = FakeSoundCard([
            FakeMic("spk", True, "Speakers"),
            FakeMic("mic", False, "Private microphone"),
        ])
        self.assertEqual([(d.id, d.name) for d in
                          list_loopback_devices(backend=fake)],
                         [("spk", "Speakers")])
        self.assertEqual(fake.devices[0].recorder_calls, 0)

    def test_regular_microphone_rejected_before_recorder(self):
        fake = FakeSoundCard([FakeMic("mic", False, "Microphone")])
        with self.assertRaisesRegex(CaptureFailure, "NOT_LOOPBACK"):
            list(capture_loopback("mic", threading.Event(), seconds=1, backend=fake))
        self.assertEqual(fake.devices[0].recorder_calls, 0)

    def test_48k_to_24k_resampling_and_pcm_range(self):
        t = np.arange(9600) / SOURCE_RATE
        waveform = (np.sin(2 * np.pi * 1000 * t) * 0.5).astype("float32")
        frames = np.column_stack((waveform, waveform))
        resampler = Downsample48To24()
        pcm = resampler.convert(frames)
        signal = np.frombuffer(pcm, dtype="<i2")
        self.assertEqual(len(signal), 4800)
        self.assertGreater(np.max(np.abs(signal[50:])), 15000)
        self.assertLess(np.max(np.abs(signal)), 32768)

    def test_filter_suppresses_aliased_high_frequencies(self):
        t = np.arange(9600) / SOURCE_RATE
        high = np.sin(2 * np.pi * 17000 * t).astype("float32")
        pcm = Downsample48To24().convert(np.column_stack((high, high)))
        audio = np.frombuffer(pcm, dtype="<i2")[100:]
        self.assertLess(np.sqrt(np.mean(audio.astype(float) ** 2)), 2200)

    def test_odd_chunks_preserve_output_sample_count(self):
        conv = Downsample48To24()
        parts = [np.ones((n, 1), dtype="float32") for n in (999, 2001, 1, 1000)]
        result = b"".join(conv.convert(a) for a in parts)
        self.assertEqual(len(result), 2 * math.ceil(sum(len(x) for x in parts) / 2))

    def test_no_nan_inf_or_multichannel_spill(self):
        conv = Downsample48To24()
        for arr in [np.array([[float("nan")]]),
                    np.ones((10, 9)),
                    np.ones((10, 1, 1))]:
            with self.assertRaises(CaptureFailure):
                conv.convert(arr)

    def test_explicit_generator_stops_after_first_frame(self):
        stop = threading.Event()
        dev = FakeMic("spk", True, "Speaker",
                      [np.zeros((9600, 2), dtype="float32")] * 10)
        fake = FakeSoundCard([dev])
        stream = capture_loopback("spk", stop, seconds=2, backend=fake)
        first = next(stream)
        self.assertEqual(len(first), 9600)
        stop.set()
        self.assertEqual(list(stream), [])
        self.assertEqual(dev.recorder_calls, 1)

    def test_cancelled_generator_never_opens_recorder(self):
        stop = threading.Event()
        stop.set()
        mic = FakeMic("spk", True, "Speaker")
        list(capture_loopback("spk", stop, seconds=1, backend=FakeSoundCard([mic])))
        self.assertEqual(mic.recorder_calls, 0)

    def test_invalid_duration_rejected_before_device(self):
        for s in (0, 121, False):
            with self.subTest(seconds=s):
                with self.assertRaisesRegex(CaptureFailure, "INVALID_DURATION"):
                    list(capture_loopback("spk", threading.Event(), seconds=s,
                                          backend=FakeSoundCard([])))

    def test_short_odd_reads_reblocked_and_counted_by_actual_frames(self):
        mic = FakeMic("spk", True, "Speaker",
                      [np.zeros((4799, 2), dtype="float32")] * 10)
        packets = list(capture_loopback("spk", threading.Event(), seconds=1,
                                       backend=FakeSoundCard([mic])))
        self.assertEqual([len(p) for p in packets], [9600] * 5)
        self.assertEqual(b"".join(packets), bytes(48000))

    def test_oversized_read_fails_before_any_pcm(self):
        mic = FakeMic("spk", True, "Speaker",
                      [np.zeros((9601, 2), dtype="float32")])
        stream = capture_loopback("spk", threading.Event(), seconds=1,
                                  backend=FakeSoundCard([mic]))
        with self.assertRaisesRegex(CaptureFailure, "FRAME_OVERRUN"):
            next(stream)

    def test_empty_read_is_sanitized_failure_not_synthetic_silence(self):
        mic = FakeMic("spk", True, "Speaker", [np.zeros((0, 2))])
        with self.assertRaisesRegex(CaptureFailure, "INVALID_FRAMES"):
            next(capture_loopback("spk", threading.Event(), seconds=1,
                                  backend=FakeSoundCard([mic])))

    def test_capture_deadline_after_blocked_read_discards_audio(self):
        mic = FakeMic("spk", True, "Speaker")
        with patch("voxbridge.wasapi_capture.time.monotonic", side_effect=[0, 1, 4]):
            with self.assertRaisesRegex(CaptureFailure, "CAPTURE_TIMEOUT"):
                next(capture_loopback("spk", threading.Event(), seconds=1,
                                      backend=FakeSoundCard([mic])))

    def test_cancel_during_device_discovery_never_opens_recorder(self):
        stop = threading.Event()
        mic = FakeMic("spk", True, "Speaker")
        class Card(FakeSoundCard):
            def all_microphones(self, include_loopback):
                stop.set()
                return super().all_microphones(include_loopback)
        self.assertEqual(list(capture_loopback("spk", stop, seconds=1,
                                               backend=Card([mic]))), [])
        self.assertEqual(mic.recorder_calls, 0)

    def test_device_replaced_after_discovery_is_not_captured(self):
        selected = FakeMic("spk", True, "Speaker")
        replacement = FakeMic("new", True, "Other speaker")
        class Card(FakeSoundCard):
            def get_microphone(self, device_id, include_loopback):
                return replacement
        with self.assertRaisesRegex(CaptureFailure, "NOT_LOOPBACK"):
            next(capture_loopback("spk", threading.Event(), seconds=1,
                                  backend=Card([selected])))
        self.assertEqual(replacement.recorder_calls, 0)

    def test_disconnect_closes_recorder_and_sanitizes_driver_failure(self):
        mic = FakeMic("spk", True, "Speaker")
        state = {"closed": False}
        class Recorder:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                state["closed"] = True
            def record(self, numframes):
                raise OSError("private endpoint name")
        with patch.object(mic, "recorder", return_value=Recorder()):
            with self.assertRaisesRegex(CaptureFailure, "^WASAPI_CAPTURE_FAILED$"):
                next(capture_loopback("spk", threading.Event(), seconds=1,
                                      backend=FakeSoundCard([mic])))
        self.assertTrue(state["closed"])

    def test_stop_during_read_discards_audio_and_closes_recorder(self):
        mic = FakeMic("spk", True, "Speaker")
        stop = threading.Event()
        state = {"closed": False}
        class Recorder:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                state["closed"] = True
            def record(self, numframes):
                stop.set()
                return np.ones((numframes, 2))
        with patch.object(mic, "recorder", return_value=Recorder()):
            self.assertEqual(list(capture_loopback("spk", stop, seconds=1,
                                                   backend=FakeSoundCard([mic]))), [])
        self.assertTrue(state["closed"])


if __name__ == "__main__":
    unittest.main()
