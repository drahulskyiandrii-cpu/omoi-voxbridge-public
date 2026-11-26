"""Deterministic CPU-only cloud contract tests. Never contact external APIs."""
import io
import threading
import unittest
import wave
from types import SimpleNamespace as NS
from unittest.mock import Mock, MagicMock, patch

from voxbridge.analysis import AnalysisRequest
from voxbridge.analysis_providers import ProviderFailure, execute_analysis
from voxbridge.cloud_providers import OpenAIResponses, OpenAIFileSTT, CartesiaTTSProvider, close_provider
from voxbridge.stt import SpeechFailure


def wav_silence(rate=16000):
    file = io.BytesIO()
    with wave.open(file, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x00" * 8000)
    return file.getvalue()


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.closed = True
    def __iter__(self):
        return iter(self.events)
    def get_final_response(self):
        return NS(status="completed", output_text="".join(e.delta for e in self.events if e.type == "response.output_text.delta"), usage=NS(input_tokens=40, output_tokens=20))


class TestOpenAIResponses(unittest.TestCase):
    def make(self, effort="low"):
        client = Mock()
        client.responses.create.return_value = NS(
            status="completed", output_text="VLAN = virtuelles LAN",
            usage=NS(input_tokens=1000, output_tokens=200),
        )
        return OpenAIResponses(client=client, effort=effort), client

    def test_fast_and_deep_payload(self):
        for effort in ("low", "high"):
            with self.subTest(effort=effort):
                provider, client = self.make(effort)
                self.assertEqual(provider.generate("system", "user"), "VLAN = virtuelles LAN")
                kwargs = client.responses.create.call_args.kwargs
                self.assertEqual(kwargs["model"], "gpt-6-luna")
                self.assertEqual(kwargs["reasoning"], {"effort": effort})
                self.assertEqual(kwargs["instructions"], "system")
                self.assertEqual(kwargs["input"], "user")
                self.assertFalse(kwargs["store"])
                self.assertAlmostEqual(provider.last_usage.estimated_usd, 0.0002)

    def test_cloud_consent_blocks_network(self):
        provider, client = self.make()
        with self.assertRaisesRegex(ProviderFailure, "CLOUD_APPROVAL_REQUIRED"):
            execute_analysis(AnalysisRequest("Erkläre VLAN"), provider)
        client.responses.create.assert_not_called()

    def test_approved_analysis(self):
        provider, client = self.make()
        result = execute_analysis(AnalysisRequest("Was bedeutet VLAN?"), provider,
                                  cloud_approved=True)
        self.assertIn("VLAN", result.answer)
        client.responses.create.assert_called_once()

    def test_invalid_model_effort_and_input(self):
        with self.assertRaisesRegex(ProviderFailure, "MODEL_ID"):
            OpenAIResponses(model="invalid id", client=Mock())
        with self.assertRaisesRegex(ProviderFailure, "EFFORT"):
            OpenAIResponses(effort="danger", client=Mock())
        provider, client = self.make()
        with self.assertRaisesRegex(ProviderFailure, "INPUT_TOO_LARGE"):
            provider.generate("s", "x" * 31000)
        client.responses.create.assert_not_called()

    def test_stream_and_usage(self):
        provider, client = self.make()
        stream = Stream([NS(type="response.output_text.delta", delta="Gut"),
                         NS(type="response.output_text.delta", delta=" erklärt")])
        client.responses.stream.return_value = stream
        chunks = []
        self.assertEqual(provider.generate_stream("s", "u", chunks.append), "Gut erklärt")
        self.assertEqual(chunks, ["Gut", " erklärt"])
        self.assertTrue(stream.closed)
        self.assertEqual(provider.last_usage.output_tokens, 20)

    def test_stream_cancel_without_network(self):
        provider, client = self.make()
        event = threading.Event()
        event.set()
        stream = Stream([NS(type="response.output_text.delta", delta="x")])
        client.responses.stream.return_value = stream
        with self.assertRaisesRegex(ProviderFailure, "CANCELLED"):
            provider.generate_stream("s", "u", lambda *_: None, event)
        client.responses.stream.assert_not_called()
        self.assertFalse(stream.closed)

    def test_owned_sdk_is_closed_once_borrowed_client_is_not(self):
        owned = Mock()
        with patch("voxbridge.cloud_providers._openai_client", return_value=owned):
            with OpenAIResponses(api_key="synthetic") as provider:
                self.assertIs(provider.client, owned)
            provider.close()
            owned.close.assert_called_once()
        borrowed = Mock()
        OpenAIResponses(client=borrowed).close()
        borrowed.close.assert_not_called()

    def test_failed_owned_close_is_sticky_and_context_reports_it(self):
        owned = Mock()
        owned.close.side_effect = OSError("private")
        with patch("voxbridge.cloud_providers._openai_client", return_value=owned):
            with self.assertRaisesRegex(ProviderFailure, "^PROVIDER_CLEANUP_FAILED$"):
                with OpenAIResponses(api_key="synthetic") as provider:
                    pass
            self.assertEqual(close_provider(provider), "PROVIDER_CLEANUP_FAILED")
            owned.close.assert_called_once()

    def test_close_provider_returns_sanitized_error_not_exception(self):
        failed = Mock()
        failed.close.side_effect = RuntimeError("private")
        self.assertEqual(close_provider(failed), "PROVIDER_CLEANUP_FAILED")
        self.assertEqual(close_provider(None), "")
        self.assertEqual(close_provider(object()), "")

    def test_error_is_sanitized(self):
        provider, client = self.make()
        class RateLimitError(Exception):
            pass
        client.responses.create.side_effect = RateLimitError("secret content here")
        with self.assertRaises(ProviderFailure) as error:
            provider.generate("s", "u")
        self.assertEqual(str(error.exception), "OPENAI_RATE_LIMIT")


class TestOpenAISTT(unittest.TestCase):
    def test_transcription_languages_and_payload(self):
        c = Mock()
        c.audio.transcriptions.create.return_value = NS(text="Guten Morgen")
        stt = OpenAIFileSTT(client=c)
        self.assertEqual(stt.transcribe(wav_silence(), "de"), "Guten Morgen")
        kwargs = c.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-transcribe")
        self.assertEqual(kwargs["languages"], ["de"])
        self.assertEqual(kwargs["file"][0], "recording.wav")
        self.assertEqual(stt.transcribe(wav_silence(), "auto"), "Guten Morgen")
        self.assertEqual(c.audio.transcriptions.create.call_args.kwargs["languages"],
                         ["de", "ru", "en"])

    def test_rejects_truncated_audio_before_network(self):
        client = Mock()
        with self.assertRaises(SpeechFailure):
            OpenAIFileSTT(client=client).transcribe(wav_silence()[:-2], "de")
        client.audio.transcriptions.create.assert_not_called()

    def test_rejects_input_before_network(self):
        c = Mock()
        stt = OpenAIFileSTT(client=c)
        with self.assertRaises(SpeechFailure):
            stt.transcribe(b"not wav", "de")
        with self.assertRaises(SpeechFailure):
            stt.transcribe(wav_silence(), "xx")
        c.audio.transcriptions.create.assert_not_called()


class TestCartesia(unittest.TestCase):
    def test_wav_generation_mock(self):
        import wave
        pcm = wav_silence(24000)
        client = MagicMock()
        response = client.tts.with_streaming_response.generate.return_value.__enter__.return_value
        response.iter_bytes.return_value = [pcm]
        provider = CartesiaTTSProvider(voice_id="example-voice", client=client)
        self.assertEqual(provider.synthesize("Hallo Welt", "de"), pcm)
        kwargs = client.tts.with_streaming_response.generate.call_args.kwargs
        self.assertEqual(kwargs["model_id"], "sonic-latest")
        self.assertEqual(kwargs["voice"], "example-voice")
        self.assertEqual(kwargs["language"], "de")

    def test_voice_required_and_validation(self):
        with self.assertRaisesRegex(ProviderFailure, "VOICE_ID_MISSING"):
            CartesiaTTSProvider(voice_id="", client=Mock())
        provider = CartesiaTTSProvider(voice_id="example-voice", client=Mock())
        with self.assertRaisesRegex(ProviderFailure, "TEXT_LIMIT"):
            provider.synthesize("")
        with self.assertRaisesRegex(ProviderFailure, "INVALID_LANGUAGE"):
            provider.synthesize("hello", "xx")
        provider.client.tts.with_streaming_response.generate.assert_not_called()

    def test_bad_audio_rejected(self):
        client = MagicMock()
        client.tts.with_streaming_response.generate.return_value.__enter__.return_value.iter_bytes.return_value = [b"INVALID"]
        provider = CartesiaTTSProvider(voice_id="example-voice", client=client)
        with self.assertRaisesRegex(ProviderFailure, "INVALID_AUDIO"):
            provider.synthesize("Hallo")
