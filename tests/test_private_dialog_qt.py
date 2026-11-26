"""Real Qt dialog dismiss routes with synthetic private content only."""
import os
import threading
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from voxbridge.analysis_ui import AnalysisDialog
from voxbridge.vision_ui import VisionDialog
from voxbridge.tts_ui import CloudTTSDialog
from voxbridge.live_captions_ui import LiveCaptionsDialog
from voxbridge.voice_ui import VoiceDialog
from voxbridge.cartesia_voices import OwnedVoice


class DismissTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def destroy(self, dialog):
        dialog.close()
        if isinstance(dialog, LiveCaptionsDialog):
            dialog._overlay.deleteLater()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make(self, cls, **kwargs):
        dialog = cls(**kwargs)
        self.addCleanup(self.destroy, dialog)
        return dialog

    def test_escape_clears_analysis(self):
        dlg = self.make(AnalysisDialog)
        dlg.text.setPlainText("Synthetic")
        dlg.output.setPlainText("Synthetic result")
        dlg.question.setText("Synthetic question")
        dlg.consent.setChecked(True)
        dlg.show()
        QTest.keyClick(dlg, Qt.Key.Key_Escape)
        self.assertFalse(dlg.isVisible())
        self.assertEqual(dlg.text.toPlainText(), "")
        self.assertEqual(dlg.output.toPlainText(), "")
        self.assertEqual(dlg.question.text(), "")
        self.assertFalse(dlg.consent.isChecked())

    def test_reject_fences_vision_delayed_capture(self):
        dlg = self.make(VisionDialog)
        dlg._png = b"synthetic"
        dlg._capture_pending = True
        dlg.question.setText("Synthetic question")
        dlg.reject()
        self.assertFalse(dlg._capture_pending)
        self.assertIsNone(dlg._png)
        self.assertEqual(dlg.question.text(), "")
        self.assertTrue(dlg._cancel_event.is_set())

    def test_stale_vision_result_cannot_unlock_new_worker(self):
        dlg = self.make(VisionDialog)
        dlg._generation = dlg._worker_generation = 7
        dlg._busy = True
        dlg._complete(6, "Synthetic stale", "")
        self.assertTrue(dlg._busy)
        self.assertEqual(dlg.output.toPlainText(), "")
        dlg._complete(7, "Synthetic final", "")
        self.assertFalse(dlg._busy)
        self.assertEqual(dlg.output.toPlainText(), "Synthetic final")

    def test_done_stops_tts_and_nested_voice_dialog(self):
        dlg = self.make(CloudTTSDialog)
        dlg._voice_dialog = Mock()
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(16))
        dlg.text.setPlainText("Synthetic")
        dlg.done(QDialog.DialogCode.Rejected)
        dlg._voice_dialog.reject.assert_called_once()
        dlg._voice_dialog = None
        self.assertTrue(dlg._cancel.is_set())
        self.assertTrue(dlg._incoming.empty())
        self.assertEqual(dlg.text.toPlainText(), "")

    def test_reject_clears_live_caption_state(self):
        dlg = self.make(LiveCaptionsDialog)
        generation = dlg._coordinator.begin(source_language="de", target_language=None)
        dlg._coordinator.final(generation, 0, "Synthetic")
        dlg._busy = True
        dlg.reject()
        self.assertTrue(dlg._stop.is_set())
        self.assertEqual(dlg._coordinator.snapshot().lines, ())
        dlg._busy = False

    def test_voice_close_blocks_overlap_until_discarded_worker_returns(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(*, cancel_event):
            entered.set()
            release.wait(2)
            return [OwnedVoice("Synthetic", "11111111-1111-4111-8111-111111111111", "ru")]
        fetcher = Mock(side_effect=fetch)
        dlg = self.make(VoiceDialog, fetcher=fetcher)
        try:
            dlg._refresh()
            self.assertTrue(entered.wait(1))
            cancel = dlg._cancel
            dlg.reject()
            self.assertTrue(cancel.is_set())
            self.assertFalse(dlg.refresh.isEnabled())
            dlg._refresh()
            fetcher.assert_called_once_with(cancel_event=cancel)
            release.set()
            end = time.monotonic()+2
            while dlg._worker_generation is not None and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertTrue(dlg.refresh.isEnabled())
            self.assertEqual(dlg.choices.count(), 0)
        finally:
            release.set()

    def test_voice_cleanup_failure_survives_dismissal_and_blocks_refresh(self):
        from voxbridge.cartesia_voices import VOICE_CLEANUP_FAILED
        dlg = self.make(VoiceDialog, fetcher=Mock())
        dlg._worker_generation = 1
        dlg._generation = 1
        dlg._cancel = threading.Event()
        dlg.reject()
        dlg._loaded(1, [], VOICE_CLEANUP_FAILED)
        self.assertEqual(dlg.status.text(), VOICE_CLEANUP_FAILED)
        self.assertFalse(dlg.refresh.isEnabled())
        dlg.reject()
        dlg._refresh()
        self.assertFalse(dlg.refresh.isEnabled())
        dlg._fetcher.assert_not_called()

    def test_region_close_restores_parent_without_capture(self):
        dlg = self.make(VisionDialog)
        dlg._start_selection()
        self.assertIsNotNone(dlg._overlay)
        self.assertFalse(dlg.isVisible())
        dlg._overlay.close()
        self.assertIsNone(dlg._overlay)
        self.assertTrue(dlg.isVisible())
        self.assertFalse(dlg._capture_pending)
