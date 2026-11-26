import tempfile
import unittest
from pathlib import Path
from voxbridge.config import Settings, load_settings, save_settings

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(Path(tmp) / "absent.json")
        self.assertEqual(s.hotkey, "Ctrl+Alt+Space")
        self.assertEqual(s.stt_backend, "openai_cloud")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "settings.json"
            s = Settings(language="ru", hotkey="Ctrl+Shift+D", stt_backend="mini_worker")
            save_settings(s, file)
            self.assertEqual(load_settings(file), s)

    def test_reject_invalid(self):
        for kwargs in ({"language": "zz"}, {"stt_backend": "cloud"},
                       {"stt_port": 80}, {"max_record_seconds": 121},
                       {"hotkey": "Space"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Settings(**kwargs).validate()

    def test_selection_hotkey_defaults_off_and_conflict_rejected(self):
        self.assertFalse(Settings().selection_hotkey_enabled)
        with self.assertRaisesRegex(ValueError, "SELECTION_HOTKEY_CONFLICT"):
            Settings(selection_hotkey="Ctrl+Alt+Space", selection_hotkey_enabled=True).validate()
        with self.assertRaises(ValueError):
            Settings(selection_hotkey_enabled=1).validate()

    def test_reject_unknown_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "settings.json"
            file.write_text('{"token":"secret"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(file)
