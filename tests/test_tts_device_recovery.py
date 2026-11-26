"""Playback state-machine regressions with fake sinks; no physical device."""
import unittest
from unittest.mock import Mock

from PySide6.QtMultimedia import QAudio
from voxbridge.audio_output import PlaybackEngine, BLOCK_BYTES, BUFFER_BYTES
from voxbridge.analysis_providers import ProviderFailure


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def device(identity=b"headset"):
    dev = Mock()
    dev.id.return_value = identity
    dev.isNull.return_value = False
    dev.isFormatSupported.return_value = True
    return dev


def sink(output):
    obj = Mock()
    obj.state.return_value = QAudio.State.ActiveState
    obj.error.return_value = QAudio.Error.NoError
    obj.processedUSecs.return_value = 0
    obj.bytesFree.return_value = 8192
    obj.start.return_value.write.side_effect = lambda data: (output.extend(data), len(data))[1]
    return obj


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.media = Mock()
        self.dev = device()
        self.media.defaultAudioOutput.return_value = self.dev
        self.media.audioOutputs.return_value = [self.dev]
        self.output = bytearray()
        self.sink = sink(self.output)
        self.factory = Mock(return_value=self.sink)
        self.engine = PlaybackEngine(clock=self.clock, media=self.media, sink_factory=self.factory)

    def drain(self, s=None):
        (s or self.sink).state.return_value = QAudio.State.IdleState
        self.clock.advance(.2)
        return self.engine.tick()

    def fail_device(self):
        self.sink.state.return_value = QAudio.State.StoppedState
        self.sink.error.return_value = QAudio.Error.IOError

    def test_reopen_replays_unacknowledged_tail_before_future_audio(self):
        e = self.engine
        audio = b"\x01\x00"*24 + b"\x02\x00"*24
        e.feed(audio)
        e.tick()
        self.assertEqual(self.output, audio)
        self.sink.processedUSecs.return_value = 1000
        e.tick()
        self.assertEqual(bytes(e.outstanding), audio[48:])
        self.fail_device()
        future = b"\x03\x00"*10
        e.feed(future)
        state = e.tick()
        self.assertEqual(bytes(e.replay), audio[48:])
        self.assertEqual(state.recovery_count, 1)
        self.sink.reset.assert_called_once()
        output = bytearray()
        second = sink(output)
        self.factory.return_value = second
        self.clock.advance(.4)
        e.tick()
        e.tick()
        self.assertEqual(output, audio[48:] + future)
        e.finish()
        self.assertTrue(self.drain(second).done)
        self.assertEqual(e.error, "")
        second.stop.assert_called_once()
        self.media.defaultAudioOutput.assert_called_once()

    def test_recovery_attempts_bounded_and_failure_reported(self):
        self.media.defaultAudioOutput.return_value.isNull.return_value = True
        e = self.engine
        e.feed(bytes(2))
        e.tick()
        for _ in range(2):
            self.clock.advance(1.1)
            e.tick()
        self.assertTrue(e.done)
        self.assertEqual(e.error, "AUDIO_DEVICE_FAILED: AUDIO_DEVICE_UNAVAILABLE")
        self.assertEqual(e.recovery_count, 2)
        self.assertEqual(self.media.defaultAudioOutput.call_count, 3)
        self.assertEqual(len(e.pending), 0)

    def test_stop_clears_scheduled_reopen_and_pcm(self):
        e = self.engine
        self.media.defaultAudioOutput.return_value.isNull.return_value = True
        e.feed(bytes(2))
        e.tick()
        self.assertGreater(e.reopen_at, 0)
        e.cancel()
        self.clock.advance(10)
        e.tick()
        self.assertEqual(self.media.defaultAudioOutput.call_count, 1)
        self.assertTrue(e.done)
        self.assertFalse(e.state().queued)
        self.assertEqual(e.error, "AUDIO_OUTPUT_CANCELLED")

    def test_endpoint_change_never_routes_to_unselected_speakers(self):
        e = self.engine
        e.device_id = b"headset"
        self.media.audioOutputs.return_value = [device(b"speaker")]
        e.feed(bytes(2))
        e.tick()
        self.assertEqual(e.device_id, b"headset")
        self.media.defaultAudioOutput.assert_not_called()
        self.factory.assert_not_called()

    def test_recoverable_underrun_is_not_device_failure(self):
        self.sink.state.return_value = QAudio.State.IdleState
        self.sink.error.return_value = QAudio.Error.UnderrunError
        self.engine.feed(b"\x01\x00")
        self.engine.tick()
        self.assertEqual(self.output, b"\x01\x00")
        self.assertEqual(self.engine.recovery_count, 0)
        self.assertFalse(self.engine.done)

    def test_stalled_device_recovers_without_new_sink_until_delay(self):
        e = self.engine
        self.sink.bytesFree.return_value = 0
        e.feed(bytes(BLOCK_BYTES))
        e.tick()
        self.clock.advance(9)
        e.tick()
        self.assertEqual(e.recovery_count, 1)
        self.assertEqual(len(e.pending), BLOCK_BYTES)
        self.sink.reset.assert_called_once()
        self.factory.assert_called_once()
        self.assertFalse(e.done)

    def test_stop_resets_device_instead_of_draining(self):
        self.engine.feed(bytes(2))
        self.engine.tick()
        state = self.engine.cancel()
        self.sink.reset.assert_called_once()
        self.sink.stop.assert_not_called()
        self.assertTrue(state.done)
        self.assertFalse(state.queued)

    def test_idle_provider_gap_is_not_device_stall(self):
        e = self.engine
        e.feed(bytes(2))
        e.tick()
        self.sink.state.return_value = QAudio.State.IdleState
        e.tick()
        self.clock.advance(46)
        e.tick()
        self.assertEqual(e.recovery_count, 0)
        self.assertFalse(e.done)
        # First new input after a long provider gap gets a fresh stall budget.
        e.feed(bytes(2))
        self.sink.bytesFree.return_value = 0
        e.tick()
        self.assertEqual(e.recovery_count, 0)

    def test_device_writes_are_bounded_and_ordered(self):
        e = self.engine
        self.sink.bytesFree.return_value = 96000
        blocks = [bytes(BLOCK_BYTES), b"\x01\x00"*(BLOCK_BYTES//2), b"\x02\x00"*(BLOCK_BYTES//2)]
        for block in blocks:
            e.feed(block)
        e.tick()
        self.assertEqual(self.output, b"".join(blocks[:2]))
        e.tick()
        self.assertEqual(self.output, b"".join(blocks))

    def test_recovery_after_remote_done_preserves_tail(self):
        e = self.engine
        data = b"\x01\x00"*100
        e.feed(data)
        e.tick()
        self.fail_device()
        e.finish()
        e.tick()
        self.assertEqual(e.recovery_count, 1)
        output = bytearray()
        second = sink(output)
        self.factory.return_value = second
        self.clock.advance(.4)
        e.tick()
        self.assertEqual(self.output, output)
        self.assertTrue(self.drain(second).done)
        self.assertEqual(e.error, "")

    def test_free_buffer_without_played_ack_never_claims_completion(self):
        e = self.engine
        audio = b"\x01\x00"*100
        e.feed(audio)
        e.tick()
        self.sink.bytesFree.return_value = 96000
        e.finish()
        self.clock.advance(.2)
        self.assertFalse(e.tick().done)
        self.assertIs(e.sink, self.sink)
        self.assertEqual(bytes(e.outstanding), audio)
        self.assertTrue(self.drain().done)

    def test_final_idle_underrun_acknowledges_tail(self):
        e = self.engine
        e.feed(bytes(200))
        e.tick()
        e.finish()
        self.sink.error.return_value = QAudio.Error.UnderrunError
        self.assertTrue(self.drain().done)
        self.assertIsNone(e.sink)
        self.assertEqual(e.error, "")

    def test_reset_failure_retains_exact_sink_and_blocks_reopen(self):
        e = self.engine
        e.feed(bytes(200))
        e.tick()
        self.fail_device()
        self.sink.reset.side_effect = OSError("private driver error")
        e.tick()
        self.assertIs(e.sink, self.sink)
        self.assertIsNone(e.io)
        self.assertTrue(e.cleanup_failed)
        self.assertTrue(e.done)
        self.assertFalse(e.state().queued)
        self.assertEqual(e.error, "AUDIO_OUTPUT_CLEANUP_FAILED")
        self.sink.deleteLater.assert_not_called()
        e.tick()
        e.cancel()
        self.sink.reset.assert_called_once()
        self.factory.assert_called_once()

    def test_finish_cleanup_failure_does_not_report_success(self):
        e = self.engine
        e.feed(bytes(200))
        e.tick()
        e.finish()
        self.sink.stop.side_effect = RuntimeError("private stop failure")
        self.assertTrue(self.drain().done)
        self.assertEqual(e.error, "AUDIO_OUTPUT_CLEANUP_FAILED")
        self.assertTrue(e.cleanup_failed)
        self.assertIs(e.sink, self.sink)
        self.sink.deleteLater.assert_not_called()

    def test_delete_later_failure_is_quarantined_in_child(self):
        e = self.engine
        e.feed(bytes(2))
        e.tick()
        self.sink.deleteLater.side_effect = RuntimeError("private QObject failure")
        e.cancel()
        self.assertTrue(e.cleanup_failed)
        self.assertIs(e.sink, self.sink)
        e.cancel()
        self.sink.reset.assert_called_once()
        self.sink.deleteLater.assert_called_once()

    def test_buffer_overflow_and_malformed_input_rejected(self):
        e = self.engine
        for _ in range(BUFFER_BYTES // BLOCK_BYTES):
            e.feed(bytes(BLOCK_BYTES))
        with self.assertRaisesRegex(ProviderFailure, "PCM_BACKPRESSURE_OVERFLOW"):
            e.feed(bytes(BLOCK_BYTES))
        for bad in (b"", b"x", bytes(BLOCK_BYTES+2), "not bytes"):
            with self.assertRaises(ProviderFailure):
                e.feed(bad)
        e.cancel()
        self.assertFalse(e.state().queued)

    def test_no_audio_cannot_be_success(self):
        self.engine.finish()
        self.assertTrue(self.engine.tick().done)
        self.assertEqual(self.engine.error, "TTS_STREAM_NO_AUDIO")
        self.factory.assert_not_called()

    def test_partial_write_preserves_unwritten_suffix(self):
        e = self.engine
        data = b"\x01\x00\x02\x00\x03\x00"
        accepted = bytearray()
        def partial(payload):
            accepted.extend(payload[:2])
            return 2
        self.sink.start.return_value.write.side_effect = partial
        e.feed(data)
        e.tick()
        e.tick()
        e.tick()
        self.assertEqual(accepted, data)
        self.assertEqual(bytes(e.outstanding), data)
        self.assertEqual(len(e.pending), 0)
