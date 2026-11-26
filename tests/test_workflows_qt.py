"""Offline cross-dialog workflow tests. Never invokes real audio or cloud."""
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
from voxbridge.analysis_ui import AnalysisDialog
from voxbridge.vision_ui import VisionDialog
from voxbridge.tts_ui import CloudTTSDialog
from voxbridge.analysis_providers import ProviderFailure
from voxbridge.app import VoxBridge


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def destroy(self, dialog):
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_original_and_selected_answer_transfer_only_on_click(self):
        speech = Mock()
        dlg = AnalysisDialog(on_speak=speech)
        dlg.text.setPlainText("Ein VLAN")
        self.assertTrue(dlg.speak_source.isEnabled())
        speech.assert_not_called()
        dlg.speak_source.click()
        speech.assert_called_once_with("Ein VLAN", None)
        speech.reset_mock()
        dlg._answer_language = "de"
        dlg.output.setPlainText("First\nSecond")
        cursor = dlg.output.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(12, QTextCursor.MoveMode.KeepAnchor)
        dlg.output.setTextCursor(cursor)
        dlg.speak_answer.click()
        speech.assert_called_once_with("First\nSecond", "de")
        del cursor
        self.destroy(dlg)

    def test_tts_preview_preserves_draft_and_starts_no_request(self):
        provider = Mock(side_effect=AssertionError("no provider"))
        dlg = CloudTTSDialog(provider_factory=provider)
        dlg.consent.setChecked(True)
        dlg.prepare_text("Synthetic", "de")
        self.assertEqual(dlg.text.toPlainText(), "Synthetic")
        self.assertEqual(dlg.language.currentData(), "auto")
        self.assertFalse(dlg.consent.isChecked())
        with self.assertRaisesRegex(ProviderFailure, "TTS_DRAFT_EXISTS"):
            dlg.prepare_text("Different", "ru")
        self.assertEqual(dlg.text.toPlainText(), "Synthetic")
        self.assertEqual(dlg.language.currentData(), "auto")
        dlg._busy = True
        with self.assertRaisesRegex(ProviderFailure, "TTS_BUSY"):
            dlg.prepare_text("Synthetic", "de")
        dlg._busy = False
        provider.assert_not_called()
        self.destroy(dlg)

    def test_vision_answer_only_not_pixels(self):
        speech = Mock()
        factory = Mock(side_effect=AssertionError("no provider"))
        dlg = VisionDialog(provider_factory=factory, on_speak=speech)
        dlg._png = b"synthetic image"
        dlg.output.setPlainText("Synthetic answer")
        dlg.speak_answer.click()
        speech.assert_called_once_with("Synthetic answer", "ru")
        factory.assert_not_called()
        self.destroy(dlg)

    def test_cancelled_analysis_keeps_worker_locked_until_exit(self):
        dlg = AnalysisDialog()
        entered, release = threading.Event(), threading.Event()
        provider = Mock(is_cloud=True)
        def generate(*_):
            entered.set()
            release.wait(3)
            return "Old result"
        provider.generate.side_effect = generate
        dlg._make_provider = Mock(return_value=provider)
        try:
            dlg.text.setPlainText("Synthetic question")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertTrue(entered.wait(1))
            dlg.cancel_button.click()
            self.assertTrue(dlg._cancel_event.is_set())
            self.assertFalse(dlg.run_button.isEnabled())
            release.set()
            deadline = time.monotonic() + 2
            while dlg._busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._busy)
            self.assertTrue(dlg.run_button.isEnabled())
            self.assertEqual(dlg.output.toPlainText(), "")
        finally:
            release.set()
            self.destroy(dlg)

    def test_analysis_cleanup_failure_after_cancel_blocks_retry(self):
        dlg = AnalysisDialog()
        entered, release = threading.Event(), threading.Event()
        provider = Mock(is_cloud=True)
        def generate(*_):
            entered.set()
            release.wait(3)
            return "Synthetic"
        provider.generate.side_effect = generate
        provider.close.side_effect = OSError("private")
        dlg._make_provider = Mock(return_value=provider)
        try:
            dlg.text.setPlainText("Synthetic question")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertTrue(entered.wait(2))
            dlg.cancel_button.click()
            release.set()
            deadline = time.monotonic() + 3
            while dlg._busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._busy)
            self.assertFalse(dlg.run_button.isEnabled())
            self.assertEqual(dlg.message.text(), "PROVIDER_CLEANUP_FAILED")
            self.assertEqual(dlg.output.toPlainText(), "")
            dlg.reject()
            dlg._on_analyze()
            dlg._make_provider.assert_called_once()
        finally:
            release.set()
            self.destroy(dlg)

    def test_vision_cleanup_failure_prevents_read_callback(self):
        provider = Mock()
        provider.read_png.return_value = "Synthetic"
        provider.close.side_effect = OSError("private")
        speak = Mock()
        dlg = VisionDialog(provider_factory=Mock(return_value=provider), on_read=speak)
        try:
            dlg._png = b"synthetic"
            dlg._region_mode = "read"
            dlg.consent.setChecked(True)
            dlg._analyze()
            deadline = time.monotonic() + 3
            while dlg._busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._busy)
            self.assertFalse(dlg.analyze.isEnabled())
            self.assertEqual(dlg.message.text(), "PROVIDER_CLEANUP_FAILED")
            speak.assert_not_called()
            dlg._analyze()
            dlg._provider_factory.assert_called_once()
        finally:
            self.destroy(dlg)

    def test_tts_cleanup_failure_after_stale_completion_blocks_play(self):
        factory = Mock()
        dlg = CloudTTSDialog(provider_factory=factory)
        try:
            dlg._generation = 2
            dlg._worker_generation = 1
            dlg._busy = True
            dlg._completed(1, "PROVIDER_CLEANUP_FAILED")
            self.assertFalse(dlg.busy)
            self.assertFalse(dlg.play.isEnabled())
            self.assertEqual(dlg.status.text(), "PROVIDER_CLEANUP_FAILED")
            self.assertFalse(dlg.start_shortcut("Synthetic", "de", "sse"))
            factory.assert_not_called()
        finally:
            self.destroy(dlg)

    def test_tray_bridge_opens_preview_but_does_not_play(self):
        dlg = Mock()
        app = NS(_shutting_down=False, tts_dialog=dlg, open_tts=Mock())
        VoxBridge._prepare_speech(app, "Synthetic", "en")
        dlg.prepare_text.assert_called_once_with("Synthetic", "en")
        app.open_tts.assert_called_once()
        dlg._play.assert_not_called()

    def test_answer_language_bound_to_request_not_current_selector(self):
        dlg = AnalysisDialog(on_speak=Mock())
        dlg._generation = dlg._worker_generation = 1
        dlg._busy = True
        dlg._request_language = "de"
        dlg.language.setCurrentIndex(dlg.language.findData("ru"))
        dlg._on_complete(1, "Synthetic", "")
        dlg.speak_answer.click()
        dlg._on_speak.assert_called_once_with("Synthetic", "de")
        self.destroy(dlg)
