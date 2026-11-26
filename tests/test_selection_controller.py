"""Selection GUI lifecycle: only fake hotkey, fake target and text reader."""
import os
import time
import threading
import unittest
from unittest.mock import Mock, patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from voxbridge.selection_controller import SelectionController
from voxbridge.selection import SelectionTarget, SelectionFailure
from voxbridge.app import VoxBridge
from types import SimpleNamespace as NS


TARGET=SelectionTarget(100,101,900,800)


class SelectionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setup_controller(self, reader=None, available=True):
        self.listener=Mock()
        self.listener.running=False
        self.factory=Mock(return_value=self.listener)
        self.target=Mock(return_value=TARGET)
        self.reader=reader or Mock(return_value="Synthetic text")
        self.preview=Mock()
        self.notify=Mock()
        self.allowed=Mock(return_value=True)
        self.ctrl=SelectionController(can_start=self.allowed, on_preview=self.preview,
                                      notify=self.notify, listener_factory=self.factory,
                                      target_factory=self.target, reader=self.reader,
                                      available=lambda:available)
        self.addCleanup(self.ctrl.disable)

    def wait_idle(self):
        deadline=time.monotonic()+2
        while self.ctrl.busy and time.monotonic()<deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(self.ctrl.busy)

    def test_default_off_then_manual_hotkey_preview(self):
        self.setup_controller()
        self.ctrl.on_hotkey(100)
        self.target.assert_not_called()
        self.ctrl.enable("Ctrl+Alt+A")
        self.reader.assert_not_called()
        self.ctrl.on_hotkey(100)
        self.wait_idle()
        self.preview.assert_called_once_with("Synthetic text")
        self.listener.start.assert_called_once()
        self.ctrl.disable()
        self.listener.stop.assert_called_once()

    def test_missing_dependency_never_registers(self):
        self.setup_controller(available=False)
        with self.assertRaisesRegex(SelectionFailure,"SDK_NOT_INSTALLED"):
            self.ctrl.enable("Ctrl+Alt+A")
        self.factory.assert_not_called()
        self.reader.assert_not_called()

    def test_busy_cancel_and_late_result_fenced(self):
        entered,release=threading.Event(),threading.Event()
        def read(target,stop):
            entered.set()
            release.wait(2)
            return "Late private result"
        self.setup_controller(read)
        self.ctrl.enable("Ctrl+Alt+A")
        try:
            self.ctrl.on_hotkey(100)
            self.assertTrue(entered.wait(1))
            self.ctrl.cancel()
            self.ctrl.on_hotkey(100)
            self.assertTrue(self.ctrl.busy)
            release.set()
            self.wait_idle()
            self.preview.assert_not_called()
            self.target.assert_called_once_with(100)
        finally:
            release.set()

    def test_target_change_blocks_preview(self):
        self.setup_controller()
        self.target.side_effect=[TARGET,SelectionTarget(100,102,900,800)]
        self.ctrl.enable("Ctrl+Alt+A")
        self.ctrl.on_hotkey(100)
        self.wait_idle()
        self.preview.assert_not_called()
        self.notify.assert_called_with("SELECTION_TARGET_CHANGED")

    def test_disallowed_and_thread_start_failure(self):
        self.setup_controller()
        self.ctrl.enable("Ctrl+Alt+A")
        self.allowed.return_value=False
        self.ctrl.on_hotkey(100)
        self.target.assert_not_called()
        self.allowed.return_value=True
        with patch("voxbridge.selection_controller.threading.Thread.start",side_effect=RuntimeError()):
            self.ctrl.on_hotkey(100)
        self.assertFalse(self.ctrl.busy)
        self.notify.assert_called_with("SELECTION_WORKER_START_FAILED")

    def test_app_preview_does_not_run_analysis(self):
        dlg=NS(text=Mock(),output=Mock(),consent=Mock(),message=Mock(),_on_analyze=Mock())
        app=NS(_selection_allowed=lambda:True,open_analysis=Mock(),analysis_dialog=dlg)
        VoxBridge._show_selection(app,"Synthetic")
        dlg.text.setPlainText.assert_called_once_with("Synthetic")
        dlg.consent.setChecked.assert_called_once_with(False)
        dlg._on_analyze.assert_not_called()


    def test_old_registration_callback_ignored_after_reenable(self):
        self.setup_controller()
        self.ctrl.enable("Ctrl+Alt+A")
        old_callback=self.factory.call_args.args[1]
        worker=threading.Thread(target=lambda: old_callback(100))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.ctrl.disable()
        self.ctrl.enable("Ctrl+Alt+A")
        self.app.processEvents()
        self.target.assert_not_called()

    def test_reenable_waits_for_old_registration_cleanup(self):
        self.setup_controller()
        self.ctrl.enable("Ctrl+Alt+A")
        self.listener.running=True
        self.ctrl.disable()
        with self.assertRaisesRegex(SelectionFailure,"CLEANUP_PENDING"):
            self.ctrl.enable("Ctrl+Alt+A")
        self.assertFalse(self.ctrl.enabled)
        self.listener.running=False
        self.ctrl.enable("Ctrl+Alt+A")
        self.assertTrue(self.ctrl.enabled)
