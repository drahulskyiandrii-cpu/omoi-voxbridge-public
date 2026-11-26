"""Offscreen Qt tests: no speaker/system capture, cloud HTTP or paid requests."""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent
    from voxbridge.live_captions_ui import LiveCaptionsDialog
except ImportError:
    QApplication = None

from voxbridge.wasapi_capture import LoopbackDevice


@unittest.skipUnless(QApplication is not None, "PySide6 not available")
class LiveCaptionsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_idle(self, dlg):
        end = time.monotonic() + 4
        while dlg._busy and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.012)
        self.app.processEvents()
        self.assertFalse(dlg._busy)

    def wait_devices(self, dlg):
        end = time.monotonic() + 3
        while dlg._refreshing and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(dlg._refreshing)

    @staticmethod
    def fake_devices():
        return [LoopbackDevice("fake_wasapi_loopback", "Test speakers")]

    @staticmethod
    def fake_capture(device_id, stop, seconds):
        assert device_id == "fake_wasapi_loopback"
        assert seconds == 30
        yield bytes(9600)

    @staticmethod
    def fake_transcribe(controller, generation, pcm, **kwargs):
        assert kwargs["approved"] is True
        assert kwargs["max_seconds"] == 30
        assert list(pcm) == [bytes(9600)]
        assert controller.final(generation, 0, "Ein Router leitet Pakete weiter.")
        return 1

    def new_dialog(self, *, discover_devices=None, capture=None,
                   transcribe=None, translation_factory=None, can_start=None, **kwargs):
        dlg = LiveCaptionsDialog(
            discover_devices=discover_devices or self.fake_devices,
            capture=capture or self.fake_capture,
            transcribe=transcribe or self.fake_transcribe,
            translation_factory=translation_factory or Mock(),
            can_start=can_start, **kwargs,
        )
        self.addCleanup(self.dispose_dialog, dlg)
        return dlg

    def dispose_dialog(self, dlg):
        # Qt objects must be disposed on the GUI thread, not at a later GC
        # during another test's queued callbacks. Some cases already delete.
        from shiboken6 import isValid
        if not isValid(dlg):
            return
        dlg.close()
        if isValid(dlg._overlay):
            dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_quarantined_native_capture_blocks_start_before_transport(self):
        from voxbridge.capture_isolation import capture_live
        dlg = self.new_dialog(capture=capture_live,transcribe=Mock())
        with patch("voxbridge.live_captions_ui.capture_block_reason",
                   return_value="WASAPI_CHILD_CLEANUP_FAILED"):
            dlg._start()
        self.assertFalse(dlg._busy)
        self.assertEqual(dlg.status.text(),"WASAPI_CHILD_CLEANUP_FAILED")
        dlg._transcribe.assert_not_called()

    def test_cleanup_failure_survives_both_worker_completion_orders(self):
        for source_first in (True, False):
            with self.subTest(source_first=source_first):
                dlg = self.new_dialog(transcribe=Mock())
                dlg._busy = True
                dlg._worker_generation = 1
                dlg._source_done = False
                dlg._translation_alive = True
                if source_first:
                    dlg._completed(1, "")
                    dlg._translation_completed(1, "PROVIDER_CLEANUP_FAILED")
                else:
                    dlg._translation_completed(1, "")
                    dlg._completed(1, "REALTIME_CLEANUP_FAILED")
                self.assertFalse(dlg._busy)
                self.assertFalse(dlg.start.isEnabled())
                self.assertEqual(dlg.status.text(), "PROVIDER_CLEANUP_FAILED" if source_first
                                 else "REALTIME_CLEANUP_FAILED")
                dlg._start()
                dlg._transcribe.assert_not_called()

    def test_construction_is_private_and_offline(self):
        enumerate_backend = Mock(side_effect=AssertionError("no discovery on startup"))
        capture_backend = Mock(side_effect=AssertionError("no capture on startup"))
        transcribe_backend = Mock(side_effect=AssertionError("no API on startup"))
        dlg = self.new_dialog(discover_devices=enumerate_backend,
                              capture=capture_backend, transcribe=transcribe_backend)
        self.assertEqual(dlg.devices.count(), 0)
        self.assertFalse(dlg.stop.isEnabled())
        self.assertEqual(dlg.translation_engine.currentData(), "off")
        self.assertFalse(dlg.translation_consent.isEnabled())
        enumerate_backend.assert_not_called()
        capture_backend.assert_not_called()
        transcribe_backend.assert_not_called()
        dlg.close()

    def test_device_refresh_is_async_and_blocks_start_until_result(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked_discovery():
            calls.append(1)
            entered.set()
            release.wait(3)
            return self.fake_devices()

        dlg = self.new_dialog(discover_devices=blocked_discovery)
        try:
            dlg._refresh()
            self.assertTrue(entered.wait(2))
            self.assertTrue(dlg._refreshing)
            self.assertFalse(dlg.start.isEnabled())
            self.assertFalse(dlg.refresh.isEnabled())
            dlg._refresh()
            dlg._start()  # even direct callback must not begin recording
            self.assertEqual(len(calls), 1)
            self.assertEqual(dlg.devices.count(), 0)
            release.set()
            self.wait_devices(dlg)
            self.assertEqual(dlg.devices.count(), 1)
            self.assertTrue(dlg.start.isEnabled())
        finally:
            release.set()
            dlg.close()

    def test_close_discards_late_device_refresh_result(self):
        entered = threading.Event()
        release = threading.Event()

        def discovery():
            entered.set()
            release.wait(3)
            return self.fake_devices()

        dlg = self.new_dialog(discover_devices=discovery)
        dlg._refresh()
        self.assertTrue(entered.wait(2))
        dlg.close()
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(dlg.devices.count(), 0)
        self.assertFalse(dlg._refreshing)
        self.assertTrue(dlg.refresh.isEnabled())
        self.assertTrue(dlg.start.isEnabled())
        dlg.close()

    def test_discovery_failure_is_safe_and_no_capture(self):
        capture = Mock(side_effect=AssertionError("must not capture"))
        def bad_discovery():
            from voxbridge.wasapi_capture import CaptureFailure
            raise CaptureFailure("WASAPI_DEVICE_ENUMERATION_FAILED")

        dlg = self.new_dialog(discover_devices=bad_discovery, capture=capture)
        dlg._refresh()
        self.wait_devices(dlg)
        self.assertIn("WASAPI_DEVICE_ENUMERATION_FAILED", dlg.status.text())
        self.assertEqual(dlg.devices.count(), 0)
        capture.assert_not_called()
        dlg.close()

    def test_dictation_busy_prevents_another_billable_audio_session(self):
        capture = Mock(side_effect=AssertionError("not allowed to capture"))
        transcribe = Mock(side_effect=AssertionError("not allowed to bill"))
        dlg = self.new_dialog(
            capture=capture, transcribe=transcribe,
            can_start=lambda: False,
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-key"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.audio_consent.setChecked(True)
            dlg._start()
        self.assertIn("Andere VoxBridge-Aufnahme", dlg.status.text())
        self.assertFalse(dlg._busy)
        capture.assert_not_called()
        transcribe.assert_not_called()
        dlg.close()

    def test_requires_device_audio_consent_and_api_key(self):
        transport = Mock(side_effect=AssertionError("must not send"))
        dlg = self.new_dialog(transcribe=transport)
        dlg._start()
        self.assertIn("Loopback", dlg.status.text())
        dlg._refresh()
        self.wait_devices(dlg)
        self.assertEqual(dlg.devices.count(), 1)
        dlg._start()
        self.assertIn("Freigabe", dlg.status.text())
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            dlg.audio_consent.setChecked(True)
            dlg._start()
        self.assertIn("OPENAI_API_KEY_MISSING", dlg.status.text())
        transport.assert_not_called()
        dlg.close()

    def test_synthetic_original_only_session(self):
        dlg = self.new_dialog()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.audio_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertIn("Sitzung beendet", dlg.status.text())
        self.assertFalse(dlg._coordinator.snapshot().active)
        self.assertIn("Router", dlg._coordinator.snapshot().lines[0].original)
        self.assertEqual(dlg._coordinator.snapshot().target_language, None)
        self.assertTrue(dlg.start.isEnabled())
        self.assertFalse(dlg.audio_consent.isChecked())
        dlg.close()
        self.assertEqual(dlg._coordinator.snapshot().lines, ())

    def test_unconfigured_local_choice_never_starts_source_or_cloud(self):
        cloud = Mock()
        transport = Mock()
        dlg = self.new_dialog(translation_factory=cloud, transcribe=transport)
        dlg.devices.addItem("Fake", "fake_wasapi_loopback")
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("gemma_cpu"))
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        dlg._start()
        self.assertIn("LOCAL_TRANSLATOR_NOT_CONFIGURED", dlg.status.text())
        transport.assert_not_called()
        cloud.assert_not_called()

    def test_local_engine_requires_fresh_approval_and_never_uses_cloud_translation(self):
        for key in ("gemma_cpu", "specialist"):
            with self.subTest(key=key):
                local = Mock(is_cloud=False)
                local.translate.return_value = "Synthetic local translation."
                factory = Mock(return_value=local)
                cloud = Mock()
                dlg = self.new_dialog(translation_factory=cloud,
                                      local_translation_factories={key: factory},
                                      permission_checker=lambda _: True)
                dlg.devices.addItem("Fake", "fake_wasapi_loopback")
                dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData(key))
                dlg._start()
                self.assertIn("Zustimmung", dlg.status.text())
                factory.assert_not_called()
                dlg.translation_consent.setChecked(True)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}):
                    dlg._start()
                    self.wait_idle(dlg)
                self.assertEqual(dlg._coordinator.snapshot().lines[0].translation,
                                 "Synthetic local translation.")
                self.assertIsNone(dlg._quota)
                factory.assert_called_once()
                local.translate.assert_called_once()
                local.close.assert_called_once()
                cloud.assert_not_called()

    def test_local_preflight_fails_before_audio_or_cloud(self):
        class Local:
            is_cloud = False
            def prepare(self, cancel):
                raise RuntimeError("private failure")
            def close(self):
                pass
        cloud, transport, capture = Mock(), Mock(), Mock()
        dlg = self.new_dialog(translation_factory=cloud, transcribe=transport, capture=capture,
                              local_translation_factories={"gemma_cpu": Local})
        dlg.devices.addItem("Fake", "fake")
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("gemma_cpu"))
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}):
            dlg._start()
            self.wait_idle(dlg)
        self.assertIn("LOCAL_TRANSLATOR_PREFLIGHT_FAILED", dlg.status.text())
        self.assertNotIn("private failure", dlg.status.text())
        capture.assert_not_called()
        transport.assert_not_called()
        cloud.assert_not_called()

    def test_switching_engine_invalidates_permissions_and_advanced_is_collapsed(self):
        dlg = self.new_dialog()
        self.assertTrue(dlg.advanced_panel.isHidden())
        dlg.advanced_toggle.click()
        self.assertFalse(dlg.advanced_panel.isHidden())
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("gemma_cpu"))
        self.assertFalse(dlg.audio_consent.isChecked())
        self.assertFalse(dlg.translation_consent.isChecked())
        self.assertEqual(dlg._current_preferences().translation_engine, "gemma_cpu")

    def test_translation_requires_separate_approval(self):
        transport = Mock(side_effect=AssertionError("must not send"))
        dlg = self.new_dialog(transcribe=transport)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg._start()
        self.assertIn("Zustimmung", dlg.status.text())
        transport.assert_not_called()
        dlg.close()

    def test_translation_one_fake_request(self):
        provider = Mock()
        provider.is_cloud = True
        provider.generate.return_value = "Роутер пересылает пакеты."
        provider_factory = Mock(return_value=provider)
        dlg = self.new_dialog(translation_factory=provider_factory)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        row = dlg._coordinator.snapshot().lines[0]
        self.assertEqual(row.status, "complete")
        self.assertEqual(row.translation, "Роутер пересылает пакеты.")
        provider.generate.assert_called_once()
        provider.close.assert_called_once()
        self.assertFalse(dlg.translation_consent.isChecked())
        dlg.close()

    def test_translation_limits_token_and_timeout_per_call(self):
        provider = Mock()
        provider.is_cloud = True
        provider.generate.return_value = "Маршрутизатор передаёт пакеты."
        factory = Mock(return_value=provider)
        dlg = self.new_dialog(translation_factory=factory)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-key"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs["max_output_tokens"], 256)
        self.assertEqual(factory.call_args.kwargs["timeout"], 15)
        self.assertEqual(dlg._quota.calls_reserved, 1)
        dlg.close()

    def test_old_cloud_translation_blocks_restart_even_after_audio_finishes(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowCloudProvider:
            is_cloud = True

            def generate(self, system, user):
                entered.set()
                release.wait(3)
                return "Запоздавший перевод."

        factory = Mock(return_value=SlowCloudProvider())
        transcriber = Mock(side_effect=self.fake_transcribe)
        dlg = self.new_dialog(translation_factory=factory,
                              transcribe=transcriber)
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-key"}):
                dlg._refresh()
                self.wait_devices(dlg)
                dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
                dlg.audio_consent.setChecked(True)
                dlg.translation_consent.setChecked(True)
                dlg._start()
                self.assertTrue(entered.wait(2))
                # Consume the source-worker completed signal, while the
                # background translator deliberately remains blocked.
                deadline = time.monotonic() + 2
                while not dlg._source_done and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.app.processEvents()
                self.assertTrue(dlg._source_done)
                self.assertTrue(dlg._translation_alive)
                self.assertTrue(dlg._busy)
                self.assertFalse(dlg.start.isEnabled())

                dlg._stop_session()
                self.assertTrue(dlg._quota._closed)
                self.assertTrue(dlg._busy)
                dlg._start()  # must do NOTHING until old remote LLM exits
                transcriber.assert_called_once()
                release.set()
                self.wait_idle(dlg)
            self.assertTrue(dlg.start.isEnabled())
            self.assertEqual(dlg._coordinator.snapshot().lines, ())
        finally:
            release.set()
            dlg.close()

    def test_limit_changes_require_fresh_translation_consent(self):
        dlg = self.new_dialog()
        dlg._refresh()
        self.wait_devices(dlg)
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        dlg.translation_limit.setValue(2)
        self.assertFalse(dlg.translation_consent.isChecked())
        dlg.translation_consent.setChecked(True)
        dlg.seconds.setValue(20)
        self.assertFalse(dlg.audio_consent.isChecked())
        self.assertFalse(dlg.translation_consent.isChecked())
        dlg.close()

    def test_configured_translation_limit_and_usage_display(self):
        def fake_transcribe(controller, generation, pcm, **kwargs):
            usage = kwargs["usage"]
            usage.claim(30)
            usage.reserve_audio(4800)
            usage.reserve_commit()
            self.fake_transcribe(controller, generation, pcm, **kwargs)
        provider = Mock()
        provider.is_cloud = True
        provider.generate.return_value = "Test"
        dlg = self.new_dialog(transcribe=fake_transcribe,
                              translation_factory=Mock(return_value=provider))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.translation_limit.setValue(2)
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertEqual(dlg._quota.max_calls, 2)
        self.assertIn("0.2 / 30 s", dlg.usage_label.text())
        self.assertIn("1/2", dlg.usage_label.text())
        self.assertFalse(dlg._usage_timer.isActive())
        self.assertFalse(dlg.audio_consent.isChecked())
        dlg.close()

    def test_language_settings_are_frozen_and_require_fresh_consent(self):
        def transcribe(controller, generation, pcm, **kwargs):
            snap = controller.snapshot()
            self.assertEqual(snap.source_language, "ru")
            self.assertEqual(snap.target_language, "de")
            self.assertEqual(kwargs["delay"], "medium")
            controller.final(generation, 0, "Test")
            return 1
        provider = Mock()
        provider.is_cloud = True
        provider.generate.return_value = "Test"
        dlg = self.new_dialog(transcribe=transcribe,
                              translation_factory=Mock(return_value=provider))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.audio_consent.setChecked(True)
            dlg.source_language.setCurrentIndex(dlg.source_language.findData("ru"))
            self.assertFalse(dlg.audio_consent.isChecked())
            self.assertNotEqual(dlg.target_language.currentData(), "ru")
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.delay.setCurrentIndex(dlg.delay.findData("medium"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertEqual(dlg._coordinator.snapshot().target_language, "de")
        self.assertIn("Sitzung beendet", dlg.status.text())
        dlg.close()

    def test_failed_source_retires_partial_and_closes_translation_quota(self):
        from voxbridge.realtime_transport import RealtimeTransportFailure
        def transcribe(controller, generation, pcm, **kwargs):
            controller.partial(generation, 0, 0, "tentative")
            raise RealtimeTransportFailure("REALTIME_FINAL_TIMEOUT")
        provider = Mock()
        dlg = self.new_dialog(transcribe=transcribe, translation_factory=provider)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertEqual(dlg._coordinator.snapshot().partial, "")
        self.assertFalse(dlg._coordinator.snapshot().active)
        self.assertIn("REALTIME_FINAL_TIMEOUT", dlg.status.text())
        self.assertEqual(dlg._quota.calls_reserved, 0)
        provider.assert_not_called()
        dlg.close()

    def test_failed_translation_thread_start_does_not_lock_dialog(self):
        dlg = self.new_dialog()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            real_start = threading.Thread.start
            def start(thread):
                if thread.name == "voxbridge-live-ru":
                    raise RuntimeError("synthetic thread failure")
                return real_start(thread)
            with patch.object(threading.Thread, "start", start):
                dlg._start()
                self.wait_idle(dlg)
        self.assertTrue(dlg.start.isEnabled())
        self.assertFalse(dlg._translation_alive)
        self.assertIn("LIVE_CAPTIONS_SESSION_FAILED", dlg.status.text())
        dlg.close()

    def test_preferences_load_save_reset_never_restore_consent(self):
        from voxbridge.caption_preferences import CaptionPreferences
        prefs = CaptionPreferences(source="ru", target="de", translate=True,
                                   font_size=24, opacity=75, translation_limit=2)
        saver = Mock()
        dlg = LiveCaptionsDialog(discover_devices=self.fake_devices,
                                 capture=self.fake_capture,
                                 transcribe=self.fake_transcribe,
                                 preference_loader=lambda: prefs,
                                 preference_saver=saver)
        self.assertEqual(dlg._current_preferences(), prefs)
        self.assertFalse(dlg.audio_consent.isChecked())
        self.assertFalse(dlg.translation_consent.isChecked())
        self.assertEqual(dlg.devices.count(), 0)
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        dlg._save_preferences()
        saver.assert_called_once_with(prefs)
        dlg._reset_preferences()
        self.assertEqual(dlg._current_preferences(), CaptionPreferences())
        self.assertFalse(dlg.audio_consent.isChecked())
        self.assertFalse(dlg.translation_consent.isChecked())
        dlg.close()

    def test_final_arriving_between_empty_queue_and_source_finished_is_translated(self):
        empty_read, final_written = threading.Event(), threading.Event()
        def transcribe(controller, generation, pcm, **kwargs):
            if not empty_read.wait(2):
                raise RuntimeError("test synchronization")
            controller.final(generation, 0, "Last sentence")
            final_written.set()
        provider = Mock(is_cloud=True)
        provider.generate.return_value = "Last translation"
        dlg = self.new_dialog(transcribe=transcribe, translation_factory=Mock(return_value=provider))
        real_take = dlg._coordinator.take_translation
        first = True
        def take():
            nonlocal first
            job = real_take()
            if first:
                first = False
                empty_read.set()
                if not final_written.wait(2) or not dlg._source_finished.wait(2):
                    raise RuntimeError("test synchronization")
                return None
            return job
        with patch.object(dlg._coordinator, "take_translation", side_effect=take), patch.dict(os.environ, {"OPENAI_API_KEY":"mock-value"}):
            dlg.devices.addItem("Synthetic", "fake")
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        self.assertEqual(dlg._coordinator.snapshot().lines[0].translation, "Last translation")
        provider.generate.assert_called_once()
        dlg.close()
        dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_grouped_session_flushes_trailing_sentence_and_calls_once(self):
        def transcribe(controller, generation, pcm, **kwargs):
            controller.final(generation, 0, "Wenn der Router")
            controller.final(generation, 1, "nicht antwortet")
            return 2
        provider = Mock(is_cloud=True)
        provider.generate.return_value = "Combined translation"
        dlg = self.new_dialog(transcribe=transcribe, translation_factory=Mock(return_value=provider))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg.devices.addItem("Synthetic", "fake")
            dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
            dlg.group_sentences.setChecked(True)
            dlg.audio_consent.setChecked(True)
            dlg.translation_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        rows = dlg._coordinator.snapshot().lines
        self.assertEqual([r.status for r in rows], ["grouped", "complete"])
        self.assertEqual(rows[-1].translation, "Combined translation")
        provider.generate.assert_called_once()
        self.assertIn("Wenn der Router nicht antwortet", provider.generate.call_args.args[1])
        dlg.close()
        dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_overlay_close_stops_underlying_session(self):
        running = threading.Event()
        def transcribe(controller, generation, pcm, **kwargs):
            controller.final(generation, 0, "Synthetic")
            running.set()
            kwargs["stop"].wait(3)
        dlg = self.new_dialog(transcribe=transcribe)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg.devices.addItem("Synthetic", "fake")
            dlg.audio_consent.setChecked(True)
            dlg._start()
            self.assertTrue(running.wait(1))
            dlg._overlay.close()
            self.assertTrue(dlg._stop.is_set())
            self.wait_idle(dlg)
        self.assertEqual(dlg._coordinator.snapshot().lines, ())
        dlg.close()
        dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_discovery_start_failure_unlocks(self):
        dlg = self.new_dialog()
        with patch("voxbridge.live_captions_ui.threading.Thread.start", side_effect=RuntimeError()):
            dlg._refresh()
        self.assertFalse(dlg._refreshing)
        self.assertTrue(dlg.refresh.isEnabled())
        self.assertIn("WORKER_START_FAILED", dlg.status.text())
        dlg.close()
        dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_successful_end_fences_partial_and_late_final(self):
        def transcribe(controller, generation, pcm, **kwargs):
            controller.final(generation, 0, "Committed")
            controller.partial(generation, 1, 0, "Tentative")
        dlg = self.new_dialog(transcribe=transcribe)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg.devices.addItem("Synthetic", "fake")
            dlg.audio_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
        snap = dlg._coordinator.snapshot()
        self.assertFalse(snap.active)
        self.assertEqual(snap.partial, "")
        self.assertEqual(snap.lines[0].original, "Committed")
        self.assertFalse(dlg._coordinator.final(snap.generation, 2, "Late"))
        dlg.close()
        dlg._overlay.deleteLater()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_stop_cancels_without_restarting(self):
        running = threading.Event()
        def blocking_transcriber(controller, generation, pcm, *, approved, stop, max_seconds, usage, delay, on_stage):
            del controller, generation, pcm, approved, max_seconds
            running.set()
            stop.wait(3)
            return 0
        dlg = self.new_dialog(transcribe=blocking_transcriber)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-value"}):
            dlg._refresh()
            self.wait_devices(dlg)
            dlg.audio_consent.setChecked(True)
            dlg._start()
            self.assertTrue(running.wait(1))
            dlg._stop_session()
            self.wait_idle(dlg)
        self.assertFalse(dlg._coordinator.snapshot().active)
        self.assertTrue(dlg._stop.is_set())
        self.assertFalse(dlg.stop.isEnabled())
        dlg.close()


    def test_ink_provider_own_consent_and_key(self):
        ink=Mock(side_effect=self.fake_transcribe)
        dlg=self.new_dialog(ink_transcribe=ink,permission_checker=lambda scope:scope=="captions_openai_audio")
        dlg.devices.addItem("Test speakers","fake_wasapi_loopback")
        dlg.audio_consent.setChecked(True)
        dlg.stt_provider.setCurrentIndex(dlg.stt_provider.findData("cartesia_preview"))
        self.assertFalse(dlg.audio_consent.isChecked())
        self.assertFalse(dlg.delay.isEnabled())
        self.assertIn("Cartesia",dlg.audio_consent.text())
        with patch.dict(os.environ,{"OPENAI_API_KEY":"","CARTESIA_API_KEY":"synthetic"}):
            dlg._start()
            self.assertFalse(dlg._busy)
            ink.assert_not_called()
            dlg.audio_consent.setChecked(True)
            dlg._start()
            self.wait_idle(dlg)
            ink.assert_called_once()
        self.assertFalse(dlg.delay.isEnabled())
        self.assertTrue(dlg.stt_provider.isEnabled())

    def test_ink_cloud_translation_needs_openai_key_too(self):
        ink=Mock()
        dlg=self.new_dialog(ink_transcribe=ink)
        dlg.devices.addItem("Test speakers","fake_wasapi_loopback")
        dlg.stt_provider.setCurrentIndex(dlg.stt_provider.findData("cartesia_preview"))
        dlg.translation_engine.setCurrentIndex(dlg.translation_engine.findData("cloud"))
        dlg.audio_consent.setChecked(True)
        dlg.translation_consent.setChecked(True)
        with patch.dict(os.environ,{"OPENAI_API_KEY":"","CARTESIA_API_KEY":"synthetic"}):
            dlg._start()
            self.assertFalse(dlg._busy)
            self.assertEqual(dlg.status.text(),"OPENAI_API_KEY_MISSING")
        ink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
