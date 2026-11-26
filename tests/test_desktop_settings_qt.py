"""Explicit desktop settings and startup recovery; no native key registration."""
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from voxbridge.app import VoxBridge
from voxbridge.config import Settings
from voxbridge.desktop_settings_ui import DesktopSettingsDialog
from voxbridge.hotkeys import HotkeyListener
from voxbridge.session import Session


class DesktopSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, active=Settings(), **kwargs):
        dlg = DesktopSettingsDialog(active, **kwargs)
        self.addCleanup(self.destroy, dlg)
        return dlg

    def destroy(self, dlg):
        dlg.close()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_microphone_save_applies_only_microphone_to_idle_runtime(self):
        active = Settings(microphone_id="a"*64, microphone_label="Missing")
        updated = replace(active, microphone_id="", microphone_label="", max_record_seconds=120)
        bridge = NS(settings=active, recorder=Mock(), _dictation_idle=lambda: True)
        with patch("voxbridge.app.save_settings") as save:
            VoxBridge._save_desktop_settings(bridge, updated)
        save.assert_called_once_with(updated)
        self.assertEqual(bridge.recorder.microphone_id, "")
        self.assertEqual(bridge.settings, replace(active, microphone_id="", microphone_label=""))

    def test_busy_microphone_change_never_modifies_active_capture(self):
        active = Settings()
        updated = replace(active, microphone_id="a"*64, microphone_label="New")
        bridge = NS(settings=active, recorder=Mock(), _dictation_idle=lambda: False)
        with patch("voxbridge.app.save_settings") as save:
            with self.assertRaisesRegex(ValueError, "MIC_CHANGE_BUSY"):
                VoxBridge._save_desktop_settings(bridge, updated)
        save.assert_not_called()
        self.assertEqual(bridge.settings, active)

    def test_settings_categorized_without_changing_values(self):
        dlg = self.dialog(loader=Settings)
        self.assertEqual(dlg.tabs.count(), 4)
        self.assertEqual(dlg.tabs.currentIndex(), 0)
        self.assertTrue(dlg.tabs.widget(0).isAncestorOf(dlg.microphone))
        self.assertTrue(dlg.tabs.widget(3).isAncestorOf(dlg.stt_port))
        self.assertTrue(dlg.tabs.widget(3).isAncestorOf(dlg.vram))
        self.assertTrue(dlg.tabs.widget(2).isAncestorOf(dlg.region_task))
        self.assertEqual(dlg.dictation.text(), Settings().hotkey)

    def test_long_duration_setting_saved(self):
        saver = Mock()
        dlg = self.dialog(loader=Settings, saver=saver)
        self.assertEqual(dlg.seconds.maximum(), 120)
        dlg.seconds.setValue(120)
        dlg.save.click()
        saver.assert_called_once_with(replace(Settings(), max_record_seconds=120))

    def test_layout_safe_preset_changes_only_draft_and_preserves_tasks(self):
        saver = Mock()
        settings = replace(Settings(), region_task2="Explain", region_task2_hotkey="Ctrl+Alt+F8")
        dlg = self.dialog(loader=lambda: settings, saver=saver)
        dlg.layout_safe.click()
        self.assertEqual(dlg.dictation.text(), "Ctrl+Shift+F1")
        self.assertEqual(dlg.read_selection.text(), "Ctrl+Shift+F6")
        self.assertEqual(dlg.copy_read.text(), "Ctrl+Shift+F7")
        self.assertEqual(dlg.region_task2_hotkey.text(), "Ctrl+Shift+F8")
        self.assertEqual(dlg.region_task2.text(), "Explain")
        self.assertEqual(dlg.region_task3_hotkey.text(), "")
        saver.assert_not_called()
        dlg.save.click()
        from voxbridge.config import layout_safe_hotkeys
        saver.assert_called_once_with(layout_safe_hotkeys(settings))

    def test_copied_text_shortcut_conflict_rejected(self):
        saver = Mock()
        dlg = self.dialog(loader=Settings, saver=saver)
        dlg.copy_read.setText(dlg.read_selection.text())
        dlg.save.click()
        self.assertIn("GLOBAL_HOTKEY_CONFLICT", dlg.status.text())
        saver.assert_not_called()

    def test_save_preserves_latest_other_settings(self):
        saved = replace(Settings(), language="ru", cartesia_voice_id="11111111-1111-4111-8111-111111111111")
        loader = Mock(side_effect=[Settings(), saved])
        saver = Mock()
        dlg = self.dialog(loader=loader, saver=saver)
        dlg.dictation.setText("Ctrl+Shift+F8")
        dlg.selection.setText("Ctrl+Shift+F9")
        dlg.seconds.setValue(15)
        saver.assert_not_called()
        dlg.save.click()
        saver.assert_called_once_with(replace(saved, hotkey="Ctrl+Shift+F8",
                                             selection_hotkey="Ctrl+Shift+F9",
                                             max_record_seconds=15))
        self.assertIn("Neustart", dlg.status.text())

    def test_unedited_prewarm_preserves_latest_tray_choice(self):
        updated = replace(Settings(), tts_transport="qwen_cf050", prewarm_local_tts=True)
        loader = Mock(side_effect=[Settings(), updated])
        saver = Mock()
        dlg = self.dialog(loader=loader, saver=saver)
        dlg.seconds.setValue(50)
        dlg.save.click()
        saver.assert_called_once_with(replace(updated, max_record_seconds=50))
        self.assertTrue(dlg.prewarm_tts.isChecked())

    def test_explicit_prewarm_edit_is_saved(self):
        original = replace(Settings(), prewarm_local_tts=True)
        saver = Mock()
        dlg = self.dialog(loader=lambda: original, saver=saver)
        dlg.prewarm_tts.click()
        dlg.save.click()
        saver.assert_called_once_with(replace(original, prewarm_local_tts=False))

    def test_invalid_or_conflicting_hotkeys_never_saved(self):
        saver = Mock()
        dlg = self.dialog(loader=Settings, saver=saver)
        dlg.dictation.setText("Ctrl+Alt+A")
        dlg.save.click()
        self.assertIn("CONFLICT", dlg.status.text())
        dlg.dictation.setText("Space")
        dlg.save.click()
        self.assertIn("FAILED", dlg.status.text())
        saver.assert_not_called()

    def test_reset_only_changes_draft(self):
        saver = Mock()
        dlg = self.dialog(loader=lambda: replace(Settings(), hotkey="Ctrl+Shift+F8"), saver=saver)
        self.assertEqual(dlg.dictation.text(), "Ctrl+Shift+F8")
        dlg.defaults.click()
        self.assertEqual(dlg.dictation.text(), Settings().hotkey)
        saver.assert_not_called()

    def test_bad_file_is_not_overwritten(self):
        saver = Mock()
        dlg = self.dialog(loader=Mock(side_effect=ValueError("private input")), saver=saver)
        self.assertFalse(dlg.save.isEnabled())
        self.assertNotIn("private", dlg.status.text())
        saver.assert_not_called()

    def test_pending_restart_settings_not_adopted_by_language_change(self):
        active = Settings()
        saved = replace(active, hotkey="Ctrl+Shift+F8", max_record_seconds=10)
        bridge = NS(settings=active, session=Session(), notify=Mock(), _refresh_menu=Mock())
        with patch("voxbridge.app.load_settings", return_value=saved), patch("voxbridge.app.save_settings") as save:
            VoxBridge.set_language(bridge, "ru")
        save.assert_called_once_with(replace(saved, language="ru"))
        self.assertEqual(bridge.settings, replace(active, language="ru"))

    def test_selection_toggle_uses_active_combo_until_restart(self):
        active = Settings()
        saved = replace(active, selection_hotkey="Ctrl+Shift+F9")
        bridge = NS(settings=active, selection=Mock(), _shutting_down=False,
                    notify=Mock(), _refresh_menu=Mock())
        with patch("voxbridge.app.load_settings", return_value=saved), patch("voxbridge.app.save_settings") as save:
            VoxBridge.toggle_selection_hotkey(bridge, True)
        bridge.selection.enable.assert_called_once_with(active.selection_hotkey)
        self.assertEqual(bridge.settings.selection_hotkey, active.selection_hotkey)
        save.assert_called_once_with(replace(saved, selection_hotkey_enabled=True))

    def test_microphone_refresh_and_explicit_save(self):
        import time
        from voxbridge.microphone_devices import InputDevice
        mic = InputDevice("a"*64, 5, "Synthetic USB", "WASAPI")
        discover = Mock(return_value=((mic,),0))
        saver = Mock()
        dlg = self.dialog(loader=Settings,saver=saver,discover=discover)
        discover.assert_not_called()
        self.assertEqual(dlg.microphone.currentData(),"")
        dlg.refresh_mics.click()
        end = time.monotonic()+2
        while dlg._discovering and time.monotonic()<end:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(dlg._discovering)
        self.assertEqual(dlg.microphone.count(),2)
        dlg.microphone.setCurrentIndex(1)
        saver.assert_not_called()
        dlg.save.click()
        saver.assert_called_once_with(replace(Settings(),microphone_id="a"*64,microphone_label=mic.label))

    def test_missing_saved_microphone_not_silently_replaced(self):
        saved=replace(Settings(),microphone_id="b"*64)
        saver=Mock()
        dlg=self.dialog(loader=lambda:saved,saver=saver)
        self.assertEqual(dlg.microphone.currentData(),"b"*64)
        dlg._discovering=True
        dlg._devices_ready((),0,"")
        self.assertEqual(dlg.microphone.currentData(),"b"*64)
        self.assertIn("UNAVAILABLE",dlg.mic_status.text())
        dlg.save.click()
        saver.assert_called_once_with(saved)
        dlg.defaults.click()
        self.assertEqual(dlg.microphone.currentData(),"")
        saver.assert_called_once()

    def test_reopen_restores_saved_microphone_without_refresh_click(self):
        import tempfile
        import time
        from pathlib import Path
        from voxbridge.config import load_settings, save_settings
        from voxbridge.microphone_devices import InputDevice
        mic = InputDevice("a"*64, 8, "Synthetic USB", "WASAPI")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"settings.json"
            saved = replace(Settings(), microphone_id=mic.device_id)
            save_settings(saved,path)
            dlg = self.dialog(active=saved,loader=lambda:load_settings(path),
                              saver=lambda value:save_settings(value,path),
                              discover=lambda:((mic,),0))
            dlg.show()
            end=time.monotonic()+2
            while dlg._discovering and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(dlg._discovering)
            self.assertEqual(dlg.microphone.currentData(),mic.device_id)
            self.assertEqual(dlg.microphone.currentText(),mic.label)
            dlg.save.click()
            self.assertEqual(load_settings(path).microphone_label,mic.label)
            dlg.close()
            reopened = self.dialog(active=load_settings(path),loader=lambda:load_settings(path),
                                   saver=lambda value:save_settings(value,path),
                                   discover=lambda:((),0))
            self.assertIn(mic.label,reopened.microphone.currentText())
            self.assertEqual(reopened.microphone.currentData(),mic.device_id)
            reopened._discovering=True
            reopened._devices_ready((),0,"MIC_DEVICE_DISCOVERY_FAILED")
            self.assertIn(mic.label,reopened.microphone.currentText())
            self.assertEqual(reopened.microphone.currentData(),mic.device_id)
            reopened.save.click()
            self.assertEqual(load_settings(path).microphone_id,mic.device_id)
            self.assertEqual(load_settings(path).microphone_label,mic.label)

    def test_microphone_discovery_start_failure_recovers(self):
        dlg=self.dialog(loader=Settings,saver=Mock())
        with patch("voxbridge.desktop_settings_ui.threading.Thread.start",side_effect=RuntimeError()):
            dlg._refresh_microphones()
        self.assertFalse(dlg._discovering)
        self.assertTrue(dlg.refresh_mics.isEnabled())
        self.assertIn("DISCOVERY_FAILED",dlg.mic_status.text())

    def test_hotkey_conflict_keeps_tray_available(self):
        hotkeys = Mock()
        hotkeys.start.side_effect = RuntimeError("conflict")
        bridge = NS(hotkeys=hotkeys, notify=Mock(), settings=Settings())
        VoxBridge._start_hotkey(bridge)
        self.assertFalse(bridge._hotkey_ready)
        hotkeys.stop.assert_called_once()
        hotkeys.start.side_effect = None
        VoxBridge._start_hotkey(bridge)
        self.assertTrue(bridge._hotkey_ready)


