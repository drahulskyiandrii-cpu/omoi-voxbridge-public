"""Reported failures: local signal quality, stoppable demo, safe error display."""
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent
from voxbridge.audio import measure_pcm, Microphone, MicrophoneError
from voxbridge.app import VoxBridge
from voxbridge.config import Settings
from voxbridge.session import Session,State
from voxbridge.captions_core import CaptionCoordinator
from voxbridge.captions_overlay import CaptionsOverlay,populate_synthetic_demo
from voxbridge.analysis_providers import ProviderFailure
from voxbridge.selection_controller import SelectionController
from voxbridge.selection import SelectionTarget


class SignalTests(unittest.TestCase):
    def test_silence_and_dc_rejected(self):
        for pcm in (bytes(32000),b"\x00\x01"*16000):
            self.assertEqual(measure_pcm(pcm).code,"MICROPHONE_SIGNAL_TOO_QUIET")
    def test_level_gate_uses_mini_threshold_and_sufficient_duration(self):
        q=measure_pcm(b"\x00\x01\x00\xff"*8000)
        self.assertEqual(q.code,"MIC_SIGNAL_OK")
        self.assertEqual(q.voice_ms,1000)
        self.assertEqual(q.seconds,1)
        self.assertEqual(q.peak,256)
    def test_clipped_or_gapped_audio_rejected(self):
        self.assertEqual(measure_pcm(b"\xff\x7f\x00\x80"*8000).code,"MICROPHONE_SIGNAL_CLIPPED")
        self.assertEqual(measure_pcm(b"\x00\x01\x00\xff"*8000,gaps=True).code,"MIC_INPUT_OVERFLOW")
    def test_silent_snapshot_not_sent_to_recognizer(self):
        mic=Microphone()
        mic._stream=Mock()
        mic._buffer.extend(bytes(32000))
        self.assertEqual(mic.snapshot(),b"")
        with self.assertRaisesRegex(MicrophoneError,"TOO_QUIET"):
            mic.stop()
        self.assertEqual(len(mic._buffer),0)
    def test_one_shot_raw_diagnostic_does_not_insert_even_invalid_result(self):
        session=Session()
        g=session.begin(123,124)
        session.advance(g,State.RECORDING,State.RECOGNIZING)
        a=NS(session=session,_dictation_worker_generation=g,_partial_turn=None,_shutting_down=False,
             _diagnose_next=True,settings=Settings(),_last_audio_info="MIC_SIGNAL_OK",status=Mock(),
             _refresh_menu=Mock(),notify=Mock(),_edit_identity=object())
        with patch("voxbridge.app.show_manual_fallback") as dialog,patch("voxbridge.app.QTimer.singleShot") as timer:
            VoxBridge.on_completed(a,g,"n"*30,"")
        dialog.assert_called_once()
        self.assertIn("STT_REPETITION_DETECTED",dialog.call_args.args[1])
        self.assertEqual(dialog.call_args.args[0],"n"*30)
        timer.assert_not_called()
        self.assertFalse(a._diagnose_next)
        self.assertEqual(session.snapshot().state,State.IDLE)


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
    def cleanup(self,widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()
    def test_demo_button_closes_and_clears(self):
        core=CaptionCoordinator()
        overlay=CaptionsOverlay(core)
        self.addCleanup(self.cleanup,overlay)
        populate_synthetic_demo(core)
        overlay.show()
        self.assertTrue(overlay.isVisible())
        overlay.close_button.click()
        self.assertFalse(overlay.isVisible())
        self.assertFalse(overlay._timer.isActive())
        self.assertEqual(core.snapshot().lines,())
    def test_demo_menu_toggle_and_stop_all(self):
        core=CaptionCoordinator()
        overlay=CaptionsOverlay(core)
        self.addCleanup(self.cleanup,overlay)
        a=NS(caption_demo_core=core,caption_demo_overlay=overlay,
             cancel=Mock(),analysis_dialog=None,vision_dialog=None,tts_dialog=None,
             live_captions_dialog=None,notify=Mock())
        VoxBridge.open_captions_demo(a)
        self.assertTrue(overlay.isVisible())
        VoxBridge.open_captions_demo(a)
        self.assertFalse(overlay.isVisible())
        VoxBridge.open_captions_demo(a)
        VoxBridge.stop_all(a)
        self.assertFalse(overlay.isVisible())
        self.assertEqual(core.snapshot().lines,())
    def test_selection_callback_error_not_disguised_as_focus_change(self):
        target=SelectionTarget(100,101,900,800)
        ctrl=SelectionController(can_start=lambda:True,on_preview=Mock(side_effect=ProviderFailure("TTS_DRAFT_EXISTS")),
             notify=Mock(),listener_factory=Mock(),target_factory=lambda _:target,available=lambda:True)
        ctrl.enable("Ctrl+Alt+S")
        try:
            ctrl._generation=ctrl._worker_generation=1
            ctrl._target=target
            ctrl._completed(1,"Synthetic","")
            ctrl._notify.assert_called_once_with("TTS_DRAFT_EXISTS")
        finally:
            ctrl.disable()

    def test_tts_transport_is_saved_without_starting_provider(self):
        from voxbridge.tts_ui import CloudTTSDialog
        factory=Mock()
        dlg=CloudTTSDialog(provider_factory=factory,transport_mode="bytes")
        self.addCleanup(self.cleanup,dlg)
        self.assertEqual(dlg.transport.currentData(),"bytes")
        self.assertEqual(dlg.transport.count(),2)
        with patch("voxbridge.tts_ui.load_settings",return_value=Settings(language="ru")),patch("voxbridge.tts_ui.save_settings") as save:
            dlg.transport.setCurrentIndex(1)
            dlg._save_transport(1)
        save.assert_called_once_with(Settings(language="ru",tts_transport="qwen_cf050"))
        factory.assert_not_called()

    def test_tts_first_audio_timeout_visible_and_cancelled(self):
        import time
        import threading
        from voxbridge.tts_ui import CloudTTSDialog
        dlg=CloudTTSDialog(provider_factory=Mock())
        self.addCleanup(self.cleanup,dlg)
        dlg._busy=True
        dlg._cancel=threading.Event()
        dlg._generation=dlg._worker_generation=1
        dlg._progress_at=time.monotonic()-26
        dlg._pump()
        self.assertTrue(dlg.isVisible())
        self.assertTrue(dlg._cancel.is_set())
        self.assertEqual(dlg.status.text(),"CARTESIA_FIRST_AUDIO_TIMEOUT")
        dlg._completed(1,"CARTESIA_CANCELLED")
        self.assertTrue(dlg.transport.isEnabled())
