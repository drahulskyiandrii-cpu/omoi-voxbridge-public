"""Dictation consent and lifecycle contracts; no device or API access."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from voxbridge.app import VoxBridge
from voxbridge.session import Session, State
from voxbridge.audio import Microphone, pcm_to_wav


def complete_microphone_stop(app):
    generation = app.session.snapshot().generation
    app.mic = Mock()
    app.mic.busy = False
    app.mic_deadline = Mock()
    VoxBridge.finish_recording(app)
    app.mic.finish.assert_called_once()
    VoxBridge._microphone_stopped(app, generation, app.recorder.stop.return_value, "")


class DictationPrivacyTests(unittest.TestCase):
    def fixture(self, approval=True):
        session = Session()
        generation = session.begin(10, 20)
        app = NS(
            session=session, _shutting_down=False, _dictation_worker_generation=None,
            _approval_dialog=None, settings=NS(stt_backend="openai_cloud", language="de"),
            deadline=Mock(), recorder=Mock(), status=Mock(), notify=Mock(),
            _approve_dictation=Mock(return_value=approval), executor=Mock(),
            signals=Mock(), active_backend=None,
        )
        app.recorder.stop.return_value = pcm_to_wav(bytes(6400))
        app.on_completed = lambda *args: VoxBridge.on_completed(app, *args)
        return app, generation

    def test_declined_recording_never_schedules_cloud(self):
        app, _ = self.fixture(False)
        complete_microphone_stop(app)
        app.executor.submit.assert_not_called()
        self.assertEqual(app.session.snapshot().state, State.IDLE)

    def test_cancel_during_modal_approval_never_schedules_cloud(self):
        app, _ = self.fixture()
        app._approve_dictation.side_effect = lambda: (app.session.cancel(), True)[1]
        complete_microphone_stop(app)
        app.executor.submit.assert_not_called()

    def test_local_config_cannot_reach_worker_even_after_recording(self):
        app, _ = self.fixture()
        app.settings.stt_backend = "shared"
        complete_microphone_stop(app)
        app._approve_dictation.assert_not_called()
        app.executor.submit.assert_not_called()

    def test_cancelled_remote_worker_blocks_new_sessions_until_exit(self):
        app, gen = self.fixture()
        complete_microphone_stop(app)
        self.assertEqual(app._dictation_worker_generation, gen)
        app.session.cancel()
        self.assertFalse(VoxBridge._dictation_idle(app))
        app.live_captions_dialog = None
        app.recorder.start.reset_mock()
        VoxBridge.on_hotkey(app, 123)
        app.recorder.start.assert_not_called()
        VoxBridge.on_completed(app, gen, "old text", "")
        self.assertTrue(VoxBridge._dictation_idle(app))
        self.assertEqual(app.session.snapshot().state, State.IDLE)

    def test_provider_cleanup_failure_preserves_confirmed_text_and_blocks_retry(self):
        from voxbridge.long_dictation import IncompleteDictation
        app, gen = self.fixture()
        provider = Mock()
        provider.transcribe.return_value = "Confirmed synthetic text"
        provider.close.side_effect = OSError("private")
        complete_microphone_stop(app)
        worker = app.executor.submit.call_args.args[0]
        with patch("voxbridge.app.OpenAIFileSTT", return_value=provider):
            with self.assertRaisesRegex(IncompleteDictation, "^PROVIDER_CLEANUP_FAILED$") as caught:
                worker()
        self.assertEqual(caught.exception.text, "Confirmed synthetic text")
        self.assertIsNone(app.active_backend)
        with patch("voxbridge.app.show_manual_fallback") as manual:
            VoxBridge.on_completed(app, gen, caught.exception.text, str(caught.exception))
            manual.assert_called_once_with("Confirmed synthetic text", "INCOMPLETE_DICTATION: PROVIDER_CLEANUP_FAILED")
        self.assertTrue(app._stt_cleanup_failed)
        VoxBridge.on_hotkey(app, 123)
        self.assertEqual(app.notify.call_args.args[0], "PROVIDER_CLEANUP_FAILED")

    def test_submit_failure_unblocks_recording(self):
        app, _ = self.fixture()
        app.executor.submit.side_effect = RuntimeError("mock")
        complete_microphone_stop(app)
        self.assertTrue(VoxBridge._dictation_idle(app))

    def test_microphone_buffer_cleared_on_stop_and_cancel_failures(self):
        for method in ("stop", "cancel"):
            mic = Microphone()
            mic._buffer.extend(bytes(6400))
            mic._stream = Mock()
            mic._stream.stop.side_effect = RuntimeError("mock")
            stream = mic._stream
            with self.assertRaises(RuntimeError):
                getattr(mic, method)()
            stream.close.assert_called_once()
            self.assertEqual(len(mic._buffer), 0)
            self.assertIsNone(mic._stream)
