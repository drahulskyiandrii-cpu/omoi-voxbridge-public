"""Deterministic per-session cloud usage quotas; no external API calls."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from voxbridge.captions_core import CaptionCoordinator
from voxbridge.session_limits import (
    MAX_TRANSLATION_OUTPUT_TOKENS, SessionQuotaExceeded, TranslationQuota, RealtimeUsage,
)


def make_job(text, context=()):
    from voxbridge.captions_core import TranslationJob
    return TranslationJob(
        generation=1, sequence=1, source_language="de",
        target_language="ru", text=text,
        context=tuple(context), cancel_event=threading.Event(),
    )


class SessionQuotaTests(unittest.TestCase):
    def setUp(self):
        self.quota = TranslationQuota(max_calls=3, max_input_chars=512)
        self.stop = threading.Event()

    def test_request_reserved_before_api_not_refunded(self):
        self.assertTrue(self.quota.reserve(make_job("Hallo"), stop=self.stop))
        self.assertEqual(self.quota.calls_reserved, 1)
        self.assertEqual(self.quota.input_chars_reserved, 5)
        self.assertTrue(self.quota.reserve(make_job("Test"), stop=self.stop))
        self.assertTrue(self.quota.reserve(make_job("Wort"), stop=self.stop))
        self.assertFalse(self.quota.reserve(make_job("nochmals"), stop=self.stop))
        self.assertEqual(self.quota.calls_reserved, 3)
        self.assertLessEqual(MAX_TRANSLATION_OUTPUT_TOKENS, 256)

    def test_context_counts_toward_input_quota(self):
        q = TranslationQuota(max_calls=3, max_input_chars=512)
        self.assertTrue(q.reserve(make_job("T" * 20, ("C" * 100,)), stop=self.stop))
        self.assertEqual(q.input_chars_reserved, 120)
        self.assertFalse(q.reserve(make_job("X" * 400), stop=self.stop))
        self.assertEqual(q.calls_reserved, 1)

    def test_reject_oversized_single_prompt(self):
        job = make_job("x" * 2000, ("c" * 1999,))
        q = TranslationQuota(max_one_input_chars=500)
        self.assertFalse(q.reserve(job, stop=self.stop))
        self.assertEqual(q.calls_reserved, 0)

    def test_cancelled_session_blocks_all_future_cloud_calls(self):
        self.quota.reserve(make_job("test"), stop=self.stop)
        self.quota.close()
        with self.assertRaisesRegex(SessionQuotaExceeded, "SESSION_CANCELLED"):
            self.quota.reserve(make_job("new"), stop=self.stop)
        self.assertEqual(self.quota.calls_reserved, 1)

    def test_stop_event_and_cancelled_job(self):
        job = make_job("test")
        self.stop.set()
        with self.assertRaisesRegex(SessionQuotaExceeded, "SESSION_CANCELLED"):
            self.quota.reserve(job, stop=self.stop)
        self.stop.clear()
        job.cancel_event.set()
        with self.assertRaisesRegex(SessionQuotaExceeded, "SESSION_CANCELLED"):
            self.quota.reserve(job, stop=self.stop)

    def test_concurrent_quota_reservations_bounded(self):
        q = TranslationQuota(max_calls=12, max_input_chars=512)
        with ThreadPoolExecutor(max_workers=8) as executor:
            allowed = list(executor.map(
                lambda _: q.reserve(make_job("x" * 20), stop=self.stop),
                range(100),
            ))
        self.assertEqual(sum(allowed), 12)
        self.assertEqual(q.calls_reserved, 12)
        self.assertEqual(q.input_chars_reserved, 240)

    def test_reject_invalid_config_and_inputs(self):
        for kwargs in [
            {"max_calls": 0}, {"max_calls": True}, {"max_calls": 13},
            {"max_input_chars": 12001}, {"max_input_chars": 64},
            {"max_one_input_chars": 5000},
        ]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    TranslationQuota(**kwargs)
        with self.assertRaises(ValueError):
            self.quota.reserve("not a job", stop=self.stop)
        with self.assertRaises(ValueError):
            self.quota.reserve(make_job("test"), stop=None)


class RealtimeUsageTests(unittest.TestCase):
    def test_single_connection_and_no_refunds(self):
        usage = RealtimeUsage(1, max_commits=1)
        usage.claim(1)
        usage.reserve_audio(4800)
        usage.reserve_commit()
        snap = usage.snapshot()
        self.assertEqual(snap.attempted_connections, 1)
        self.assertEqual(snap.attempted_audio_seconds, 0.2)
        self.assertEqual(snap.attempted_commits, 1)
        with self.assertRaises(SessionQuotaExceeded):
            usage.claim(1)
        with self.assertRaises(SessionQuotaExceeded):
            usage.reserve_commit()

    def test_audio_cap_and_invalid_reservation(self):
        usage = RealtimeUsage(1)
        with self.assertRaises(SessionQuotaExceeded):
            usage.reserve_audio(4800)
        usage.claim(1)
        for _ in range(5):
            usage.reserve_audio(4800)
        with self.assertRaises(SessionQuotaExceeded):
            usage.reserve_audio(1)
        with self.assertRaises(ValueError):
            usage.reserve_audio(True)
        self.assertEqual(usage.snapshot().attempted_audio_samples, 24000)


if __name__ == "__main__":
    unittest.main()
