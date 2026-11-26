"""Optional CPU/Qt smoke: owned voice picker does not make any API calls."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from voxbridge.voice_ui import VoiceDialog
except ImportError:
    QApplication = None

from voxbridge.cartesia_voices import OwnedVoice
from voxbridge.config import Settings

NIKA_ID = "33333333-3333-4333-8333-333333333333"
KOSTYA_ID = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(QApplication is not None, "PySide6 not installed in this interpreter")
class TestVoiceDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_own_voice_picker_with_fake_data(self):
        fetcher = Mock(side_effect=AssertionError("network never called"))
        dialog = VoiceDialog(fetcher=fetcher)
        fake_voices = [
            OwnedVoice("Kostya", KOSTYA_ID, "ru"),
            OwnedVoice("Nika", NIKA_ID, "ru"),
        ]
        with patch("voxbridge.voice_ui.load_settings", return_value=Settings()):
            dialog._worker_generation = 0
            dialog._loaded(0, fake_voices, "")
            self.assertEqual(dialog.choices.count(), 2)
            self.assertEqual(dialog.choices.currentData(), NIKA_ID)
            self.assertTrue(dialog.save.isEnabled())
            with patch("voxbridge.voice_ui.save_settings") as save:
                dialog._save()
                saved = save.call_args.args[0]
                self.assertEqual(saved.cartesia_voice_id, NIKA_ID)
        fetcher.assert_not_called()
        dialog.close()


if __name__ == "__main__":
    unittest.main()
