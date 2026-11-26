"""Streaming text preview and cancellation; all data synthetic."""
import os
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from voxbridge.analysis import AnalysisRequest
from voxbridge.analysis_providers import ProviderFailure, execute_analysis
from voxbridge.analysis_stream import AnalysisPreview
from voxbridge.analysis_ui import AnalysisDialog
from voxbridge.cloud_providers import OpenAIResponses


class PreviewTests(unittest.TestCase):
    def test_cumulative_bound_and_cancel(self):
        stop=threading.Event()
        preview=AnalysisPreview(stop,max_chars=8)
        preview.push("1234")
        self.assertEqual(preview.drain(),"1234")
        preview.push("5678")
        with self.assertRaisesRegex(ProviderFailure,"TOO_LARGE"):
            preview.push("9")
        stop.set()
        self.assertEqual(preview.drain(),"")
        with self.assertRaisesRegex(ProviderFailure,"CANCELLED"):
            preview.push("x")

    def test_stream_permission_required_and_no_fallback(self):
        provider=Mock(is_cloud=True)
        cb=Mock()
        with self.assertRaisesRegex(ProviderFailure,"CLOUD_APPROVAL_REQUIRED"):
            execute_analysis(AnalysisRequest("Synthetic"),provider,on_delta=cb)
        provider.generate_stream.assert_not_called()
        provider.generate.assert_not_called()
        provider.generate_stream.side_effect=ProviderFailure("STREAM_FAILED")
        with self.assertRaisesRegex(ProviderFailure,"STREAM_FAILED"):
            execute_analysis(AnalysisRequest("Synthetic"),provider,on_delta=cb,cloud_approved=True)
        provider.generate.assert_not_called()


class Stream:
    def __init__(self, release, first, *, fail=False):
        self.release,self.first,self.fail=release,first,fail
        self.closed=False
    def __enter__(self):
        return self
    def __exit__(self,*_):
        self.closed=True
    def __iter__(self):
        yield NS(type="response.output_text.delta",delta="Partial")
        self.first.set()
        self.release.wait(2)
        if self.fail:
            yield NS(type="response.failed")
        else:
            yield NS(type="response.output_text.delta",delta=" preview")
    def get_final_response(self):
        return NS(status="completed", output_text="Canonical final",
                  usage=NS(input_tokens=1,output_tokens=2))


class StreamingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def run_case(self, *, cancel=False, fail=False):
        release,first=threading.Event(),threading.Event()
        stream=Stream(release,first,fail=fail)
        client=Mock()
        client.responses.stream.return_value=stream
        provider=OpenAIResponses(client=client)
        speak=Mock()
        dlg=AnalysisDialog(on_speak=speak)
        dlg._make_provider=Mock(return_value=provider)
        try:
            dlg.text.setPlainText("Synthetic")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertTrue(first.wait(1))
            dlg._drain_preview()
            self.assertEqual(dlg.output.toPlainText(),"Partial")
            self.assertFalse(dlg.copy_button.isEnabled())
            self.assertFalse(dlg.speak_answer.isEnabled())
            dlg._speak_answer()
            speak.assert_not_called()
            if cancel:
                dlg.cancel_button.click()
                self.assertEqual(dlg.output.toPlainText(),"")
                self.assertFalse(dlg.run_button.isEnabled())
            release.set()
            end=time.monotonic()+2
            while dlg._busy and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._busy)
            self.assertFalse(dlg._preview_timer.isActive())
            self.assertTrue(stream.closed)
            self.assertTrue(dlg.run_button.isEnabled())
            if cancel or fail:
                self.assertEqual(dlg.output.toPlainText(),"")
                self.assertFalse(dlg.copy_button.isEnabled())
            else:
                self.assertEqual(dlg.output.toPlainText(),"Canonical final")
                self.assertTrue(dlg.copy_button.isEnabled())
                self.assertTrue(dlg.speak_answer.isEnabled())
            client.responses.create.assert_not_called()
            client.responses.stream.assert_called_once()
        finally:
            release.set()
            dlg.close()
            dlg.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_partial_preview_and_canonical_final(self):
        self.run_case()

    def test_cancel_discards_preview(self):
        self.run_case(cancel=True)

    def test_failed_stream_discards_preview(self):
        self.run_case(fail=True)
