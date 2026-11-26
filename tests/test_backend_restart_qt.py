"""Regression for saved local engine choice across app restarts."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from voxbridge.app import VoxBridge
from voxbridge.config import Settings, load_settings, save_settings
from voxbridge.selection import SelectionTarget
from voxbridge.session import State


class BackendRestartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def destroy(self, bridge):
        bridge.shutdown()
        bridge.status.deleteLater()
        bridge.tray.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_saved_cpu_engine_works_after_restart_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
             patch("voxbridge.app.HotkeyListener"), \
             patch("voxbridge.selection_controller.HotkeyListener"), \
             patch("voxbridge.app.Microphone") as mic, \
             patch.object(VoxBridge, "notify"), \
             patch("voxbridge.app.QMessageBox.question") as prompt, \
             patch("voxbridge.app.ResidentMiniSTT") as resident, \
             patch("voxbridge.app.MiniOneShotSTT") as batch, \
             patch("voxbridge.app.OpenAIFileSTT") as cloud:
            first = VoxBridge(self.app)
            try:
                first.set_backend("mini_cpu")
                self.assertEqual(load_settings().stt_backend, "mini_cpu")
                self.assertTrue(first._local_stt_armed)
                prompt.assert_not_called()
            finally:
                self.destroy(first)
            second = VoxBridge(self.app)
            try:
                self.assertEqual(second.settings.stt_backend, "mini_cpu")
                self.assertTrue(second._local_stt_armed)
                mic.return_value.start.assert_not_called()
                resident.assert_not_called()
                batch.assert_not_called()
                cloud.assert_not_called()
                with patch("voxbridge.app.capture_target", return_value=SelectionTarget(100,101,900,901)), \
                     patch("voxbridge.app.threading.Thread.start"):
                    second.on_hotkey(100)
                self.assertEqual(second.session.snapshot().state, State.VERIFYING_TARGET)
                prompt.assert_not_called()
                resident.assert_not_called()
                cloud.assert_not_called()
            finally:
                self.destroy(second)

    def test_saved_cuda_and_shared_do_not_load_at_startup(self):
        for mode in ("mini_worker","shared"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {"LOCALAPPDATA":tmp}), \
                 patch("voxbridge.app.HotkeyListener"), \
                 patch("voxbridge.selection_controller.HotkeyListener"), \
                 patch("voxbridge.app.Microphone") as mic, \
                 patch.object(VoxBridge,"notify"), \
                 patch("voxbridge.app.ResidentMiniSTT") as resident, \
                 patch("voxbridge.app.MiniOneShotSTT") as batch, \
                 patch("voxbridge.app.SpeechCoreHTTP") as shared:
                save_settings(Settings(stt_backend=mode,microphone_id="a"*64,microphone_label="Synthetic USB"))
                bridge=VoxBridge(self.app)
                try:
                    self.assertTrue(bridge._local_stt_armed)
                    mic.assert_called_once_with(25,microphone_id="a"*64)
                    mic.return_value.start.assert_not_called()
                    resident.assert_not_called()
                    batch.assert_not_called()
                    shared.assert_not_called()
                finally:
                    self.destroy(bridge)