class HotkeyLifecycleTests(unittest.TestCase):
    def test_timeout_requests_stop_and_retains_live_thread(self):
        listener = HotkeyListener("Ctrl+Alt+Space", Mock())
        thread = Mock()
        thread.is_alive.return_value = True
        listener._ready = Mock()
        listener._ready.wait.return_value = False
        with patch("voxbridge.hotkeys.threading.Thread", return_value=thread):
            with self.assertRaisesRegex(RuntimeError, "THREAD_TIMEOUT"):
                listener.start()
        self.assertTrue(listener._stop_requested.is_set())
        self.assertIs(listener._thread, thread)
        with self.assertRaisesRegex(RuntimeError, "ALREADY_RUNNING"):
            listener.start()
        thread.is_alive.return_value = False
        listener.stop()
        self.assertIsNone(listener._thread)

    def test_thread_start_failure_is_safe_to_stop(self):
        listener = HotkeyListener("Ctrl+Alt+Space", Mock())
        thread = Mock()
        thread.start.side_effect = RuntimeError("synthetic")
        with patch("voxbridge.hotkeys.threading.Thread", return_value=thread):
            with self.assertRaisesRegex(RuntimeError, "THREAD_START_FAILED"):
                listener.start()
        listener.stop()
        self.assertIsNone(listener._thread)
        thread.join.assert_not_called()


    def test_stop_never_joins_live_thread(self):
        listener=HotkeyListener("Ctrl+Alt+Space", Mock())
        thread=Mock()
        thread.is_alive.return_value=True
        listener._thread=thread
        listener.stop()
        thread.join.assert_not_called()
        self.assertTrue(listener._stop_requested.is_set())
        self.assertTrue(listener.running)
        self.assertFalse(listener.wait_closed(.01))
        thread.join.assert_called_once_with(timeout=.01)
        thread.is_alive.return_value=False
        self.assertTrue(listener.wait_closed(.01))

    def test_native_setup_failure_signals_ready_without_timeout(self):
        listener=HotkeyListener("Ctrl+Alt+Space", Mock())
        with patch("voxbridge.hotkeys.ctypes.WinDLL",side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(RuntimeError,"HOTKEY_NATIVE_FAILED"):
                listener.start()
        self.assertTrue(listener._ready.is_set())
        self.assertTrue(listener.wait_closed(1))

    def test_registration_failure_does_not_unregister_unowned_hotkey(self):
        listener=HotkeyListener("Ctrl+Alt+Space",Mock())
        api=Mock()
        api.RegisterHotKey.return_value=False
        with patch("voxbridge.hotkeys.ctypes.WinDLL",return_value=api):
            with self.assertRaisesRegex(RuntimeError,"HOTKEY_IN_USE_OR_UNAVAILABLE"):
                listener.start()
        self.assertTrue(listener.wait_closed(1))
        api.UnregisterHotKey.assert_not_called()

    def test_native_wait_failure_releases_own_registration(self):
        listener=HotkeyListener("Ctrl+Alt+Space",Mock())
        api=Mock()
        api.RegisterHotKey.return_value=True
        api.UnregisterHotKey.return_value=True
        api.MsgWaitForMultipleObjectsEx.return_value=0xffffffff
        with patch("voxbridge.hotkeys.ctypes.WinDLL",return_value=api):
            try:
                listener.start()
            except RuntimeError as exc:
                self.assertEqual(str(exc),"HOTKEY_MESSAGE_WAIT_FAILED")
            self.assertTrue(listener.wait_closed(1))
        api.UnregisterHotKey.assert_called_once()
        listener.callback.assert_not_called()
