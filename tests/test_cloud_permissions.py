"""Standing opt-in is explicit, provider-scoped, revocable, and never auto-runs."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from voxbridge.cloud_permissions import (
    CloudPermissions, SCOPES, approved, load_permissions, save_permissions,
    standing_permission,
)
from voxbridge.cloud_permissions_ui import CloudPermissionsDialog
from voxbridge.app import VoxBridge
from voxbridge.analysis_ui import AnalysisDialog
from voxbridge.tts_ui import CloudTTSDialog
from voxbridge.vision_ui import VisionDialog
from voxbridge.live_captions_ui import LiveCaptionsDialog
from voxbridge.wasapi_capture import LoopbackDevice


class PolicyTests(unittest.TestCase):
    def test_default_scope_and_revocation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "permissions.json"
            with patch("voxbridge.cloud_permissions.permission_path", return_value=path):
                self.assertFalse(any(standing_permission(scope) for scope in SCOPES))
                save_permissions(CloudPermissions(dictation_openai=True))
                self.assertTrue(standing_permission("dictation_openai"))
                self.assertFalse(standing_permission("vision_openai"))
                self.assertFalse(standing_permission("local"))
                save_permissions(CloudPermissions())
                self.assertFalse(standing_permission("dictation_openai"))

    def test_invalid_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "permissions.json"
            with patch("voxbridge.cloud_permissions.permission_path", return_value=path):
                for content in ('{"dictation_openai":1}', '{"unknown":true}', '{', ' ' * 4097):
                    path.write_text(content)
                    self.assertFalse(standing_permission("dictation_openai"))
                self.assertFalse(approved(lambda scope: 1, "dictation_openai"))
                self.assertFalse(approved(Mock(side_effect=RuntimeError()), "dictation_openai"))

    def test_dictation_standing_permission_skips_modal(self):
        with patch("voxbridge.app.standing_permission", return_value=True) as checker:
            with patch("voxbridge.app.QMessageBox") as dialog:
                self.assertTrue(VoxBridge._approve_dictation(object()))
                dialog.assert_not_called()
            checker.assert_called_once_with("dictation_openai")


class PermissionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_explicit_save_and_revoke(self):
        saver = Mock()
        dlg = CloudPermissionsDialog(loader=lambda: CloudPermissions(), saver=saver)
        self.assertFalse(any(c.isChecked() for c in dlg.choices.values()))
        dlg.choices["tts_cartesia"].setChecked(True)
        saver.assert_not_called()
        dlg._save()
        saver.assert_called_once_with(CloudPermissions(tts_cartesia=True))
        dlg._revoke()
        dlg._save()
        self.assertEqual(saver.call_args.args[0], CloudPermissions())
        dlg.close()

    def test_analysis_scope_separate_and_checked_at_action(self):
        allowed = {"analysis_openai"}
        dlg = AnalysisDialog(permission_checker=lambda s: s in allowed)
        dlg.text.setPlainText("Explain VLAN")
        dlg._make_provider = Mock()
        dlg.provider.setCurrentIndex(dlg.provider.findData("anthropic"))
        dlg._on_analyze()
        dlg._make_provider.assert_not_called()
        dlg.provider.setCurrentIndex(dlg.provider.findData("openai"))
        with patch("voxbridge.analysis_ui.threading.Thread.start", side_effect=RuntimeError()):
            dlg._on_analyze()
        dlg._make_provider.assert_called_once_with("openai")
        allowed.clear()
        dlg._make_provider.reset_mock()
        dlg._on_analyze()
        dlg._make_provider.assert_not_called()
        dlg.close()

    def test_captions_audio_does_not_grant_translation(self):
        allowed = {"captions_openai_audio"}
        dlg = LiveCaptionsDialog(permission_checker=lambda s: s in allowed,
                                 capture=Mock(), transcribe=Mock())
        dlg.devices.addItem("Synthetic", "fake")
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
        with patch.dict(os.environ, {"OPENAI_API_KEY":"synthetic"}):
            dlg._start()
            self.assertFalse(dlg._busy)
            self.assertIn("Separate Zustimmung", dlg.status.text())
            allowed.add("captions_openai_translation")
            with patch("voxbridge.live_captions_ui.threading.Thread.start", side_effect=RuntimeError()):
                dlg._start()
            self.assertIn("WORKER_START_FAILED", dlg.status.text())
            self.assertFalse(dlg._busy)
        dlg._capture.assert_not_called()
        dlg._transcribe.assert_not_called()
        dlg.close()

    def test_tts_and_vision_accept_standing_opt_in_without_checkbox(self):
        from voxbridge.config import Settings
        tts = CloudTTSDialog(provider_factory=Mock(),
                             permission_checker=lambda s: s == "tts_cartesia")
        tts.text.setPlainText("Synthetic")
        with patch("voxbridge.tts_ui.load_settings", return_value=Settings(cartesia_voice_id="11111111-1111-4111-8111-111111111111")):
            with patch("voxbridge.tts_ui.threading.Thread.start", side_effect=RuntimeError()):
                tts._play()
        self.assertIn("WORKER_START_FAILED", tts.status.text())
        tts._provider_factory.assert_not_called()
        tts.close()
        vision = VisionDialog(provider_factory=Mock(),
                              permission_checker=lambda s: s == "vision_openai")
        vision._png = b"synthetic"
        with patch("voxbridge.vision_ui.threading.Thread.start", side_effect=RuntimeError()):
            vision._analyze()
        self.assertIn("WORKER_START_FAILED", vision.message.text())
        vision._provider_factory.assert_not_called()
        vision.close()
