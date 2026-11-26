"""Offscreen normal tray exit: synthetic work and no physical devices or API."""
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from voxbridge.app import VoxBridge


class ShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": directory}))
        self.stack.enter_context(patch("voxbridge.app.HotkeyListener"))
        self.stack.enter_context(patch("voxbridge.selection_controller.HotkeyListener"))
        self.stack.enter_context(patch("voxbridge.app.Microphone"))
        self.stack.enter_context(patch.object(VoxBridge, "notify"))
        self.quit = self.stack.enter_context(patch.object(self.app, "quit"))
        self.bridge = VoxBridge(self.app)
        self.addCleanup(self.dispose)

    def dispose(self):
        bridge = self.bridge
        bridge.shutdown()
        bridge._exit_timer.stop()
        for widget in (bridge.control_center, bridge.analysis_dialog, bridge.tts_dialog,
                       bridge.vision_dialog, bridge.voice_dialog, bridge.live_captions_dialog,
                       bridge.caption_demo_overlay, bridge.status, bridge.tray):
            if widget is not None:
                widget.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_exit_waits_for_real_analysis_worker_and_keeps_qt_alive(self):
        bridge = self.bridge
        bridge.open_analysis()
        dlg = bridge.analysis_dialog
        entered, release = threading.Event(), threading.Event()
        provider = Mock(is_cloud=True)
        def generate(*args):
            entered.set()
            release.wait(4)
            return "synthetic"
        provider.generate.side_effect = generate
        dlg._make_provider = Mock(return_value=provider)
        try:
            dlg.text.setPlainText("Synthetic")
            dlg.consent.setChecked(True)
            dlg._on_analyze()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            bridge.request_exit()
            self.assertLess(time.monotonic()-start, 1)
            self.assertTrue(bridge._exit_timer.isActive())
            self.assertTrue(dlg._cancel_event.is_set())
            self.assertEqual(dlg.text.toPlainText(), "")
            self.quit.assert_not_called()
            self.assertEqual(len(bridge.tray.contextMenu().actions()), 1)
            release.set()
            end = time.monotonic()+3
            while not self.quit.called and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
            self.quit.assert_called_once()
            self.assertFalse(bridge._exit_timer.isActive())
            self.assertFalse(dlg._busy)
            provider.close.assert_called_once()
        finally:
            release.set()

    def test_exit_timeout_keeps_polling_and_never_forces_quit(self):
        bridge = self.bridge
        bridge._dictation_worker_generation = 1
        bridge.request_exit()
        bridge._exit_deadline = time.monotonic()-1
        bridge._poll_exit()
        self.quit.assert_not_called()
        self.assertTrue(bridge._exit_timer.isActive())
        self.assertIn("SHUTDOWN_CLEANUP_PENDING: STT", bridge.tray.toolTip())
        bridge._dictation_worker_generation = None
        bridge._poll_exit()
        self.quit.assert_called_once()

    def test_shared_voice_selector_and_cancelled_metadata_keep_exit_pending(self):
        bridge = self.bridge
        bridge.open_tts()
        bridge.tts_dialog._choose_voice()
        voice = bridge.voice_dialog
        self.assertIsNotNone(voice)
        bridge.tts_dialog._choose_voice()
        self.assertIs(bridge.voice_dialog, voice)
        self.assertIsNone(bridge.tts_dialog._voice_dialog)
        voice._worker_generation = voice._generation = 1
        voice._cancel = threading.Event()
        bridge.request_exit()
        self.assertTrue(voice._cancel.is_set())
        self.quit.assert_not_called()
        voice._loaded(1, [], "CARTESIA_VOICES_CANCELLED")
        bridge._poll_exit()
        self.quit.assert_called_once()

    def test_sticky_cleanup_error_is_not_a_live_worker(self):
        bridge = self.bridge
        bridge.mic._cleanup_failed = True
        bridge.open_tts()
        bridge.tts_dialog._output_cleanup_failed = True
        bridge.request_exit()
        self.quit.assert_called_once()

    def test_main_window_close_hides_and_tray_exit_quits(self):
        bridge = self.bridge
        bridge.open_control_center()
        dlg = bridge.control_center
        dlg.close()
        self.app.processEvents()
        self.assertFalse(dlg.isVisible())
        self.assertFalse(bridge._shutting_down)
        self.quit.assert_not_called()
        bridge.open_control_center()
        self.assertTrue(dlg.isVisible())
        bridge.request_exit()
        end = time.monotonic()+3
        while not self.quit.called and time.monotonic()<end:
            self.app.processEvents()
            time.sleep(.01)
        self.quit.assert_called_once()
        self.assertTrue(bridge._shutting_down)

    def test_shutdown_cancels_queued_tts_prewarm(self):
        bridge = self.bridge
        bridge.open_tts()
        dlg = bridge.tts_dialog
        dlg._prewarm_requested = True
        bridge.request_exit()
        self.assertFalse(dlg._prewarm_requested)
