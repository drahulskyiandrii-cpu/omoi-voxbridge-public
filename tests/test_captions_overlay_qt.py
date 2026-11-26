"""Offscreen preview tests; no real audio, paid inference or screen capture."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from voxbridge.captions_overlay import CaptionsOverlay, format_caption, populate_synthetic_demo
except ImportError:
    QApplication = None

from voxbridge.captions_core import CaptionCoordinator


@unittest.skipUnless(QApplication is not None, "PySide6 not installed")
class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_two_lanes_and_pending_markers(self):
        core = CaptionCoordinator()
        generation = core.begin()
        core.final(generation, 0, "Eine neue IP-Adresse.")
        view = CaptionsOverlay(core)
        view.refresh()
        self.assertIn("Eine neue IP-Adresse.", view.de_label.text())
        self.assertIn("…", view.ru_label.text())
        job = core.take_translation()
        core.finish_translation(job, "Новый IP-адрес.")
        view.refresh()
        self.assertIn("Новый IP-адрес.", view.ru_label.text())
        self.assertEqual(view.de_label.textFormat(), Qt.PlainText)
        self.assertEqual(view.ru_label.textFormat(), Qt.PlainText)
        view.close()
        self.assertFalse(core.snapshot().active)
        self.assertEqual(core.snapshot().lines, ())

    def test_partial_is_visibly_distinct(self):
        core = CaptionCoordinator()
        generation = core.begin()
        core.partial(generation, 0, 2, "VLAN ist")
        original, ru = format_caption(core.snapshot())
        self.assertIn("≈ VLAN ist", original)
        self.assertIn("Русский", ru)
        self.assertEqual(core.snapshot().pending_count, 0)

    def test_original_only_hides_translation(self):
        core = CaptionCoordinator()
        gen = core.begin(target_language=None)
        core.final(gen, 0, "Kein Cloud-Zugriff.")
        overlay = CaptionsOverlay(core)
        overlay.refresh()
        self.assertFalse(overlay.ru_label.isVisible())
        self.assertIn("Kein Cloud-Zugriff.", overlay.de_label.text())
        overlay.close()

    def test_explicit_synthetic_demo_has_two_complete_lanes(self):
        core = CaptionCoordinator()
        populate_synthetic_demo(core)
        overlay = CaptionsOverlay(core)
        overlay.refresh()
        self.assertEqual(len(core.snapshot().lines), 2)
        self.assertTrue(all(row.status == "complete" for row in core.snapshot().lines))
        self.assertIn("VLAN", overlay.de_label.text())
        self.assertIn("DHCP", overlay.de_label.text())
        self.assertIn("VLAN", overlay.ru_label.text())
        self.assertIn("DHCP", overlay.ru_label.text())
        populate_synthetic_demo(core)  # reopen demo resets, never duplicates
        self.assertEqual(len(core.snapshot().lines), 2)
        overlay.close()

    def test_display_modes_font_rows_and_opacity(self):
        core = CaptionCoordinator()
        gen = core.begin(source_language="ru", target_language="de")
        for i in range(4):
            core.final(gen, i, f"Original {i}")
        core.partial(gen, 4, 0, "Tentative")
        overlay = CaptionsOverlay(core)
        overlay.show()
        overlay.set_display_options("translation", 24, 2, 75)
        self.assertTrue(overlay.de_label.isHidden())
        self.assertFalse(overlay.ru_label.isHidden())
        self.assertEqual(overlay.de_label.font().pixelSize(), 24)
        self.assertAlmostEqual(overlay.windowOpacity(), 0.75, delta=0.01)
        self.assertNotIn("Original 1", overlay.de_label.text())
        self.assertIn("Original 2", overlay.de_label.text())
        self.assertIn("Tentative", overlay.de_label.text())
        overlay.set_display_options("original", 18, 3, 100)
        self.assertFalse(overlay.de_label.isHidden())
        self.assertTrue(overlay.ru_label.isHidden())
        overlay.hide()
        self.assertFalse(overlay._timer.isActive())
        overlay.close()

    def test_translation_only_without_target_falls_back_to_original(self):
        core = CaptionCoordinator()
        gen = core.begin(target_language=None)
        core.final(gen, 0, "Original")
        overlay = CaptionsOverlay(core)
        overlay.set_display_options("translation", 15, 2, 100)
        self.assertFalse(overlay.de_label.isHidden())
        self.assertTrue(overlay.ru_label.isHidden())
        overlay.close()

    def test_untrusted_markup_rendered_only_as_text(self):
        core = CaptionCoordinator()
        gen = core.begin()
        core.final(gen, 0, "<script>not executable</script>")
        overlay = CaptionsOverlay(core)
        overlay.refresh()
        self.assertEqual(overlay.de_label.textFormat(), Qt.PlainText)
        self.assertIn("<script>", overlay.de_label.text())
        overlay.close()


if __name__ == "__main__":
    unittest.main()
