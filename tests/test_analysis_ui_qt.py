"""Qt analysis lifecycle tests; all providers are mocks."""
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from voxbridge.analysis_ui import AnalysisDialog


class AnalysisLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_local_gemma_selected_without_cloud_consent(self):
        from pathlib import Path
        from dataclasses import replace
        from voxbridge.config import Settings
        from voxbridge.local_llm import LocalModel
        model = LocalModel(Path("synthetic.gguf"))
        runtime = Mock()
        def stream(model, messages, callback, cancel):
            callback("synthetic")
            return "synthetic answer"
        runtime.stream.side_effect = stream
        dlg = AnalysisDialog(local_runtime=runtime, local_models=(model,),
                             preferences=replace(Settings(), analysis_provider="local"))
        try:
            dlg.text.setPlainText("Explain VLAN")
            dlg._on_analyze()
            end = time.monotonic() + 3
            while dlg._busy and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertEqual(dlg.output.toPlainText(), "synthetic answer")
            runtime.stream.assert_called_once()
            self.assertFalse(dlg.consent.isVisible())
        finally:
            dlg.close()

    def test_text_and_settings_invalidate_approval(self):
        dlg = AnalysisDialog()
        dlg.text.setPlainText("First")
        dlg.consent.setChecked(True)
        dlg.text.setPlainText("Second")
        self.assertFalse(dlg.consent.isChecked())
        dlg.consent.setChecked(True)
        dlg.effort.setCurrentIndex(1)
        self.assertFalse(dlg.consent.isChecked())
        dlg.close()

    def test_close_does_not_allow_overlapping_request(self):
        dlg = AnalysisDialog()
        entered, release = threading.Event(), threading.Event()
        provider = Mock(is_cloud=True)
        def generate(*args):
            entered.set()
            release.wait(3)
            return "Old response"
        provider.generate.side_effect = generate
        dlg._make_provider = Mock(return_value=provider)
        try:
            dlg.text.setPlainText("Explain VLAN")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertTrue(entered.wait(1))
            dlg.close()
            dlg.text.setPlainText("New")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertEqual(provider.generate.call_count, 1)
            self.assertFalse(dlg.run_button.isEnabled())
            release.set()
            until = time.monotonic() + 2
            while dlg._busy and time.monotonic() < until:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._busy)
            self.assertTrue(dlg.run_button.isEnabled())
            self.assertNotIn("Old response", dlg.output.toPlainText())
            provider.close.assert_called_once()
        finally:
            release.set()
            dlg.close()

    def test_thread_start_failure_unlocks(self):
        dlg = AnalysisDialog()
        dlg._make_provider = Mock(return_value=Mock(is_cloud=True))
        dlg.text.setPlainText("Explain VLAN")
        dlg.consent.setChecked(True)
        with patch("voxbridge.analysis_ui.threading.Thread.start",
                   side_effect=RuntimeError("mock")):
            dlg._on_analyze()
        self.assertFalse(dlg._busy)
        self.assertTrue(dlg.run_button.isEnabled())
        self.assertEqual(dlg.message.text(), "ANALYSIS_WORKER_START_FAILED")
        dlg.close()
