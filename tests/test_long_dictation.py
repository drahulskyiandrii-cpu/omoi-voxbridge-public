"""Long-recording coverage with generated PCM and mocked recognizers."""
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS

from voxbridge.audio import Microphone, RATE, pcm_to_wav
from voxbridge.config import Settings
from voxbridge.long_dictation import ChunkedRecording, LongDictationTurn, IncompleteDictation, recording_pcm
from voxbridge.stt import SpeechFailure, _validate_wav
from voxbridge.app import VoxBridge
from voxbridge.session import Session, State

SECOND = b"\x00\x01\x00\xff" * (RATE // 2)


def wav(seconds):
    return pcm_to_wav(SECOND * seconds)


def complete_microphone_stop(app):
    generation = app.session.snapshot().generation
    app.mic = Mock()
    app.mic.busy = False
    app.mic_deadline = Mock()
    VoxBridge.finish_recording(app)
    app.mic.finish.assert_called_once()
    VoxBridge._microphone_stopped(app, generation, app.recorder.stop.return_value, "")


class LongDictationTests(unittest.TestCase):
    def test_settings_and_recorder_capacity(self):
        for seconds in (31, 60, 120):
            self.assertEqual(Settings(max_record_seconds=seconds).validate().max_record_seconds, seconds)
            mic = Microphone(seconds)
            self.assertEqual(mic._limit, seconds * RATE * 2)
        with self.assertRaises(ValueError):
            Microphone(121)
        mic = Microphone(60)
        mic._stream = Mock()
        mic._buffer.extend(SECOND * 35)
        self.assertEqual(mic.raw_snapshot(), wav(35))
        self.assertEqual(mic.stop(), wav(35))
        self.assertEqual(mic.last_quality.seconds, 35)

    def test_all_samples_once_and_provider_limit(self):
        sent = []
        def decode(audio, lang):
            _validate_wav(audio)
            self.assertEqual(lang, "de")
            sent.append(recording_pcm(audio))
            return f"segment {len(sent)}"
        chunks = ChunkedRecording(decode, "de", lambda: False)
        updates = []
        self.assertEqual(chunks.consume(wav(22), on_update=updates.append), "segment 1")
        self.assertEqual(chunks.consume(wav(22), on_update=updates.append), "segment 1")
        chunks.consume(wav(43), on_update=updates.append)
        self.assertEqual(chunks.consume(wav(75), final=True), "segment 1 segment 2 segment 3 segment 4")
        self.assertEqual(b"".join(sent), SECOND * 75)
        self.assertEqual(updates, ["segment 1", "segment 1 segment 2"])
        self.assertTrue(all(len(x) <= 30*RATE*2 for x in sent))
        self.assertEqual(chunks.offset, 75*RATE*2)

    def test_quiet_boundary_preserves_samples_and_silence_is_not_transcribed(self):
        speech = SECOND * 17
        raw = speech + bytes(2*RATE*2) + SECOND * 2
        sent = []
        chunks = ChunkedRecording(lambda a,l: sent.append(recording_pcm(a)) or "spoken", "ru", lambda: False)
        chunks.consume(pcm_to_wav(raw))
        self.assertGreaterEqual(chunks.offset, 17*RATE*2)
        self.assertLessEqual(chunks.offset, 19*RATE*2)
        self.assertEqual(chunks.consume(pcm_to_wav(raw), final=True), "spoken spoken")
        self.assertEqual(b"".join(sent), raw)
        chunks.consume(pcm_to_wav(raw+bytes(RATE*2*25)), final=True)
        self.assertEqual(len(sent), 2)

    def test_cancel_between_chunks_never_sends_next(self):
        stop = threading.Event()
        def decode(*args):
            stop.set()
            return "late"
        fn = Mock(side_effect=decode)
        chunks = ChunkedRecording(fn, "de", stop.is_set)
        with self.assertRaisesRegex(SpeechFailure, "STT_CANCELLED"):
            chunks.consume(wav(75), final=True)
        fn.assert_called_once()
        self.assertEqual(chunks.text,"")

    def test_failed_segment_preserves_confirmed_prefix_for_manual_copy(self):
        fn = Mock(side_effect=["first confirmed", SpeechFailure("STT_TEST_ERROR")])
        chunks = ChunkedRecording(fn,"de",lambda: False)
        with self.assertRaises(IncompleteDictation) as ctx:
            chunks.consume(wav(75), final=True)
        self.assertEqual(ctx.exception.text, "first confirmed")
        self.assertEqual(str(ctx.exception), "STT_TEST_ERROR")

    def test_live_preview_then_stop_reuses_one_worker(self):
        backend = Mock()
        sent = []
        def decode(audio,lang):
            _validate_wav(audio)
            sent.append(recording_pcm(audio))
            return f"part {len(sent)}"
        backend.transcribe_final.side_effect = decode
        preview = Mock()
        turn = LongDictationTurn(backend, lambda: wav(22), "ru", preview, interval=.01)
        preview.side_effect = lambda text: turn.finalize(wav(45))
        self.assertEqual(turn.run(), "part 1 part 2")
        self.assertEqual(b"".join(sent), SECOND * 45)
        preview.assert_called_once_with("part 1")
        backend.ensure.assert_called_once()
        backend.close.assert_called_once()

    def test_early_draft_is_provisional_and_exact_final_audio_reused(self):
        backend=Mock(on_delta=None)
        events=[]
        def decode(audio,lang):
            backend.on_delta("draft")
            self.assertEqual(turn.confirmed_text(),"")
            return "final text"
        backend.transcribe_final.side_effect=decode
        def preview(text):
            events.append(text)
            if text=="final text":
                turn.finalize(wav(8))
        turn=LongDictationTurn(backend,lambda:wav(8),"de",preview,
                               native_preview=True,interval=.01)
        self.assertEqual(turn.run(),"final text")
        self.assertEqual(events,["draft","final text"])
        backend.transcribe_final.assert_called_once_with(wav(8),"de")
        self.assertIsNone(backend.on_delta)
        self.assertIsNone(turn._early_wav)

    def test_early_draft_never_changes_committed_boundary_or_final_text(self):
        backend=Mock(on_delta=None)
        sent=[]
        events=[]
        def decode(audio,lang):
            sent.append(recording_pcm(audio))
            backend.on_delta("unconfirmed "+str(len(sent)))
            return "part "+str(len(sent))
        backend.transcribe_final.side_effect=decode
        def preview(text):
            events.append((text,turn.confirmed_text()))
            if text=="part 2 part 3":
                turn.finalize(wav(75))
        snapshots=iter([wav(8),wav(22),wav(43)])
        turn=LongDictationTurn(backend,lambda:next(snapshots),"ru",preview,
                               native_preview=True,interval=.01)
        self.assertEqual(turn.run(),"part 2 part 3 part 4 part 5")
        self.assertEqual(sent[0],SECOND*8)
        self.assertEqual(b"".join(sent[1:]),SECOND*75)
        self.assertEqual(events,[("unconfirmed 1",""),("part 1",""),
                                 ("unconfirmed 2",""),("part 2","part 2"),
                                 ("part 2 unconfirmed 3","part 2"),
                                 ("part 2 part 3","part 2 part 3")])
        self.assertIsNone(backend.on_delta)

    def test_stop_during_early_decode_suppresses_draft_and_uses_full_recording(self):
        backend=Mock(on_delta=None)
        def decode(audio,lang):
            if audio==wav(8):
                turn.finalize(wav(12))
                backend.on_delta("late draft")
                return "early text"
            backend.on_delta("final draft")
            return "complete recording"
        backend.transcribe_final.side_effect=decode
        preview=Mock()
        turn=LongDictationTurn(backend,lambda:wav(8),"de",preview,
                               native_preview=True,interval=.01)
        self.assertEqual(turn.run(),"complete recording")
        preview.assert_not_called()
        self.assertEqual(backend.transcribe_final.call_count,2)
        self.assertEqual(backend.transcribe_final.call_args.args,(wav(12),"de"))

    def test_cancel_during_early_decode_discards_private_draft(self):
        backend=Mock(on_delta=None)
        def decode(audio,lang):
            turn.cancel()
            backend.on_delta("late draft")
            return "late text"
        backend.transcribe_final.side_effect=decode
        preview=Mock()
        turn=LongDictationTurn(backend,lambda:wav(8),"de",preview,
                               native_preview=True,interval=.01)
        with self.assertRaisesRegex(SpeechFailure,"STT_CANCELLED"):
            turn.run()
        preview.assert_not_called()
        self.assertEqual(turn.confirmed_text(),"")
        self.assertIsNone(turn._early_wav)
        backend.cancel.assert_called_once()
        backend.close.assert_called_once()

    def test_failed_stream_keeps_confirmed_prefix_not_draft(self):
        backend=Mock(on_delta=None)
        def decode(audio,lang):
            if backend.transcribe_final.call_count==1:
                return "confirmed first"
            backend.on_delta("unreliable draft")
            raise SpeechFailure("STT_TEST_ERROR")
        backend.transcribe_final.side_effect=decode
        snapshots=iter([wav(22),wav(43)])
        turn=LongDictationTurn(backend,lambda:next(snapshots),"de",Mock(),
                               native_preview=True,interval=.01)
        with self.assertRaises(IncompleteDictation) as ctx:
            turn.run()
        self.assertEqual(ctx.exception.text,"confirmed first")
        self.assertEqual(turn.confirmed_text(),"confirmed first")
        turn.on_partial.assert_called_with("confirmed first unreliable draft")

    def test_snapshot_failure_preserves_confirmed_text(self):
        backend = Mock()
        backend.transcribe_final.return_value = "confirmed"
        snapshot = Mock(side_effect=[wav(22), RuntimeError("private driver detail")])
        turn = LongDictationTurn(backend, snapshot, "de", Mock(), interval=.01)
        with self.assertRaises(IncompleteDictation) as ctx:
            turn.run()
        self.assertEqual(str(ctx.exception), "STT_ERROR")
        self.assertEqual(ctx.exception.text, "confirmed")
        backend.close.assert_called_once()

    def test_cleanup_failure_after_final_decode_preserves_text_for_manual_copy(self):
        backend = Mock()
        backend.transcribe_final.return_value = "confirmed"
        backend.close.side_effect = SpeechFailure("STT_PIPE_CLEANUP_TIMEOUT")
        turn = LongDictationTurn(backend, Mock(), "ru", Mock(), interval=.01)
        turn.finalize(wav(12))
        with self.assertRaises(IncompleteDictation) as ctx:
            turn.run()
        self.assertEqual(str(ctx.exception), "STT_PIPE_CLEANUP_TIMEOUT")
        self.assertEqual(ctx.exception.text, "confirmed")

    def test_cleanup_failure_after_cancel_never_returns_private_text(self):
        backend = Mock()
        backend.transcribe_final.return_value = "confirmed"
        backend.close.side_effect = RuntimeError("private exception detail")
        turn = LongDictationTurn(backend, lambda: wav(22), "ru", Mock(), interval=.01)
        turn.on_partial.side_effect = lambda _: turn.cancel()
        with self.assertRaisesRegex(SpeechFailure, "^STT_BACKEND_CLEANUP_FAILED$") as ctx:
            turn.run()
        self.assertNotIsInstance(ctx.exception, IncompleteDictation)
        self.assertEqual(turn.confirmed_text(), "")
        self.assertEqual(turn._chunks.text, "")

    def test_invalid_final_wav_offers_only_confirmed_prefix(self):
        session = Session()
        g = session.begin(1,2)
        session.advance(g, State.RECORDING, State.STOPPING_MIC)
        backend = Mock()
        turn = LongDictationTurn(backend, Mock(), "de", Mock())
        turn._confirmed = "confirmed earlier"
        app = NS(session=session, settings=Settings(stt_backend="mini_cpu", max_record_seconds=120),
                 _local_stt_armed=True, _shutting_down=False, _partial_turn=turn,
                 mic=Mock(), mic_deadline=Mock(), deadline=Mock(), recorder=Mock(),
                 status=Mock(), notify=Mock())
        app._microphone_failed = lambda *args: VoxBridge._microphone_failed(app, *args)
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            VoxBridge._microphone_stopped(app,g,b"bad","")
        fallback.assert_called_once_with("confirmed earlier","INCOMPLETE_DICTATION: INVALID_WAV_LENGTH")
        self.assertEqual(session.snapshot().state,State.IDLE)
        self.assertEqual(turn.confirmed_text(),"")
        backend.cancel.assert_called_once()

    def test_early_worker_failure_uses_confirmed_prefix_if_result_empty(self):
        session=Session()
        g=session.begin(1,2)
        backend=Mock()
        turn=LongDictationTurn(backend,Mock(),"ru",Mock())
        turn._confirmed="confirmed only"
        app=NS(session=session,_dictation_worker_generation=g,_partial_turn=turn,
               _shutting_down=False,deadline=Mock(),mic_deadline=Mock(),mic=Mock(),
               status=Mock(),notify=Mock())
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            VoxBridge.on_completed(app,g,"","STT_ERROR")
        fallback.assert_called_once_with("confirmed only","INCOMPLETE_DICTATION: STT_ERROR")
        self.assertEqual(session.snapshot().state,State.IDLE)
        self.assertIsNone(app._dictation_worker_generation)

    def test_standard_mode_does_not_decode_early(self):
        backend=Mock()
        backend.transcribe_final.return_value="confirmed"
        snapshots=iter([wav(8),wav(22)])
        turn=LongDictationTurn(backend,lambda:next(snapshots),"ru",Mock(),interval=.01)
        turn.on_partial.side_effect=lambda text:turn.finalize(wav(22))
        self.assertEqual(turn.run(),"confirmed confirmed")
        self.assertEqual(backend.transcribe_final.call_count,2)
        self.assertEqual(sum(len(recording_pcm(c.args[0])) for c in backend.transcribe_final.call_args_list),len(SECOND)*22)
        self.assertFalse(turn._early_decoded)

    def test_long_text_manual_copy_never_truncates(self):
        session=Session()
        g=session.begin(1,2)
        session.advance(g,State.RECORDING,State.INSERTING)
        text="word " * 700
        app=NS(session=session,_edit_identity=object(),_edit_worker_generation=None)
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            VoxBridge.try_insert(app,g,text)
        fallback.assert_called_once_with(text,"LONG_TEXT_MANUAL_COPY")
        self.assertEqual(session.snapshot().state,State.IDLE)

    def test_cloud_long_batch_waits_for_approval(self):
        session=Session()
        g=session.begin(1,2)
        app=NS(session=session,settings=Settings(stt_backend="openai_cloud",max_record_seconds=120),
               deadline=Mock(),recorder=Mock(),status=Mock(),notify=Mock(),_shutting_down=False,
               _approve_dictation=Mock(return_value=False),executor=Mock(),_partial_turn=None)
        app.recorder.stop.return_value=wav(45)
        with patch("voxbridge.app.OpenAIFileSTT") as provider:
            complete_microphone_stop(app)
        provider.assert_not_called()
        app.executor.submit.assert_not_called()
        self.assertEqual(session.snapshot().state,State.IDLE)

    def test_cloud_long_batch_after_approval(self):
        session=Session()
        g=session.begin(1,2)
        provider=Mock()
        provider.transcribe.side_effect=lambda audio,language: ("confirmed" if len(recording_pcm(audio))<=30*RATE*2 else self.fail("oversize"))
        future=Mock()
        app=NS(session=session,settings=Settings(stt_backend="openai_cloud",max_record_seconds=120),
               deadline=Mock(),recorder=Mock(),status=Mock(),notify=Mock(),_shutting_down=False,
               _approve_dictation=Mock(return_value=True),executor=Mock(),_partial_turn=None,active_backend=None)
        app.recorder.stop.return_value=wav(45)
        app.executor.submit.side_effect=lambda fn: (self.assertEqual(fn(),"confirmed confirmed"),future)[1]
        with patch("voxbridge.app.OpenAIFileSTT",return_value=provider):
            complete_microphone_stop(app)
        self.assertEqual(provider.transcribe.call_count,2)
        provider.close.assert_called_once()
        app._approve_dictation.assert_called_once()
