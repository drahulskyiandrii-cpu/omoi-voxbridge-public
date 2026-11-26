"""Full application startup, local previews and shutdown in isolated offscreen Qt.

No native hotkeys, microphone, actual screenshots, audio output, provider calls,
user settings, clipboard reads or local models. All inputs synthetic.
"""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from voxbridge.app import VoxBridge


def main():
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory(prefix="voxbridge-smoke-") as directory, \
         patch.dict(os.environ, {"LOCALAPPDATA": directory, "VOXBRIDGE_PROFILE_ROOT": directory,
                                  "VOXBRIDGE_RUNTIME_ROOT": str(Path(directory)/"local-runtime")}), \
         patch("voxbridge.app.HotkeyListener") as hotkey, \
         patch("voxbridge.selection_controller.HotkeyListener"), \
         patch("voxbridge.app.Microphone") as microphone, \
         patch.object(VoxBridge, "notify"), \
         patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
         patch("socket.socket.connect_ex", side_effect=AssertionError("NETWORK_FORBIDDEN")):
        bridge = VoxBridge(app)
        try:
            assert bridge._hotkey_ready
            assert hotkey.return_value.start.call_count == 6
            microphone.return_value.start.assert_not_called()
            bridge.open_control_center()
            bridge.control_center.buttons["analysis"].click()
            bridge.analysis_dialog.text.setPlainText("Ein VLAN ist ein logisches Netz.")
            bridge.analysis_dialog.speak_source.click()
            assert bridge.tts_dialog.text.toPlainText() == "Ein VLAN ist ein logisches Netz."
            assert not bridge.tts_dialog._busy
            bridge.control_center.buttons["vision"].click()
            assert bridge.vision_dialog._png is None
            bridge.control_center.buttons["voices"].click()
            assert bridge.voice_dialog.choices.count() == 0
            bridge.control_center.buttons["captions"].click()
            assert not bridge.live_captions_dialog._busy
            assert bridge.live_captions_dialog.devices.count() == 0
            bridge.control_center.buttons["demo"].click()
            assert len(bridge.caption_demo_core.snapshot().lines) == 2
            app.processEvents()
            bridge.control_center.stop_all.click()
            assert not bridge._shutting_down
            assert not bridge.analysis_dialog._busy
            assert not bridge.tts_dialog._busy
            assert not bridge.live_captions_dialog._busy
            microphone.return_value.start.assert_not_called()
            assert not list(Path(directory).iterdir()), "No user-settings writes expected"
        finally:
            bridge.shutdown()
            assert bridge._shutting_down
            assert not bridge.control_center._timer.isActive()
            assert bridge.analysis_dialog.text.toPlainText() == ""
            assert bridge.tts_dialog.text.toPlainText() == ""
            assert bridge.caption_demo_core.snapshot().lines == ()
            assert hotkey.return_value.stop.call_count == 6
            bridge.live_captions_dialog._overlay.deleteLater()
            for dialog in (bridge.control_center, bridge.analysis_dialog,
                           bridge.tts_dialog, bridge.vision_dialog, bridge.voice_dialog,
                           bridge.live_captions_dialog, bridge.caption_demo_overlay,
                           bridge.status, bridge.tray):
                dialog.deleteLater()
            bridge.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    print("OFFSCREEN_APP_START_PREVIEW_STOP_SHUTDOWN_PASS; NO REAL DEVICE_OR_NETWORK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
