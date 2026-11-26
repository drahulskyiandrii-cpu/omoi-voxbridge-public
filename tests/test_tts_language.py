"""Speech language planning uses the text, not the STT setting."""
import unittest
from voxbridge.tts_language import plan_speech
from voxbridge.analysis_providers import ProviderFailure


class SpeechLanguageTests(unittest.TestCase):
    def test_russian_with_german_fallback(self):
        text = "\u041f\u0440\u0438\u0432\u0435\u0442, CUDA \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442."
        self.assertEqual(plan_speech(text, fallback="de", local=True), ((text,"ru"),))

    def test_german_with_russian_fallback(self):
        text = "Der Router ist bereit."
        self.assertEqual(plan_speech(text, fallback="ru"), ((text,"de"),))

    def test_mixed_sentences_preserve_order_and_words(self):
        text = "Guten Morgen! \u041f\u0440\u0438\u0432\u0435\u0442! Der Router ist bereit."
        plan = plan_speech(text, fallback="de", local=True)
        self.assertEqual([lang for _, lang in plan], ["de", "ru", "de"])
        self.assertEqual("".join(part for part, _ in plan), text)

    def test_english_and_manual_choice(self):
        self.assertEqual(plan_speech("This is a test.", fallback="de")[0][1], "en")
        self.assertEqual(plan_speech("This is a test.", "de")[0][1], "de")

    def test_long_russian_chunking_keeps_text(self):
        text = ("\u041f\u0440\u0438\u0432\u0435\u0442, CUDA 123. " * 200) + "\u041a\u043e\u043d\u0435\u0446."
        plan = plan_speech(text, local=True)
        self.assertGreater(len(plan), 1)
        self.assertTrue(all(lang == "ru" and len(chunk) <= 280 for chunk, lang in plan))
        self.assertEqual("".join(chunk for chunk, _ in plan), text)

    def test_invalid_before_synthesis(self):
        for text in ("", "a"*12001, "bad\x00"):
            with self.subTest(text_length=len(text)):
                with self.assertRaises(ProviderFailure):
                    plan_speech(text, local=True)
