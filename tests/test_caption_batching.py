import unittest

from voxbridge.caption_batching import FinalFragment, SentenceBatcher, ends_sentence
from voxbridge.captions_core import CaptionError


class SentenceBatchingTests(unittest.TestCase):
    def test_dependent_german_clause_groups_without_loss(self):
        b = SentenceBatcher()
        self.assertEqual(b.feed(FinalFragment(0, 0, "Wenn das Paket ankommt,")), [])
        out = b.feed(FinalFragment(1, 400, "wird es weitergeleitet."))
        self.assertEqual(out[0].sequences, (0, 1))
        self.assertEqual(out[0].text, "Wenn das Paket ankommt, wird es weitergeleitet.")
        self.assertEqual(out[0].reason, "sentence")

    def test_abbreviation_and_numeric_boundary(self):
        for value in ("z.B.", "Version 2.", "192.168.1.1.", "Dr.", "т.д.", "wait..."):
            with self.subTest(value=value):
                self.assertFalse(ends_sentence(value))
        for value in ("Der Router arbeitet.", "Что это?", "Er sagte „Ja!“"):
            self.assertTrue(ends_sentence(value))

    def test_deadline_flush_and_next_sentence(self):
        b = SentenceBatcher(wait_ms=600)
        b.feed(FinalFragment(0, 100, "offen"))
        self.assertEqual(b.deadline_ms, 700)
        self.assertEqual(b.tick(699), [])
        self.assertEqual(b.tick(700)[0].reason, "deadline")
        self.assertEqual(b.feed(FinalFragment(1, 701, "fertig!"))[0].sequences, (1,))
        self.assertEqual(b.finish(900), [])

    def test_sequence_gap_never_joins_missing_fragments(self):
        b = SentenceBatcher()
        b.feed(FinalFragment(0, 0, "first"))
        out = b.feed(FinalFragment(2, 200, "third."))
        self.assertEqual([x.sequences for x in out], [(0,), (2,)])

    def test_limits_and_oversized_final_not_split(self):
        b = SentenceBatcher(max_chars=64, max_fragments=2)
        b.feed(FinalFragment(0, 0, "a"*40))
        out = b.feed(FinalFragment(1, 10, "b"*70))
        self.assertEqual([x.sequences for x in out], [(0,), (1,)])
        self.assertEqual(out[1].text, "b"*70)
        b.feed(FinalFragment(2, 20, "left"))
        self.assertEqual(b.feed(FinalFragment(3, 30, "right"))[0].reason, "limit")

    def test_cancel_discards_pending_and_rejects_more(self):
        b = SentenceBatcher()
        b.feed(FinalFragment(0, 0, "private"))
        b.cancel()
        self.assertIsNone(b.deadline_ms)
        with self.assertRaises(CaptionError):
            b.finish(100)

    def test_stale_sequence_or_time_rejected(self):
        b = SentenceBatcher()
        b.feed(FinalFragment(2, 100, "first"))
        with self.assertRaises(CaptionError):
            b.feed(FinalFragment(1, 200, "duplicate"))
        with self.assertRaises(CaptionError):
            b.feed(FinalFragment(3, 99, "early"))
        self.assertEqual(b.finish(500)[0].sequences, (2,))

    def test_final_at_deadline_not_combined_with_expired_batch(self):
        b = SentenceBatcher(wait_ms=300)
        b.feed(FinalFragment(0, 0, "first"))
        out = b.feed(FinalFragment(1, 300, "second."))
        self.assertEqual([x.sequences for x in out], [(0,), (1,)])
