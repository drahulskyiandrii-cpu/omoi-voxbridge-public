"""Qt Vision preview tests: no real screenshot, no network, no local model."""
import os
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPixmap, QColor
    from voxbridge.vision_ui import VisionDialog, RegionOverlay
except ImportError:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "PySide6 not available")
class VisionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def fake_region(self):
        pixmap = QPixmap(140, 90)
        pixmap.fill(QColor("#223344"))
        return pixmap

    def test_local_vision_never_constructs_cloud_provider(self):
        from pathlib import Path
        from dataclasses import replace
        from voxbridge.config import Settings
        from voxbridge.local_llm import LocalModel
        model = LocalModel(Path("synthetic.gguf"), Path("synthetic-mmproj.gguf"))
        runtime = Mock()
        runtime.stream.return_value = "VLAN 20"
        factory = Mock(side_effect=AssertionError("cloud"))
        dialog = VisionDialog(provider_factory=factory, local_runtime=runtime, local_models=(model,),
                              preferences=replace(Settings(), vision_provider="local"))
        try:
            dialog._accept_pixmap(self.fake_region())
            dialog._analyze()
            end = time.monotonic()+3
            while dialog._busy and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.01)
            self.assertEqual(dialog.output.toPlainText(), "VLAN 20")
            self.assertTrue(runtime.stream.call_args.kwargs["vision"])
            factory.assert_not_called()
        finally:
            dialog.close()

    def test_ocr_read_uses_no_cloud_or_llm(self):
        from dataclasses import replace
        from unittest.mock import patch
        from voxbridge.config import Settings
        runtime = Mock()
        factory = Mock(side_effect=AssertionError("cloud"))
        spoken = Mock()
        dialog = VisionDialog(provider_factory=factory, local_runtime=runtime,
                               preferences=replace(Settings(), vision_provider="local_ocr"),
                               on_read=spoken)
        try:
            dialog._accept_pixmap(self.fake_region())
            with patch("voxbridge.local_llm.recognize_png", return_value="VLAN 20"):
                dialog._read_preview()
                end = time.monotonic()+3
                while dialog._busy and time.monotonic()<end:
                    self.app.processEvents()
                    time.sleep(.01)
            self.assertEqual(dialog.output.toPlainText(), "VLAN 20")
            spoken.assert_called_once_with("VLAN 20")
            runtime.stream.assert_not_called()
            factory.assert_not_called()
        finally:
            dialog.close()

    def test_dialog_not_capturing_or_uploading_on_open(self):
        factory = Mock(side_effect=AssertionError("must not send"))
        dialog = VisionDialog(provider_factory=factory)
        self.assertIsNone(dialog._png)
        self.assertFalse(dialog.analyze.isEnabled())
        self.assertFalse(dialog.cancel.isEnabled())
        self.assertGreaterEqual(dialog.screen_choice.count(), 1)
        factory.assert_not_called()
        dialog.close()

    def test_region_overlay_only_selects_by_user(self):
        screen = self.app.primaryScreen()
        overlay = RegionOverlay(screen)
        signals = Mock()
        overlay.selected.connect(signals)
        self.assertIsNone(overlay._start)
        self.assertTrue(overlay._region.isNull())
        signals.assert_not_called()
        overlay.close()

    def test_preview_sends_only_after_fresh_consent(self):
        provider = Mock()
        provider.analyze_png.return_value = "VLAN — это виртуальная сеть."
        factory = Mock(return_value=provider)
        dialog = VisionDialog(provider_factory=factory)
        dialog._accept_pixmap(self.fake_region())
        self.assertIsNotNone(dialog._png)
        self.assertTrue(dialog.analyze.isEnabled())
        self.assertFalse(dialog.consent.isChecked())
        dialog._analyze()
        self.assertIn("Cloud-Zustimmung", dialog.message.text())
        factory.assert_not_called()
        selected_png = dialog._png
        dialog.question.setText("Что изображено?")
        dialog.consent.setChecked(True)
        dialog._analyze()
        end = time.monotonic() + 3
        while dialog._busy and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(dialog._busy)
        provider.analyze_png.assert_called_once_with(selected_png, "Что изображено?")
        self.assertIn("VLAN", dialog.output.toPlainText())
        self.assertFalse(dialog.consent.isChecked())
        dialog.close()
        self.assertIsNone(dialog._png)
        self.assertFalse(dialog.analyze.isEnabled())

    def test_question_edit_invalidates_consent(self):
        dialog = VisionDialog(provider_factory=Mock())
        dialog._accept_pixmap(self.fake_region())
        dialog.consent.setChecked(True)
        dialog.question.setText("New question")
        self.assertFalse(dialog.consent.isChecked())
        dialog.close()

    def test_close_fences_delayed_screen_grab(self):
        from unittest.mock import patch
        from PySide6.QtCore import QRect
        dialog = VisionDialog(provider_factory=Mock())
        screen = Mock()
        dialog._overlay = Mock()
        callbacks = []
        with patch("voxbridge.vision_ui.QTimer.singleShot",
                   side_effect=lambda _, callback: callbacks.append(callback)):
            dialog._picked(screen, QRect(0, 0, 100, 100))
        dialog.close()
        callbacks[0]()
        screen.grabWindow.assert_not_called()
        self.assertIsNone(dialog._png)

    def test_cancel_ignores_late_response(self):
        provider = Mock()
        dialog = VisionDialog(provider_factory=lambda: provider)
        dialog._accept_pixmap(self.fake_region())
        dialog._busy = True
        dialog._generation = 3
        dialog._cancel()
        self.assertEqual(dialog._generation, 4)
        dialog._complete(3, "Do not show this", "")
        self.assertNotIn("Do not show this", dialog.output.toPlainText())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
