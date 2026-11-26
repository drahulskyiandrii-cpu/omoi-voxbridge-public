"""No-network Cartesia SSE PCM contract tests."""
from __future__ import annotations

import base64
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from voxbridge.analysis_providers import ProviderFailure
from voxbridge.cloud_providers import CartesiaTTSProvider
from voxbridge.tts_stream import BoundedPCMBuffer, stream_to_callback, validate_tts_text


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.closed = False
    def __iter__(self):
        return iter(self.events)
    def close(self):
        self.closed = True


def chunk(pcm: bytes):
    return NS(type="chunk", data=base64.b64encode(pcm).decode("ascii"), status_code=200)


def done():
    return NS(type="done", status_code=200)


class StreamTests(unittest.TestCase):
    def make(self, events):
        client = Mock()
        stream = FakeStream(events)
        client.tts.generate_sse.return_value = stream
        provider = CartesiaTTSProvider(voice_id="synthetic-uuid", client=client)
        return provider, client, stream

    def test_sse_pcm_and_options(self):
        provider, client, stream = self.make(
            [chunk(b"\x01\x00\x02\x00"), NS(type="timestamps"), chunk(b"\x03\x00"), done()]
        )
        received = []
        count = stream_to_callback(provider, "Привет", "ru", received.append)
        self.assertEqual(count, 6)
        self.assertEqual(received, [b"\x01\x00\x02\x00", b"\x03\x00"])
        self.assertTrue(stream.closed)
        kwargs = client.tts.generate_sse.call_args.kwargs
        self.assertEqual(kwargs["model_id"], "sonic-latest")
        self.assertEqual(kwargs["voice"], "synthetic-uuid")
        self.assertEqual(kwargs["transcript"], "Привет")
        self.assertEqual(kwargs["language"], "ru")
        self.assertEqual(kwargs["output_format"],
            {"container":"raw","encoding":"pcm_s16le","sample_rate":24000})

    def test_explicit_cancellation_prevents_network(self):
        provider, client, _ = self.make([chunk(b"\x00\x00"), done()])
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ProviderFailure, "CARTESIA_CANCELLED"):
            stream_to_callback(provider, "Тест", "ru", lambda *_: None, stop)
        client.tts.generate_sse.assert_not_called()

    def test_cancel_during_generation_discards_later_chunks(self):
        stop = threading.Event()
        provider, client, stream = self.make([chunk(b"\x01\x00"), chunk(b"\x02\x00"), done()])
        received = []
        def accept(pcm):
            received.append(pcm)
            stop.set()
        with self.assertRaisesRegex(ProviderFailure, "CARTESIA_CANCELLED"):
            stream_to_callback(provider, "Тест", "ru", accept, stop)
        self.assertEqual(received, [b"\x01\x00"])
        self.assertTrue(stream.closed)

    def test_validation_blocks_network(self):
        provider, client, _ = self.make([done()])
        for text, language in [("", "ru"), ("a" * 2001, "ru"), ("Hallo", "wrong"), ("x" + chr(0), "ru"), (chr(0xd800), "ru")]:
            with self.subTest(textlen=len(text), language=language):
                with self.assertRaises(ProviderFailure):
                    stream_to_callback(provider, text, language, lambda _: None)
        client.tts.generate_sse.assert_not_called()

    def test_rejects_malformed_and_unfinished_events(self):
        cases = [
            [NS(type="chunk", data="***", status_code=200), done()],
            [NS(type="chunk", data=base64.b64encode(b"\x00").decode(), status_code=200), done()],
            [NS(type="chunk", data="a" * 300000, status_code=200), done()],
            [chunk(b"\x01\x00")],
            [NS(type="error", status_code=400)],
            [done()],
        ]
        for events in cases:
            with self.subTest(events=events):
                provider, _, stream = self.make(events)
                with self.assertRaises(ProviderFailure):
                    stream_to_callback(provider, "Тест", "ru", lambda *_: None)
                self.assertTrue(stream.closed)

    def test_api_error_does_not_leak_secret(self):
        provider, client, _ = self.make([])
        class RateLimitError(Exception):
            pass
        client.tts.generate_sse.side_effect = RateLimitError("secret or user text")
        with self.assertRaises(ProviderFailure) as ctx:
            stream_to_callback(provider, "Тест", "ru", lambda *_: None)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(str(ctx.exception), "CARTESIA_STREAM_RATE_LIMIT")

    def test_buffer_bound_and_read(self):
        q = BoundedPCMBuffer(8)
        q.push(b"\x01\x00\x02\x00")
        self.assertEqual(q.peek(3), b"\x01\x00")
        q.discard(2)
        q.push(b"\x03\x00\x04\x00")
        self.assertEqual(len(q), 6)
        with self.assertRaisesRegex(ProviderFailure, "PCM_BACKPRESSURE"):
            q.push(b"\x05\x00\x06\x00")
        self.assertEqual(q.peek(10), b"\x02\x00\x03\x00\x04\x00")
        q.clear()
        self.assertEqual(len(q), 0)


if __name__ == "__main__":
    unittest.main()
