"""Caption preference persistence; isolated temp paths, never real user config."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxbridge.caption_preferences import (CaptionPreferences, load_caption_preferences,
                                           save_caption_preferences)
from voxbridge.config import Settings, save_settings


class CaptionPreferencesTests(unittest.TestCase):
    def test_roundtrip_without_consent_or_personal_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.json"
            prefs = CaptionPreferences(source="ru", target="de", translate=True,
                                       opacity=70, font_size=24, translation_limit=2)
            self.assertEqual(load_caption_preferences(path), CaptionPreferences())
            save_caption_preferences(prefs, path)
            self.assertEqual(load_caption_preferences(path), prefs)
            keys = set(json.loads(path.read_text()))
            for forbidden in ("audio_consent", "translation_consent", "device_id",
                              "transcript", "api_key", "voice_id"):
                self.assertNotIn(forbidden, keys)

    def test_legacy_and_local_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.json"
            path.write_text('{"translate":true}')
            self.assertEqual(load_caption_preferences(path).translation_engine, "cloud")
            prefs = CaptionPreferences(translate=True, translation_engine="gemma_cpu")
            save_caption_preferences(prefs, path)
            self.assertEqual(load_caption_preferences(path), prefs)
            with self.assertRaises(ValueError):
                CaptionPreferences(translation_engine="unknown").validate()

    def test_invalid_values_and_unknown_fields(self):
        for args in ({"source":"ru"}, {"translate":1}, {"seconds":True},
                     {"font_size":33}, {"delay":"bad"}, {"opacity":49}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                CaptionPreferences(**args).validate()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.json"
            path.write_text('{"audio_consent":true}')
            with self.assertRaises(ValueError):
                load_caption_preferences(path)

    def test_atomic_failure_keeps_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            save_settings(Settings(), path)
            prior = path.read_bytes()
            with patch("voxbridge.config.os.replace", side_effect=OSError("mock")):
                with self.assertRaises(OSError):
                    save_settings(Settings(language="ru"), path)
            self.assertEqual(path.read_bytes(), prior)
            self.assertEqual(list(Path(temp).iterdir()), [path])
