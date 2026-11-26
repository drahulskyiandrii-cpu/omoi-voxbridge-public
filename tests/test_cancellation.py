"""Cancellation and GPU no-load rules using mocks only."""
import unittest
from unittest.mock import patch
from voxbridge.audio import pcm_to_wav
from voxbridge.stt import MiniOneShotSTT, SpeechFailure

WAV = pcm_to_wav(b"\x00\x01" * 4800)

class TestCancel(unittest.TestCase):
    def test_pre_cancel_blocks_gpu_probe(self):
        model = MiniOneShotSTT()
        model.cancel()
        with patch("voxbridge.stt.require_free_memory") as gpu:
            with self.assertRaisesRegex(SpeechFailure, "STT_CANCELLED"):
                model.transcribe(WAV, "de")
            gpu.assert_not_called()

    def test_no_gpu_for_unsupported_language(self):
        with patch("voxbridge.stt.require_free_memory") as gpu:
            with self.assertRaisesRegex(SpeechFailure, "DE_RU_ONLY"):
                MiniOneShotSTT().transcribe(WAV, "en")
            gpu.assert_not_called()
