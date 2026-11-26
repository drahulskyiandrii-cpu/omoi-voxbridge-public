"""Qt lifecycle tests with blocked synthetic native operations, no real devices."""
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from voxbridge.microphone_lifecycle import MicrophoneLifecycle
from voxbridge.microphone_devices import MicrophoneError
from voxbridge.audio import Microphone, pcm_to_wav
from voxbridge.app import VoxBridge
from voxbridge.long_dictation import LongDictationTurn
from voxbridge.session import Session, State


class MicrophoneLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def controller(self, recorder):
        mic = MicrophoneLifecycle(recorder)
        self.addCleanup(mic.deleteLater)
        return mic

    def test_start_and_stop_do_not_block_qt(self):
        main_thread = threading.get_ident()
        entered = threading.Event()
        release_start = threading.Event()
        release_stop = threading.Event()
        calls = []
        recorder = Mock()
        def start(*, cancelled):
            calls.append(threading.get_ident())
            entered.set()
            release_start.wait(3)
        def stop():
            calls.append(threading.get_ident())
            release_stop.wait(3)
            return b"synthetic-wav"
        recorder.start.side_effect = start
        recorder.stop.side_effect = stop
        mic = self.controller(recorder)
        ready, stopped = [], []
        mic.ready.connect(lambda *args: ready.append(args))
        mic.stopped.connect(lambda *args: stopped.append(args))
        try:
            mic.start(7)
            self.assertTrue(entered.wait(1))
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.wait_for(lambda: tick)
            self.assertTrue(mic.busy)
            self.assertEqual(ready, [])
            release_start.set()
            self.wait_for(lambda: ready)
            self.assertEqual(ready, [(7,"")])
            mic.finish()
            tick.clear()
            QTimer.singleShot(0, lambda: tick.append(True))
            self.wait_for(lambda: tick)
            self.assertTrue(mic.busy)
            self.assertEqual(stopped, [])
            release_stop.set()
            self.wait_for(lambda: stopped)
            self.assertEqual(stopped, [(7,b"synthetic-wav","")])
            self.assertFalse(mic.busy)
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0],calls[1])
            self.assertNotEqual(calls[0],main_thread)
        finally:
            release_start.set()
            release_stop.set()
            mic.cancel()
            self.wait_for(lambda: not mic.busy)

    def test_cancel_during_start_blocks_reopen_and_suppresses_late_ready(self):
        recorder = Mock()
        entered, release = threading.Event(), threading.Event()
        events = []
        def start(*, cancelled):
            entered.set()
            release.wait(3)
            self.assertTrue(cancelled.is_set())
        recorder.start.side_effect = start
        mic = self.controller(recorder)
        mic.ready.connect(lambda *args: events.append(args))
        try:
            mic.start(1)
            self.assertTrue(entered.wait(1))
            mic.cancel()
            with self.assertRaisesRegex(MicrophoneError,"MIC_CLEANUP_PENDING"):
                mic.start(2)
            release.set()
            self.wait_for(lambda: not mic.busy)
            recorder.cancel.assert_called_once()
            recorder.stop.assert_not_called()
            self.assertEqual(events,[])
        finally:
            release.set()
            mic.cancel()
            self.wait_for(lambda: not mic.busy)

    def test_cancel_during_stop_drops_audio_and_waits_for_cleanup(self):
        recorder = Mock()
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            release.wait(3)
            return b"private-synthetic"
        recorder.stop.side_effect = stop
        mic = self.controller(recorder)
        ready, result = [], []
        mic.ready.connect(lambda *a: ready.append(a))
        mic.stopped.connect(lambda *a: result.append(a))
        try:
            mic.start(1)
            self.wait_for(lambda: ready)
            mic.finish()
            self.assertTrue(entered.wait(1))
            mic.cancel()
            self.assertTrue(mic.busy)
            release.set()
            self.wait_for(lambda: not mic.busy)
            self.assertEqual(result,[])
            recorder.cancel.assert_called_once()
        finally:
            release.set()
            mic.cancel()
            self.wait_for(lambda: not mic.busy)

    def test_cleanup_failure_blocks_new_recording(self):
        recorder = Mock()
        recorder.start.side_effect = RuntimeError("driver")
        recorder.cancel.side_effect = RuntimeError("driver close")
        mic = self.controller(recorder)
        result=[]
        mic.ready.connect(lambda *a: result.append(a))
        mic.start(1)
        self.wait_for(lambda: result)
        self.assertEqual(result,[(1,"MIC_CLEANUP_FAILED")])
        self.assertTrue(mic.busy)
        self.assertTrue(mic.cleanup_failed)
        with self.assertRaisesRegex(MicrophoneError,"MIC_CLEANUP_PENDING"):
            mic.start(2)

    def test_stale_ready_and_stop_cannot_restart_or_submit(self):
        session=Session()
        g=session.begin(1,2)
        session.advance(g,State.RECORDING,State.STARTING_MIC)
        session.cancel()
        app=NS(session=session,_shutting_down=False,mic_deadline=Mock(),
               recorder=Mock(),executor=Mock(),deadline=Mock(),status=Mock())
        VoxBridge._microphone_ready(app,g,"")
        VoxBridge._microphone_stopped(app,g,b"synthetic","")
        app.deadline.start.assert_not_called()
        app.executor.submit.assert_not_called()
        self.assertEqual(session.snapshot().state,State.IDLE)

    def test_timeout_recovers_only_confirmed_long_prefix(self):
        session=Session()
        g=session.begin(1,2)
        session.advance(g,State.RECORDING,State.STOPPING_MIC)
        backend=Mock()
        turn=LongDictationTurn(backend,Mock(),"de",Mock())
        turn._confirmed="first confirmed segment"
        app=NS(session=session,mic=Mock(),mic_deadline=Mock(),deadline=Mock(),
               _partial_turn=turn,recorder=Mock(),status=Mock(),notify=Mock())
        app._microphone_failed=lambda *args: VoxBridge._microphone_failed(app,*args)
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            VoxBridge._microphone_timeout(app)
        fallback.assert_called_once_with("first confirmed segment","INCOMPLETE_DICTATION: MIC_DRIVER_TIMEOUT")
        self.assertEqual(session.snapshot().state,State.IDLE)
        self.assertEqual(turn.confirmed_text(),"")
        backend.cancel.assert_called_once()
        app.mic.cancel.assert_called_once()

    def test_failed_native_close_retains_handle_for_retry(self):
        mic=Microphone()
        stream=Mock()
        mic._stream=stream
        stream.close.side_effect=[OSError("driver"),None]
        with self.assertRaises(OSError):
            mic.stop()
        self.assertIs(mic._stream,stream)
        mic.cancel()
        self.assertIsNone(mic._stream)
        self.assertEqual(stream.close.call_count,2)

    def test_cancelled_callback_does_not_capture_more_pcm(self):
        mic=Microphone()
        stop=threading.Event()
        sd=Mock()
        with patch.dict("sys.modules",{"sounddevice":sd}), patch(
                "voxbridge.audio.resolve_input",return_value=1):
            mic.start(cancelled=stop)
            callback=sd.RawInputStream.call_args.kwargs["callback"]
            callback(bytes(2048),1024,None,None)
            self.assertEqual(len(mic._buffer),2048)
            stop.set()
            callback(bytes(2048),1024,None,None)
            self.assertEqual(len(mic._buffer),2048)
            mic.cancel()
        self.assertEqual(len(mic._buffer),0)
        self.assertIsNone(mic._stream)
