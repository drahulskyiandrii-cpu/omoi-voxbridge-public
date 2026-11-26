"""Control centre and audio interlock tests; no native hotkeys, devices or APIs."""
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from voxbridge.app import VoxBridge
from voxbridge.config import Settings
from voxbridge.control_center import ControlCenter, ACTION_LABELS
from voxbridge.session import Session
from voxbridge.tts_ui import CloudTTSDialog


class CenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def destroy(self, dlg):
        dlg.close()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def fixture(self):
        app = NS(settings=Settings(), session=Session(), _shutting_down=False,
                 _hotkey_ready=True, _dictation_worker_generation=None,
                 selection=NS(enabled=False,busy=False), analysis_dialog=None,
                 vision_dialog=None, tts_dialog=None, live_captions_dialog=None,
                 cancel=Mock(),notify=Mock())
        app._tts_idle = lambda: VoxBridge._tts_idle(app)
        app._dictation_idle = lambda: VoxBridge._dictation_idle(app)
        return app

    def test_center_open_and_refresh_never_run_actions(self):
        actions = {name:Mock() for name in ACTION_LABELS}
        stop = Mock()
        values = {"dictation":"Ctrl+Alt+Space", "local":"OFF"}
        dlg = ControlCenter(status_provider=lambda: values, actions=actions, stop_all=stop)
        self.addCleanup(self.destroy, dlg)
        self.assertFalse(dlg._timer.isActive())
        dlg.show()
        self.assertTrue(dlg.status_group.isHidden())
        dlg.details.click()
        self.assertFalse(dlg.status_group.isHidden())
        self.assertTrue(dlg._timer.isActive())
        dlg.refresh.click()
        self.assertEqual(dlg.status_rows["dictation"].text(),"Ctrl+Alt+Space")
        for cb in actions.values():
            cb.assert_not_called()
        stop.assert_not_called()
        dlg.buttons["analysis"].click()
        actions["analysis"].assert_called_once()
        dlg.stop_all.click()
        stop.assert_called_once()
        dlg.hide()
        self.assertFalse(dlg._timer.isActive())

    def test_status_uses_key_presence_never_values(self):
        app = self.fixture()
        with patch.dict(os.environ, {"OPENAI_API_KEY":"private-fake-value",
                                      "CARTESIA_API_KEY":"private-fake-value"}, clear=True):
            status = VoxBridge._dashboard_status(app)
        self.assertIn("OpenAI",status["cloud"])
        self.assertIn("Cartesia",status["cloud"])
        self.assertNotIn("private-fake-value",repr(status))
        self.assertIn("ungepr",status["cloud"])
        self.assertIn("OFF",status["local"])
        self.assertIn("Ctrl+Alt+Space",status["dictation"])

    def test_stop_all_invokes_only_active_operations(self):
        app = self.fixture()
        app.analysis_dialog = Mock(_busy=True)
        app.vision_dialog = Mock(_busy=False,_overlay=object(),_capture_pending=False)
        app.tts_dialog = Mock(_busy=True)
        app.live_captions_dialog = Mock(_busy=True)
        VoxBridge.stop_all(app)
        app.cancel.assert_called_once()
        app.analysis_dialog._cancel_analysis.assert_called_once()
        app.vision_dialog.close.assert_called_once()
        app.tts_dialog.stop_all.assert_called_once()
        app.live_captions_dialog._stop_session.assert_called_once()
        app.vision_dialog._cancel.assert_not_called()

    def test_stop_all_includes_idle_warm_model_and_metadata_refresh(self):
        app = self.fixture()
        app.tts_dialog = Mock(busy=False)
        app.voice_dialog = Mock(_worker_generation=1)
        app.live_captions_dialog = Mock(_busy=False, _refreshing=True)
        VoxBridge.stop_all(app)
        app.tts_dialog.stop_all.assert_called_once()
        app.voice_dialog.reject.assert_called_once()
        app.live_captions_dialog.reject.assert_called_once()

    def test_audio_interlock_blocks_warmup_but_not_ready_residency(self):
        app = self.fixture()
        app.cuda_warm = Mock()
        app.tts_dialog = NS(busy=False)
        for phase in ("loading", "closing"):
            app.cuda_warm.status.return_value = (phase, "")
            self.assertFalse(VoxBridge._can_play_tts(app))
            self.assertFalse(VoxBridge._can_start_captions(app))
        app.cuda_warm.status.return_value = ("ready", "")
        self.assertTrue(VoxBridge._can_play_tts(app))
        self.assertTrue(VoxBridge._can_start_captions(app))
        app.cuda_warm.deactivate.assert_not_called()

    def test_audio_interlock_uses_pending_playback_and_capture(self):
        app = self.fixture()
        self.assertTrue(VoxBridge._can_start_captions(app))
        self.assertTrue(VoxBridge._can_play_tts(app))
        app.tts_dialog = NS(busy=True)
        self.assertFalse(VoxBridge._can_start_captions(app))
        app.recorder = Mock()
        VoxBridge.on_hotkey(app, 100)
        app.recorder.start.assert_not_called()
        app.tts_dialog = None
        app.live_captions_dialog = NS(_busy=True)
        self.assertFalse(VoxBridge._can_play_tts(app))
        app.live_captions_dialog = None
        app.session.begin(100,101)
        self.assertFalse(VoxBridge._can_play_tts(app))

    def test_tts_rejects_while_other_capture_active_even_with_standing_permission(self):
        factory = Mock()
        dlg = CloudTTSDialog(provider_factory=factory, permission_checker=lambda _: True,
                             can_start=lambda: False)
        self.addCleanup(self.destroy, dlg)
        dlg.text.setPlainText("Synthetic")
        dlg._play()
        self.assertIn("Andere VoxBridge-Aufnahme",dlg.status.text())
        factory.assert_not_called()
        self.assertFalse(dlg._busy)

class SimpleNavigationTests(unittest.TestCase):
    setUpClass = classmethod(CenterTests.setUpClass.__func__)
    destroy = CenterTests.destroy
    def test_engine_and_language_selectors_never_load_on_open(self):
        actions={key:Mock() for key in ACTION_LABELS}
        backend,language=Mock(),Mock()
        dlg=ControlCenter(status_provider=lambda:{},actions=actions,stop_all=Mock(),
                          settings_provider=lambda:Settings(stt_backend="mini_worker",language="ru"),
                          set_backend=backend,set_language=language)
        self.addCleanup(self.destroy,dlg)
        self.assertEqual(dlg.engine.currentData(),"mini_worker")
        self.assertEqual({dlg.engine.itemData(i) for i in range(dlg.engine.count())},{"mini_cpu","mini_worker","openai_cloud","groq_cloud"})
        self.assertEqual(dlg.language.currentData(),"ru")
        backend.assert_not_called()
        language.assert_not_called()
        self.assertTrue(dlg.buttons["voices"].isHidden())
        self.assertTrue(dlg.buttons["demo"].isHidden())
        dlg.engine.activated.emit(dlg.engine.findData("mini_cpu"))
        backend.assert_called_once_with("mini_cpu")
