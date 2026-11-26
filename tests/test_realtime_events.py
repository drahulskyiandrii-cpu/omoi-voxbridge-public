"""Pure protocol tests, fake events only; no WebSocket/API, GPU or audio device."""
import base64
import unittest

from voxbridge.captions_core import CaptionCoordinator
from voxbridge.realtime_events import (
    REALTIME_TRANSCRIPTION_URL, RATE_HZ,
    RealtimeEventError, RealtimeTranscriptionMapper,
    build_audio_append, build_audio_commit, build_session_update,
)


def committed(item):
    return {"type": "input_audio_buffer.committed", "item_id": item}


def delta(item, text):
    return {"type": "conversation.item.input_audio_transcription.delta",
            "item_id": item, "content_index": 0, "delta": text}


def completed(item, text):
    return {"type": "conversation.item.input_audio_transcription.completed",
            "item_id": item, "content_index": 0, "transcript": text}


def failed(item):
    return {"type": "conversation.item.input_audio_transcription.failed", "item_id": item}


class RealtimeContractTests(unittest.TestCase):
    def setUp(self):
        self.core = CaptionCoordinator()
        self.gen = self.core.begin()
        self.mapper = RealtimeTranscriptionMapper(self.core, self.gen)

    def test_official_session_uses_24k_no_server_vad(self):
        config = build_session_update()
        self.assertEqual(config["type"], "session.update")
        self.assertEqual(config["session"]["type"], "transcription")
        inp = config["session"]["audio"]["input"]
        self.assertEqual(inp["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(inp["transcription"]["model"], "gpt-live-transcribe")
        self.assertEqual(inp["transcription"]["languages"], ["de"])
        self.assertEqual(inp["transcription"]["delay"], "low")
        self.assertIsNone(inp["turn_detection"])
        self.assertIn("intent=transcription", REALTIME_TRANSCRIPTION_URL)
        self.assertNotIn("?model=", REALTIME_TRANSCRIPTION_URL)

    def test_language_and_delay_session_contract(self):
        inp = build_session_update("ru", "medium")["session"]["audio"]["input"]
        self.assertEqual(inp["transcription"]["languages"], ["ru"])
        self.assertEqual(inp["transcription"]["delay"], "medium")
        for args in (("xx", "low"), ("de", "unknown")):
            with self.assertRaises(RealtimeEventError):
                build_session_update(*args)

    def test_base64_audio_chunks_and_explicit_commit(self):
        pcm = b"\x01\x00" * 4800
        obj = build_audio_append(pcm)
        self.assertEqual(obj["type"], "input_audio_buffer.append")
        self.assertEqual(base64.b64decode(obj["audio"]), pcm)
        self.assertEqual(RATE_HZ, 24000)
        self.assertEqual(build_audio_commit(), {"type": "input_audio_buffer.commit"})

    def test_audio_rejected_without_network(self):
        for pcm in (b"", b"\x00", b"\x00" * 9601, "not bytes", bytearray(4)):
            with self.subTest(pcm=str(pcm)[:10]):
                with self.assertRaisesRegex(RealtimeEventError, "INVALID_PCM"):
                    build_audio_append(pcm)

    def test_committed_partial_and_final(self):
        self.assertTrue(self.mapper.accept(committed("item_001")))
        self.assertTrue(self.mapper.accept(delta("item_001", "Ein ")))
        self.assertTrue(self.mapper.accept(delta("item_001", "VLAN")))
        self.assertEqual(self.core.snapshot().partial, "Ein VLAN")
        self.assertIsNone(self.core.take_translation())
        self.assertTrue(self.mapper.accept(completed("item_001", "Ein VLAN.")))
        snap = self.core.snapshot()
        self.assertEqual(snap.partial, "")
        self.assertEqual(snap.lines[0].original, "Ein VLAN.")
        self.assertEqual(snap.lines[0].sequence, 0)

    def test_out_of_order_completions_are_ordered_by_commit_item_id(self):
        self.mapper.accept(committed("item_a"))
        self.mapper.accept(committed("item_b"))
        self.mapper.accept(completed("item_b", "Zweiter Satz"))
        self.assertEqual(self.core.snapshot().lines, ())
        self.mapper.accept(completed("item_a", "Erster Satz"))
        self.assertEqual([l.original for l in self.core.snapshot().lines],
                         ["Erster Satz", "Zweiter Satz"])
        self.assertEqual([l.sequence for l in self.core.snapshot().lines], [0, 1])

    def test_early_completed_before_commit_ack(self):
        self.assertTrue(self.mapper.accept(completed("item_a", "Guten Morgen")))
        self.assertEqual(self.core.snapshot().lines, ())
        self.assertTrue(self.mapper.accept(committed("item_a")))
        self.assertEqual(self.core.snapshot().lines[0].original, "Guten Morgen")

    def test_failed_turn_skipped_without_wrong_translation(self):
        self.mapper.accept(committed("item_1"))
        self.mapper.accept(committed("item_2"))
        self.mapper.accept(completed("item_2", "Zweites Segment"))
        self.mapper.accept(failed("item_1"))
        self.assertEqual(self.mapper.dropped_failed_turns, 1)
        self.assertEqual(self.core.snapshot().lines[0].sequence, 1)
        self.assertEqual(self.core.snapshot().lines[0].original, "Zweites Segment")

    def test_failed_and_empty_finals_retire_partial_and_count_once(self):
        for i, terminal in enumerate((failed("a"), completed("b", " "))):
            item = terminal["item_id"]
            self.mapper.accept(committed(item))
            self.mapper.accept(delta(item, "tentative"))
            self.assertTrue(self.mapper.accept(terminal))
            self.assertFalse(self.mapper.accept(terminal))
            self.assertEqual(self.mapper.settled_turns, i + 1)
            self.assertEqual(self.core.snapshot().partial, "")
            self.assertEqual(self.core.snapshot().lines, ())
        self.assertEqual(self.mapper.dropped_failed_turns, 1)

    def test_terminal_counter_independent_of_visible_rows(self):
        for i in range(9):
            item = f"item_{i}"
            self.mapper.accept(committed(item))
            self.mapper.accept(completed(item, "Text"))
        self.assertEqual(self.mapper.settled_turns, 9)
        self.assertEqual(len(self.core.snapshot().lines), 6)

    def test_early_duplicate_terminal_keeps_first_and_order(self):
        self.mapper.accept(committed("a"))
        self.assertTrue(self.mapper.accept(completed("b", "First")))
        self.assertFalse(self.mapper.accept(failed("b")))
        self.mapper.accept(committed("b"))
        self.assertEqual(self.mapper.settled_turns, 0)
        self.mapper.accept(failed("a"))
        self.assertEqual(self.mapper.settled_turns, 2)
        self.assertEqual(self.core.snapshot().lines[0].original, "First")

    def test_duplicate_commit_and_late_result_ignored(self):
        self.mapper.accept(committed("item_1"))
        self.mapper.accept(completed("item_1", "Original"))
        self.assertFalse(self.mapper.accept(committed("item_1")))
        self.assertFalse(self.mapper.accept(completed("item_1", "Changed")))
        self.assertFalse(self.mapper.accept(delta("item_1", "Changed")))
        self.assertEqual(len(self.core.snapshot().lines), 1)
        self.assertEqual(self.core.snapshot().lines[0].original, "Original")

    def test_unexpected_ack_cannot_publish_early_terminal(self):
        self.mapper.accept(completed("a", "Test"))
        with self.assertRaisesRegex(RealtimeEventError, "UNEXPECTED_COMMIT"):
            self.mapper.accept(committed("a"), max_acknowledgements=0)
        self.assertEqual(self.core.snapshot().lines, ())
        self.assertEqual(self.mapper.acknowledged_turns, 0)

    def test_duplicate_ack_remains_fenced_for_whole_session(self):
        for i in range(80):
            item = f"item_{i}"
            self.mapper.accept(committed(item))
            self.mapper.accept(completed(item, "Test"))
        self.assertFalse(self.mapper.accept(committed("item_0")))
        self.assertFalse(self.mapper.accept(completed("item_0", "late")))
        self.assertEqual(self.mapper.acknowledged_turns, 80)

    def test_live_preview_before_commit_then_complete(self):
        self.assertTrue(self.mapper.accept(delta("a","Ein "),allow_live_preview=True))
        self.assertEqual(self.core.snapshot().partial,"Ein")
        self.assertIsNone(self.core.take_translation())
        self.mapper.accept(delta("a","VLAN"),allow_live_preview=True)
        self.assertEqual(self.core.snapshot().partial,"Ein VLAN")
        self.mapper.accept(committed("a"))
        self.mapper.accept(delta("a"," trennt"))
        self.assertEqual(self.core.snapshot().partial,"Ein VLAN trennt")
        self.mapper.accept(completed("a","Ein VLAN trennt Netze."))
        self.assertEqual(self.core.snapshot().partial,"")
        self.assertEqual(self.core.take_translation().text,"Ein VLAN trennt Netze.")

    def test_ambiguous_early_items_not_guessed(self):
        self.mapper.accept(delta("a","First"),allow_live_preview=True)
        self.mapper.accept(delta("b","Second"),allow_live_preview=True)
        self.assertEqual(self.core.snapshot().partial,"")
        self.mapper.accept(committed("b"))
        self.assertEqual(self.core.snapshot().partial,"Second")
        self.mapper.accept(completed("b","Second final"))
        self.mapper.accept(committed("a"))
        self.assertEqual(self.core.snapshot().partial,"First")
        self.assertEqual(self.core.take_translation().text,"Second final")

    def test_live_preview_cleared_on_abort_and_stale_generation(self):
        self.mapper.accept(delta("a","Tentative"),allow_live_preview=True)
        self.core.abort(self.gen)
        self.assertEqual(self.core.snapshot().partial,"")
        self.assertFalse(self.mapper.accept(delta("a","Late"),allow_live_preview=True))

    def test_unknown_delta_ignored(self):
        self.assertFalse(self.mapper.accept(delta("unknown_id", "Private text")))
        self.assertEqual(self.core.snapshot().partial, "")

    def test_unsupported_events_ignored(self):
        self.assertFalse(self.mapper.accept({"type": "session.updated"}))
        self.assertFalse(self.mapper.accept({"type": "rate_limits.updated"}))

    def test_invalid_event_and_item_safely_rejected(self):
        with self.assertRaisesRegex(RealtimeEventError, "INVALID_EVENT"):
            self.mapper.accept("not a dictionary")
        with self.assertRaisesRegex(RealtimeEventError, "INVALID_ITEM_ID"):
            self.mapper.accept(committed("../../secret"))
        with self.assertRaisesRegex(RealtimeEventError, "INVALID_TRANSCRIPT"):
            self.mapper.accept(completed("item_01", "A" * 2001))
        with self.assertRaisesRegex(RealtimeEventError, "UNSUPPORTED_CONTENT_INDEX"):
            self.mapper.accept({"type": "input_audio_buffer.committed",
                                "item_id": "item_2", "content_index": 1})

    def test_expanding_delta_rejected(self):
        self.mapper.accept(committed("item_a"))
        self.mapper.accept(delta("item_a", "A" * 1999))
        with self.assertRaisesRegex(RealtimeEventError, "PARTIAL_TOO_LARGE"):
            self.mapper.accept(delta("item_a", "MORE"))

    def test_backlog_limit_blocks_unbounded_items(self):
        for i in range(12):
            self.mapper.accept(committed(f"item_{i}"))
        with self.assertRaisesRegex(RealtimeEventError, "BACKLOG_LIMIT"):
            self.mapper.accept(committed("item_overflow"))

    def test_early_buffer_limit(self):
        for i in range(8):
            self.mapper.accept(completed(f"item_{i}", "text"))
        with self.assertRaisesRegex(RealtimeEventError, "EARLY_EVENTS_LIMIT"):
            self.mapper.accept(completed("item_overflow", "text"))

    def test_remote_error_sanitized(self):
        with self.assertRaisesRegex(RealtimeEventError, "REMOTE_ERROR") as ctx:
            self.mapper.accept({"type":"error", "error":{"message":"private token"}})
        self.assertNotIn("private token", str(ctx.exception))

    def test_close_stops_and_clears_session(self):
        self.mapper.accept(committed("item_a"))
        self.mapper.accept(delta("item_a", "Partial"))
        self.mapper.close()
        self.assertFalse(self.core.snapshot().active)
        self.assertEqual(self.core.snapshot().partial, "")
        self.assertFalse(self.mapper.accept(completed("item_a", "Late")))

    def test_generation_change_rejects_old_messages(self):
        old = self.mapper
        new_gen = self.core.begin()
        self.assertNotEqual(new_gen, old.generation)
        self.assertFalse(old.accept(committed("item_old")))
        self.assertEqual(self.core.snapshot().lines, ())


if __name__ == "__main__":
    unittest.main()
