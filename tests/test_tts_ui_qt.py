"""Offscreen Qt tests: no physical audio device, real API or billable request."""
import os
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from voxbridge.tts_ui import CloudTTSDialog
except ImportError:
    QApplication = None

from voxbridge.config import Settings
from voxbridge.output_isolation import OutputResult


@unittest.skipUnless(QApplication is not None, "PySide6 unavailable")
class TTSUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_construction_is_offline(self):
        factory = Mock(side_effect=AssertionError("must not use API"))
        dlg = CloudTTSDialog(provider_factory=factory)
        self.assertFalse(dlg.stop.isEnabled())
        self.assertEqual(dlg.language.currentData(), "auto")
        self.assertTrue(dlg.play.isEnabled())
        self.assertEqual(dlg.text.toPlainText(), "")
        factory.assert_not_called()
        dlg.close()

    def test_requires_explicit_consent(self):
        factory = Mock(side_effect=AssertionError("must not use API"))
        dlg = CloudTTSDialog(provider_factory=factory)
        dlg.text.setPlainText("Тест")
        dlg._play()
        self.assertIn("Cloud-Freigabe", dlg.status.text())
        factory.assert_not_called()
        dlg.close()

    def test_edit_requires_fresh_consent(self):
        dlg = CloudTTSDialog(provider_factory=Mock())
        dlg.text.setPlainText("First")
        dlg.consent.setChecked(True)
        dlg.text.setPlainText("Second")
        self.assertFalse(dlg.consent.isChecked())
        dlg.close()

    def test_mock_no_audio_stream_completes_with_error(self):
        class ZeroStream:
            def __iter__(self):
                return iter([NS(type="done", status_code=200)])
            def close(self):
                pass
        client = Mock()
        client.tts.generate_sse.return_value = ZeroStream()
        factory = Mock(return_value=NS(model="sonic-latest", voice_id="mock-only", client=client))
        dlg = CloudTTSDialog(provider_factory=factory)
        dlg.text.setPlainText("Тест")
        dlg.consent.setChecked(True)
        settings = Settings(cartesia_voice_id="11111111-1111-4111-8111-111111111111")
        with patch("voxbridge.tts_ui.load_settings", return_value=settings):
            dlg._play()
        end = time.monotonic() + 3
        while dlg._busy and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(dlg._busy)
        self.assertIn("CARTESIA_STREAM_INCOMPLETE", dlg.status.text())
        self.assertTrue(dlg.play.isEnabled())
        self.assertFalse(dlg.consent.isChecked())
        self.assertEqual(client.tts.generate_sse.call_count, 1)
        dlg.close()

    def test_stop_invalidates_generation_and_disables_new_requests_until_finished(self):
        dlg = CloudTTSDialog(provider_factory=Mock())
        dlg._generation = 7
        dlg._worker_generation = 7
        dlg._busy = True  # pretend that a request is already running
        dlg._cancel = __import__("threading").Event()
        dlg._stop()
        self.assertTrue(dlg._cancel.is_set())
        self.assertEqual(dlg._generation, 8)
        self.assertFalse(dlg.play.isEnabled())
        dlg._completed(7, "CARTESIA_CANCELLED")
        self.assertTrue(dlg.play.isEnabled())
        dlg.close()


    def test_pcm_worker_signal_starts_isolated_output_and_completes(self):
        import threading
        output = bytearray()
        def runner(incoming, finished, stop, recover):
            self.assertTrue(finished.is_set())
            while not incoming.empty():
                output.extend(incoming.get_nowait())
            return OutputResult()
        dlg = CloudTTSDialog(provider_factory=Mock(), output_runner=runner)
        dlg._generation = dlg._worker_generation = 4
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(b"\x01\x00\x02\x00\x03\x00")
        with patch("voxbridge.tts_ui.threading.Thread", side_effect=lambda *,target,**kwargs:NS(start=target)):
            dlg._completed(4, "")
        self.assertEqual(output, b"\x01\x00\x02\x00\x03\x00")
        self.assertIsNone(dlg._output_generation)
        self.assertTrue(dlg.play.isEnabled())
        self.assertFalse(dlg.stop.isEnabled())
        self.assertIn("Fertig", dlg.status.text())
        dlg.close()

    def test_output_failure_cancels_remote_without_touching_speaker(self):
        import threading
        dlg = CloudTTSDialog(provider_factory=Mock(),
                             output_runner=lambda *args: OutputResult("AUDIO_OUTPUT_PROTOCOL"))
        dlg._generation = dlg._worker_generation = 3
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(2))
        with patch("voxbridge.tts_ui.threading.Thread", side_effect=lambda *,target,**kwargs:NS(start=target)):
            dlg._pump()
        self.assertTrue(dlg._cancel.is_set())
        self.assertTrue(dlg._incoming.empty())
        self.assertFalse(dlg.play.isEnabled())
        self.assertIn("AUDIO_OUTPUT_PROTOCOL", dlg.status.text())
        dlg._completed(3, "CARTESIA_CANCELLED")
        self.assertTrue(dlg.play.isEnabled())
        dlg.close()


    def test_fast_producer_backpressure_and_stop(self):
        import threading
        from voxbridge.tts_stream import MAX_CHUNK_BYTES
        settings = Settings(cartesia_voice_id="11111111-1111-4111-8111-111111111111")
        dlg = CloudTTSDialog(provider_factory=Mock())
        dlg.text.setPlainText("Synthetic")
        dlg.consent.setChecked(True)
        entered = threading.Event()
        done = threading.Event()
        def fast(provider, text, language, emit, cancel):
            entered.set()
            try:
                emit(bytes(MAX_CHUNK_BYTES))
            finally:
                done.set()
        with patch("voxbridge.tts_ui.load_settings", return_value=settings), patch("voxbridge.tts_ui.stream_to_callback", side_effect=fast):
            dlg._play()
            self.assertTrue(entered.wait(1))
            # Don't process Qt events: producer must block at eight chunks.
            end = time.monotonic() + 1
            while dlg._incoming.qsize() < 8 and time.monotonic() < end:
                time.sleep(.01)
            self.assertEqual(dlg._incoming.qsize(), 8)
            self.assertFalse(done.is_set())
            dlg._stop()
            self.assertTrue(done.wait(1))
            end = time.monotonic() + 2
            while dlg._busy and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
        self.assertFalse(dlg._busy)
        self.assertTrue(dlg.play.isEnabled())
        dlg.close()

    def test_pending_audio_is_drained_even_after_remote_completion(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        output = bytearray()
        def runner(incoming, finished, stop, recover):
            self.assertTrue(finished.is_set())
            output.extend(incoming.get_nowait())
            entered.set()
            release.wait(2)
            return OutputResult()
        dlg = CloudTTSDialog(provider_factory=Mock(), output_runner=runner)
        dlg._generation = dlg._worker_generation = 9
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(4096))
        try:
            dlg._completed(9, "")
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(output), 4096)
            self.assertTrue(dlg._remote_done)
            self.assertTrue(dlg.busy)
            self.assertFalse(dlg.play.isEnabled())
            release.set()
            end = time.monotonic() + 2
            while dlg.busy and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg.busy)
            self.assertTrue(dlg.play.isEnabled())
        finally:
            release.set()
            dlg.close()

    def test_stalled_output_cancels_and_unlocks_after_remote_finishes(self):
        import threading
        dlg = CloudTTSDialog(provider_factory=Mock(),
                             output_runner=lambda *args: OutputResult("AUDIO_DEVICE_FAILED: AUDIO_PLAYBACK_STALLED"))
        dlg._generation = dlg._worker_generation = 6
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(4096))
        with patch("voxbridge.tts_ui.threading.Thread", side_effect=lambda *,target,**kwargs:NS(start=target)):
            dlg._pump()
        self.assertTrue(dlg._cancel.is_set())
        self.assertEqual(dlg.status.text(), "AUDIO_DEVICE_FAILED: AUDIO_PLAYBACK_STALLED")
        self.assertFalse(dlg.play.isEnabled())
        dlg._completed(6, "CARTESIA_CANCELLED")
        self.assertTrue(dlg.play.isEnabled())
        dlg.close()

    def test_stop_keeps_output_lease_until_supervisor_exits(self):
        import threading
        from types import SimpleNamespace
        from voxbridge.app import VoxBridge
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def runner(incoming, finished, stop, recover):
            entered.set()
            stop.wait(2)
            stopped.set()
            release.wait(2)
            return OutputResult()
        dlg = CloudTTSDialog(provider_factory=Mock(), output_runner=runner)
        dlg._generation = dlg._worker_generation = 5
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(2))
        try:
            dlg._pump()
            self.assertTrue(entered.wait(1))
            dlg._stop()
            self.assertTrue(stopped.wait(1))
            dlg._completed(5, "TTS_CANCELLED")
            self.assertTrue(dlg.busy)
            self.assertFalse(dlg.play.isEnabled())
            self.assertFalse(VoxBridge._tts_idle(SimpleNamespace(tts_dialog=dlg)))
            release.set()
            end = time.monotonic() + 2
            while dlg.busy and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertTrue(dlg.play.isEnabled())
            self.assertFalse(dlg.busy)
            self.assertEqual(dlg.status.text(), "Gestoppt.")
            self.assertTrue(VoxBridge._tts_idle(SimpleNamespace(tts_dialog=dlg)))
        finally:
            release.set()
            dlg.close()

    def test_unverified_child_cleanup_blocks_new_playback_even_after_cancel(self):
        import threading
        dlg = CloudTTSDialog(provider_factory=Mock(), shortcut_notify=Mock())
        dlg._shortcut = True
        dlg._generation = dlg._worker_generation = dlg._output_generation = 7
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._stop()
        dlg._output_completed(7, OutputResult("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED", cleanup_failed=True))
        dlg._completed(7, "TTS_CANCELLED")
        self.assertTrue(dlg.busy)
        self.assertFalse(dlg.play.isEnabled())
        self.assertFalse(dlg.start_shortcut("Synthetic", "de", "sse"))
        self.assertEqual(dlg.status.text(), "AUDIO_OUTPUT_CHILD_CLEANUP_FAILED")
        dlg._provider_factory.assert_not_called()
        dlg.close()

    def test_stale_completion_does_not_unlock_new_request(self):
        dlg = CloudTTSDialog(provider_factory=Mock())
        dlg._generation = dlg._worker_generation = 10
        dlg._busy = True
        dlg.play.setEnabled(False)
        dlg._completed(9, "")
        self.assertTrue(dlg._busy)
        self.assertFalse(dlg.play.isEnabled())
        dlg._busy = False
        dlg.close()

if __name__ == "__main__":
    unittest.main()
