"""Feedback regressions. No physical capture, screen reading, keys or API."""
import os
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRect,QCoreApplication,QEvent
from PySide6.QtGui import QPixmap,QColor
from voxbridge.dictation_quality import inspect_transcript
from voxbridge.vision_ui import VisionDialog
from voxbridge.vision import OpenAIVision
from voxbridge.config import Settings
from voxbridge.device_discovery import discover_loopback_devices
from voxbridge.wasapi_capture import CaptureFailure
from voxbridge.app import VoxBridge
from voxbridge.session import Session,State


def fake_discovery(sender):
    try:
        sender.send_bytes(json.dumps({"devices":[["test","Fake speakers",True]]}).encode("ascii"))
    finally:
        sender.close()


def slow_discovery(sender):
    try:
        time.sleep(5)
    finally:
        sender.close()


class FeedbackTests(unittest.TestCase):
    def test_repeated_letters_rejected_without_rewriting(self):
        self.assertEqual(inspect_transcript("abc"+"n"*30+"ot"),"STT_REPETITION_DETECTED")
        self.assertEqual(inspect_transcript("\u043d"*30),"STT_REPETITION_DETECTED")
        self.assertEqual(inspect_transcript("Hallo, 11111111!"),"STT_TEXT_OK")

    def test_bad_transcript_does_not_schedule_insertion(self):
        session=Session()
        g=session.begin(12,34)
        session.advance(g,State.RECORDING,State.RECOGNIZING)
        app=NS(session=session,_dictation_worker_generation=g,_shutting_down=False,
               status=Mock(),notify=Mock(),_edit_identity=object())
        with patch("voxbridge.app.QTimer.singleShot") as timer:
            VoxBridge.on_completed(app,g,"a"*30,"")
        timer.assert_not_called()
        self.assertEqual(app._last_transcript_check,"STT_REPETITION_DETECTED")
        self.assertEqual(session.snapshot().state,State.IDLE)

    def test_failed_cleanup_never_offers_malformed_recovery_text(self):
        session = Session()
        g = session.begin(12,34)
        session.advance(g,State.RECORDING,State.RECOGNIZING)
        app = NS(session=session,_dictation_worker_generation=g,_shutting_down=False,
                 status=Mock(),notify=Mock(),_edit_identity=object())
        with patch("voxbridge.app.show_manual_fallback") as manual, patch("voxbridge.app.QTimer.singleShot") as timer:
            VoxBridge.on_completed(app,g,"<|ru|> malformed","PROVIDER_CLEANUP_FAILED")
            manual.assert_not_called()
            timer.assert_not_called()
        self.assertTrue(app._stt_cleanup_failed)
        self.assertEqual(session.snapshot().state,State.IDLE)

    def test_discovery_fresh_process_and_timeout(self):
        self.assertTrue(discover_loopback_devices(_worker=fake_discovery)[0].is_default)
        with self.assertRaisesRegex(CaptureFailure,"TIMEOUT"):
            discover_loopback_devices(.1,_worker=slow_discovery)

    def test_conflicting_new_hotkeys_rejected(self):
        with self.assertRaisesRegex(ValueError,"GLOBAL_HOTKEY_CONFLICT"):
            Settings(region_read_hotkey="Ctrl+Alt+Space").validate()


    def test_native_edit_replacement_sends_one_undoable_message(self):
        import ctypes
        from voxbridge.injection import replace_native_text
        user=Mock()
        user.IsWindowUnicode.return_value=True
        seen=[]
        def send(focus,msg,undo,address,flags,timeout,result):
            seen.append((focus,msg,undo,ctypes.wstring_at(address),timeout))
            return 1
        user.SendMessageTimeoutW.side_effect=send
        with patch("voxbridge.injection.ctypes.WinDLL",return_value=user), patch("voxbridge.injection._verify_target") as verify:
            count=replace_native_text(100,101,"Gr\u00fc\u00dfe, \u041f\u0440\u0438\u0432\u0435\u0442.")
        self.assertGreater(count,0)
        self.assertEqual(seen,[(101,0x00c2,1,"Gr\u00fc\u00dfe, \u041f\u0440\u0438\u0432\u0435\u0442.",1500)])
        verify.assert_called_once_with(user,100,101)
        user.SendInput.assert_not_called()

    def test_native_message_timeout_never_retries(self):
        from voxbridge.injection import replace_native_text
        from voxbridge.injection import InjectionError
        user=Mock()
        user.IsWindowUnicode.return_value=True
        user.SendMessageTimeoutW.return_value=0
        with patch("voxbridge.injection.ctypes.WinDLL",return_value=user),patch("voxbridge.injection._verify_target"):
            with self.assertRaisesRegex(InjectionError,"PARTIAL_INSERT_POSSIBLE"):
                replace_native_text(100,101,"Synthetic")
        user.SendMessageTimeoutW.assert_called_once()
        user.SendInput.assert_not_called()

    def test_ocr_does_not_translate_original_text(self):
        from test_vision import PNG_1PX
        client=Mock()
        client.responses.create.return_value=NS(status="completed",output_text="Hallo",usage=NS(input_tokens=20,output_tokens=10))
        provider=OpenAIVision(client=client)
        self.assertEqual(provider.read_png(PNG_1PX),"Hallo")
        kw=client.responses.create.call_args.kwargs
        self.assertIn("original language",kw["instructions"])
        self.assertIn("Do not translate",kw["instructions"])
        self.assertFalse(kw["store"])

    def test_realtime_progress_separates_connection_capture_and_speech(self):
        from test_realtime_transport import Factory
        from voxbridge.captions_core import CaptionCoordinator
        from voxbridge.realtime_transport import run_realtime_transcription
        import threading
        import numpy as np
        c=CaptionCoordinator()
        gen=c.begin(source_language="de",target_language=None)
        stages=[]
        run_realtime_transcription(c,gen,[np.full(4800,3000,dtype="<i2").tobytes()],
            approved=True,stop=threading.Event(),api_key="synthetic",socket_factory=Factory(),
            on_stage=stages.append)
        self.assertEqual(stages,["REALTIME_CONNECTING","REALTIME_CONFIGURING","WASAPI_WAITING_FOR_AUDIO",
                                 "WASAPI_AUDIO_RECEIVED","REALTIME_SPEECH_DETECTED","REALTIME_WAITING_FOR_FINAL"])


class RegionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def make(self,**kw):
        d=VisionDialog(**kw)
        def cleanup():
            d.close()
            d.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.addCleanup(cleanup)
        return d

    def pixmap(self):
        p=QPixmap(120,80)
        p.fill(QColor("blue"))
        return p

    def wait(self,d):
        deadline=time.monotonic()+2
        while d._busy and time.monotonic()<deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(d._busy)

    def test_region_read_uses_ocr_and_speech_callback(self):
        provider=Mock()
        provider.read_png.return_value="Hallo"
        spoken=Mock()
        d=self.make(provider_factory=lambda:provider,on_read=spoken,permission_checker=lambda _: True)
        d._accept_pixmap(self.pixmap())
        d._read_preview()
        self.wait(d)
        provider.read_png.assert_called_once()
        provider.analyze_png.assert_not_called()
        spoken.assert_called_once_with("Hallo")

    def test_region_without_permission_never_sends(self):
        factory=Mock()
        d=self.make(provider_factory=factory,on_read=Mock())
        d._accept_pixmap(self.pixmap())
        d._read_preview()
        factory.assert_not_called()

    def test_shortcut_opens_overlay_for_each_screen(self):
        d=self.make(provider_factory=Mock())
        screen=Mock()
        screen.geometry.return_value=QRect(-800,0,800,600)
        second=Mock()
        second.geometry.return_value=QRect(0,0,800,600)
        with patch("voxbridge.vision_ui.QApplication.screens",return_value=[screen,second]):
            self.assertTrue(d.start_shortcut("read"))
        self.assertEqual(len(d._overlays),2)
        self.assertEqual(d._overlays[0].geometry().x(),-800)
        d._aborted()
        self.assertEqual(d._overlays,[])
        self.assertIsNone(d._overlay)
        self.assertFalse(d.isVisible())
