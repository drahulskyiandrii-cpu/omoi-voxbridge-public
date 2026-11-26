"""Anthropic HTTP contract with fake transport and local cancellation."""
import json
import threading
import unittest
from unittest.mock import patch
try:
    import httpx
except ImportError:
    httpx = None

from voxbridge.analysis import AnalysisRequest
from voxbridge.analysis_providers import AnthropicAPI, ProviderFailure, execute_analysis
if httpx is not None:
    from voxbridge.cloud_http import DeadlineTransport


class FakeTransport(httpx.AsyncBaseTransport if httpx is not None else object):
    def __init__(self, body=None, *, status=200, fail_close=False, encoding=None):
        self.body = body if body is not None else {"content": [{"type": "text", "text": "synthetic answer"}]}
        self.status = status
        self.fail_close = fail_close
        self.encoding = encoding
        self.requests = []
        self.closed = 0

    async def handle_async_request(self, request):
        self.requests.append((request.url, dict(request.headers), json.loads(await request.aread())))
        headers = {"content-encoding": self.encoding} if self.encoding else {}
        # Real async stream, not httpx's pre-consumed in-memory response.
        class Body(httpx.AsyncByteStream):
            async def __aiter__(inner):
                yield json.dumps(self.body).encode("utf-8")
        return httpx.Response(self.status, headers=headers, stream=Body())

    async def aclose(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("private cleanup detail")


@unittest.skipUnless(httpx is not None, "cloud HTTPX extra required")
class TestProvider(unittest.TestCase):
    def provider(self, transport):
        p = AnthropicAPI("synthetic-model", api_key="synthetic-key")
        client = httpx.Client(transport=DeadlineTransport(5, max_bytes=64000, transport=transport),
                              trust_env=False, follow_redirects=False)
        return p, patch.object(p, "_client", return_value=client)

    def test_model_requires_configuration(self):
        with self.assertRaises(ProviderFailure):
            AnthropicAPI("", api_key="test")

    def test_api_payload_and_fixed_endpoint(self):
        transport = FakeTransport()
        p, factory = self.provider(transport)
        with factory:
            self.assertEqual(p.generate("s","u"), "synthetic answer")
        url, headers, body = transport.requests[0]
        self.assertEqual(str(url), AnthropicAPI.URL)
        self.assertEqual(headers["x-api-key"], "synthetic-key")
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(body["messages"], [{"role": "user", "content": "u"}])
        self.assertEqual(body["system"], "s")
        self.assertEqual(transport.closed, 1)

    def test_precancel_and_consent_never_open_client(self):
        p = AnthropicAPI("synthetic-model", api_key="synthetic-key")
        stop = threading.Event()
        stop.set()
        with patch.object(p, "_client") as factory:
            with self.assertRaisesRegex(ProviderFailure, "CLOUD_APPROVAL_REQUIRED"):
                execute_analysis(AnalysisRequest("synthetic"), p)
            with self.assertRaisesRegex(ProviderFailure, "ANTHROPIC_CANCELLED"):
                p.generate("s", "u", cancel_event=stop)
            factory.assert_not_called()

    def test_invalid_results_and_http_status(self):
        for transport, code in (
            (FakeTransport({"content": None}), "LLM_INVALID_RESULT"),
            (FakeTransport(status=302), "LLM_HTTP_ERROR"),
            (FakeTransport(encoding="gzip"), "LLM_INVALID_ENCODING"),
            (FakeTransport({"content": [{"type":"text", "text":"x"*70000}]}), "ANTHROPIC_RESPONSE_TOO_LARGE"),
        ):
            with self.subTest(code=code):
                p, factory = self.provider(transport)
                with factory, self.assertRaisesRegex(ProviderFailure, "^"+code+"$"):
                    p.generate("s","u")
                self.assertEqual(len(transport.requests),1)
                self.assertEqual(transport.closed,1)

    def test_cleanup_failure_is_sticky(self):
        transport = FakeTransport(fail_close=True)
        p, factory = self.provider(transport)
        with factory as create:
            for _ in range(2):
                with self.assertRaisesRegex(ProviderFailure, "^PROVIDER_CLEANUP_FAILED$"):
                    p.generate("s","u")
            create.assert_called_once()
        self.assertEqual(transport.closed,1)
