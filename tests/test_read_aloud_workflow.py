"""Synthetic browser selection and shortcut speech, no native content or API."""
import os
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QAudio
from voxbridge.selection import read_document_selection,SelectionTarget,SelectionFailure
from voxbridge.selection_controller import SelectionController
from voxbridge.tts_stream import split_read_text,validate_tts_text
from voxbridge.tts_ui import CloudTTSDialog
from voxbridge.config import Settings
from voxbridge.analysis_providers import ProviderFailure

TARGET=SelectionTarget(100,101,900,800)
VOICE=Settings(cartesia_voice_id="11111111-1111-4111-8111-111111111111")

class DocumentTests(unittest.TestCase):
    def setUp(self):
        def node(kind,pid=901,hwnd=0,focus=False):
            x=Mock()
            x.CurrentControlType=kind
            x.CurrentProcessId=pid
            x.CurrentNativeWindowHandle=hwnd
            x.CurrentHasKeyboardFocus=focus
            x.GetCurrentPropertyValueEx.return_value=False
            return x
        self.root=node(50033,900,100)
        self.doc=node(50030)
        self.focus=node(50020,focus=True)
        self.link=node(50005)
        self.leaf=node(50020)
        self.parent={self.focus:self.doc,self.doc:self.root,self.link:self.doc,self.leaf:self.link}
        self.auto=Mock()
        self.auto.GetFocusedElement.return_value=self.focus
        self.auto.CompareElements.side_effect=lambda a,b:a is b
        w=self.auto.RawViewWalker
        w.GetParentElement.side_effect=lambda x:self.parent.get(x)
        w.GetFirstChildElement.side_effect=lambda x:self.leaf if x is self.link else None
        w.GetNextSiblingElement.return_value=None
        self.pattern=self.doc.GetCurrentPattern.return_value.QueryInterface.return_value
        self.pattern.GetSelection.return_value.Length=1
        self.range=self.pattern.GetSelection.return_value.GetElement.return_value
        self.range.CompareEndpoints.return_value=-1
        self.range.GetEnclosingElement.return_value=self.doc
        self.range.GetChildren.return_value.Length=1
        self.range.GetChildren.return_value.GetElement.return_value=self.link
        self.range.GetText.return_value="Browser selection with a link"
        self.range.Compare.return_value=True
        self.normal=Mock()
        self.verify=Mock(return_value=TARGET)
    def read(self):
        return read_document_selection(self.auto,TARGET,verify_target=self.verify,text_pattern_interface=object(),require_normal=self.normal)
    def test_document_ancestor_and_cross_process_link(self):
        self.assertEqual(self.read(),"Browser selection with a link")
        self.range.GetText.assert_called_once_with(12001)
        self.pattern.DocumentRange.assert_not_called()
        self.leaf.GetCurrentPattern.assert_not_called()
        self.normal.assert_any_call(901)
        self.normal.assert_any_call(900)
    def test_document_above_native_focus_leaf_is_reachable(self):
        self.focus.CurrentNativeWindowHandle=TARGET.focus
        self.focus.CurrentProcessId=TARGET.process_id
        self.assertEqual(self.read(),"Browser selection with a link")

    def test_text_pattern2_fallback_keeps_same_selection_guards(self):
        unknown=self.doc.GetCurrentPattern.return_value
        self.doc.GetCurrentPattern.side_effect=lambda pattern_id: unknown if pattern_id==10024 else None
        self.assertEqual(self.read(),"Browser selection with a link")
        self.leaf.GetCurrentPropertyValueEx.return_value=True
        self.range.GetText.reset_mock()
        with self.assertRaisesRegex(SelectionFailure,"PROTECTED"):
            self.read()
        self.range.GetText.assert_not_called()

    def test_password_or_edit_child_denied_before_read(self):
        self.leaf.GetCurrentPropertyValueEx.return_value=True
        with self.assertRaisesRegex(SelectionFailure,"PROTECTED"):
            self.read()
        self.range.GetText.assert_not_called()
        self.leaf.GetCurrentPropertyValueEx.return_value=False
        self.leaf.CurrentControlType=50004
        with self.assertRaisesRegex(SelectionFailure,"EMBEDDED"):
            self.read()
        self.range.GetText.assert_not_called()
    def test_unlinked_document_denied(self):
        self.parent[self.doc]=None
        with self.assertRaisesRegex(SelectionFailure,"PROTECTED"):
            self.read()
        self.range.GetText.assert_not_called()
    def test_caret_not_read(self):
        self.range.CompareEndpoints.return_value=0
        with self.assertRaisesRegex(SelectionFailure,"EMPTY"):
            self.read()
        self.range.GetText.assert_not_called()

