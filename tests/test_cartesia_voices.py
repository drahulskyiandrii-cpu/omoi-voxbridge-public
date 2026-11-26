"""Offline contract tests: only owned voice metadata, no audio requests."""
import threading
try:
    import httpx
except ImportError:
    httpx = None
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from voxbridge.cartesia_voices import list_owned_voices, valid_voice_id
from voxbridge.config import Settings
from voxbridge.analysis_providers import ProviderFailure

ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"


@unittest.skipUnless(httpx is not None, "cloud HTTPX extra required")
class CartesiaVoiceTests(unittest.TestCase):
    def test_filters_owner_only_and_sorts(self):
        events = []
        def handler(req):
            events.append(req)
            return httpx.Response(200, json={"data": [
                {"name": "Nika", "id": ID1, "language": "ru", "is_owner": True},
                {"name": "Vlada", "id": ID2, "language": "ru", "is_owner": True},
                {"name": "Foreign", "id": ID1, "language": "ru", "is_owner": False},
                {"name": "Missing owner", "id": "123", "language": "ru"},
            ], "has_more": False})
        voices = list_owned_voices(api_key="test-only", transport=httpx.MockTransport(handler))
        self.assertEqual([v.name for v in voices], ["Nika", "Vlada"])
        self.assertEqual(len(events), 1)
        request = events[0]
        self.assertEqual(urlsplit(str(request.url)).hostname, "api.cartesia.ai")
        self.assertEqual(parse_qs(urlsplit(str(request.url)).query)["is_owner"], ["true"])
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer test-only")
        self.assertEqual(request.headers["Cartesia-version"], "2026-08-14")
        self.assertEqual(str(request.url).count("tts"), 0)

    def test_pagination(self):
        seen = []
        def handler(req):
            qs = parse_qs(urlsplit(str(req.url)).query)
            seen.append(qs)
            if "starting_after" not in qs:
                return httpx.Response(200, json={"data":[{"name":"Nika","id":ID1,"language":"ru","is_owner":True}],
                                 "has_more":True,"next_page":ID1})
            return httpx.Response(200, json={"data":[{"name":"Vlada","id":ID2,"language":"ru","is_owner":True}],
                             "has_more":False})
        self.assertEqual(len(list_owned_voices(api_key="x", transport=httpx.MockTransport(handler))), 2)
        self.assertEqual(seen[1]["starting_after"], [ID1])

    def test_repeated_cursor_rejected_before_third_request(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"data": [], "has_more": True, "next_page": ID1})
        with self.assertRaisesRegex(ProviderFailure, "INVALID_CURSOR"):
            list_owned_voices(api_key="x", transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 2)

    def test_missing_key_stops_before_http(self):
        def no_net(*args, **kwargs):
            self.fail("must not call network")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ProviderFailure, "API_KEY_MISSING"):
                list_owned_voices(transport=httpx.MockTransport(no_net))

    def test_auth_error_is_sanitized(self):
        def auth(req):
            return httpx.Response(401, content=b"secret private")
        with self.assertRaisesRegex(ProviderFailure, "CARTESIA_VOICES_AUTH") as ctx:
            list_owned_voices(api_key="x", transport=httpx.MockTransport(auth))
        self.assertNotIn("secret", str(ctx.exception))

    def test_invalid_input_is_skipped(self):
        def handler(req):
            return httpx.Response(200, json={"data":[{"id":"bad","name":"Voice","language":"ru","is_owner":True}],
                             "has_more":False})
        self.assertEqual(list_owned_voices(api_key="x", transport=httpx.MockTransport(handler)), [])

    def test_cancel_between_pages_prevents_second_request(self):
        cancel = threading.Event()
        calls = []
        def handler(req):
            calls.append(req)
            cancel.set()
            return httpx.Response(200, json={"data": [], "has_more": True, "next_page": ID1})
        with self.assertRaisesRegex(ProviderFailure, "CARTESIA_VOICES_CANCELLED"):
            list_owned_voices(api_key="x", cancel_event=cancel,
                              transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 1)

    def test_pre_cancel_does_not_contact_network(self):
        cancel = threading.Event()
        cancel.set()
        def handler(req):
            self.fail("unexpected network")
        with self.assertRaisesRegex(ProviderFailure, "CARTESIA_VOICES_CANCELLED"):
            list_owned_voices(api_key="x", cancel_event=cancel,
                              transport=httpx.MockTransport(handler))

    def test_rejects_redirect_without_following(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        with self.assertRaisesRegex(ProviderFailure, "HTTP_ERROR"):
            list_owned_voices(api_key="x", transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 1)

    def test_rejects_missing_pagination_flag_and_invalid_json(self):
        for reply in (httpx.Response(200, json={"data": []}),
                      httpx.Response(200, content=b"{bad")):
            with self.subTest(reply=reply):
                with self.assertRaisesRegex(ProviderFailure, "INVALID_RESPONSE"):
                    list_owned_voices(api_key="x", transport=httpx.MockTransport(lambda req: reply))

    def test_rejects_oversized_metadata(self):
        with self.assertRaisesRegex(ProviderFailure, "RESPONSE_TOO_LARGE"):
            list_owned_voices(api_key="x", transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=b"x"*150001)))

    def test_rejects_compressed_metadata(self):
        # Invalid compressed content must not even be decoded.
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError("must not read body")
        with self.assertRaisesRegex(ProviderFailure, "INVALID_RESPONSE"):
            list_owned_voices(api_key="x", transport=httpx.MockTransport(
                lambda req: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Body())))

    def test_client_cleanup_failure_has_stable_code(self):
        from voxbridge.cartesia_voices import VOICE_CLEANUP_FAILED
        class BrokenClose(httpx.MockTransport):
            def close(self):
                raise OSError("private detail")
        for status in (200, 401):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ProviderFailure, "^"+VOICE_CLEANUP_FAILED+"$"):
                    list_owned_voices(api_key="synthetic", transport=BrokenClose(
                        lambda req: httpx.Response(status, json={"data": [], "has_more": False})))

    def test_settings_voice_id_stays_local(self):
        with self.assertRaisesRegex(ValueError, "INVALID_CARTESIA_VOICE_ID"):
            Settings(cartesia_voice_id="../not-a-voice").validate()
        self.assertTrue(valid_voice_id(ID1))
        self.assertFalse(valid_voice_id("not a uuid"))
        self.assertEqual(Settings(cartesia_voice_id=ID1).validate().cartesia_voice_id, ID1)


if __name__ == "__main__":
    unittest.main()