class ChunkTests(unittest.TestCase):
    def test_long_text_chunks_preserve_full_content(self):
        text=("Synthetic sentence 123. "*250)+"Last ending."
        parts=split_read_text(text,"en")
        self.assertGreater(len(parts),2)
        self.assertEqual("".join(parts),text)
        for part in parts:
            validate_tts_text(part,"en")
    def test_oversize_rejected_before_provider(self):
        with self.assertRaisesRegex(ProviderFailure,"12000"):
            split_read_text("x"*12001,"en")

class ShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
    def dialog(self,**kwargs):
        dlg=CloudTTSDialog(provider_factory=Mock(),**kwargs)
        self.addCleanup(self.destroy,dlg)
        return dlg
    def destroy(self,dlg):
        dlg.close()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()
    def mock_audio(self,dlg):
        from voxbridge.output_isolation import OutputResult
        output=bytearray()
        def runner(incoming, finished, stop, recover):
            self.assertTrue(finished.is_set())
            while not incoming.empty():
                output.extend(incoming.get_nowait())
            return OutputResult()
        dlg._output_runner=Mock(side_effect=runner)
        return output

    def test_shortcut_reads_all_parts_and_preserves_manual_draft_hidden(self):
        notify=Mock()
        dlg=self.dialog(permission_checker=lambda _:True,shortcut_notify=notify)
        dlg.text.setPlainText("manual draft")
        out=self.mock_audio(dlg)
        parts=[]
        text=("Synthetic sentence. "*140)+"End."
        def stream(provider,part,lang,emit,cancel):
            parts.append(part)
            emit(b"\x01\x00")
        def inline_thread(*,target,**kwargs):
            return NS(start=target)
        with patch("voxbridge.tts_ui.load_settings",return_value=VOICE),patch("voxbridge.tts_ui.stream_to_callback",side_effect=stream),patch("voxbridge.tts_ui.threading.Thread",side_effect=inline_thread):
            self.assertTrue(dlg.start_shortcut(text,"en","sse"))
        self.assertEqual("".join(parts),text)
        self.assertEqual(out,b"\x01\x00"*len(parts))
        self.assertFalse(dlg.busy)
        self.assertFalse(dlg.isVisible())
        self.assertEqual(dlg.text.toPlainText(),"manual draft")
        self.assertFalse(dlg._remote_done)
        dlg._output_runner.assert_called_once()
        notify.assert_not_called()
    def test_shortcut_permission_or_voice_error_never_opens_menu(self):
        notify=Mock()
        dlg=self.dialog(permission_checker=lambda _:False,shortcut_notify=notify)
        self.assertFalse(dlg.start_shortcut("Text","en","sse"))
        notify.assert_called_with("TTS_PERMISSION_REQUIRED")
        self.assertFalse(dlg.isVisible())
        dlg._permission_checker=lambda _:True
        with patch("voxbridge.tts_ui.load_settings",return_value=Settings()):
            self.assertFalse(dlg.start_shortcut("Text","en","sse"))
        notify.assert_called_with("CARTESIA_VOICE_NOT_SELECTED")
        self.assertFalse(dlg.isVisible())
        dlg._provider_factory.assert_not_called()
    def test_remote_error_drains_received_audio_and_notifies_without_window(self):
        notify=Mock()
        dlg=self.dialog(permission_checker=lambda _:True,shortcut_notify=notify)
        dlg._shortcut=True
        dlg._generation=dlg._worker_generation=4
        dlg._busy=True
        dlg._cancel=threading.Event()
        out=self.mock_audio(dlg)
        dlg._incoming.put(b"\x01\x00"*16)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=lambda *,target,**kwargs:NS(start=target)):
            dlg._completed(4,"CARTESIA_STREAM_INCOMPLETE")
        self.assertEqual(len(out),32)
        self.assertFalse(dlg.busy)
        dlg._output_runner.assert_called_once()
        notify.assert_called_once_with("CARTESIA_STREAM_INCOMPLETE")
        self.assertFalse(dlg.isVisible())
    def test_repeat_shortcut_stops_before_capturing_target(self):
        target=Mock()
        controller=SelectionController(can_start=lambda:False,on_preview=Mock(),notify=Mock(),listener_factory=Mock(),target_factory=target,available=lambda:True,on_repeat=lambda:True)
        controller.enable("Ctrl+Alt+S")
        try:
            controller.on_hotkey(100)
            target.assert_not_called()
        finally:
            controller.disable()
            controller.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
