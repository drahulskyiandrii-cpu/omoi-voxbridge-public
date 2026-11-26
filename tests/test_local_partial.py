"""Partial recognition pipeline ? no actual audio, models, GPU, or cloud."""
import sys
import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from voxbridge.audio import Microphone, pcm_to_wav
from voxbridge.local_partial import LocalPartialTurn, ResidentMiniSTT
from voxbridge.stt import SpeechFailure
from voxbridge.config import Settings
from voxbridge.session import Session, State
from voxbridge.app import VoxBridge

PART=pcm_to_wav(b"\x00\x01"*16000)
FINAL=pcm_to_wav(b"\x00\x01"*32000)


def complete_microphone_stop(app):
    generation = app.session.snapshot().generation
    app.mic = Mock()
    app.mic.busy = False
    app.mic_deadline = Mock()
    VoxBridge.finish_recording(app)
    app.mic.finish.assert_called_once()
    VoxBridge._microphone_stopped(app, generation, app.recorder.stop.return_value, "")


class PartialTests(unittest.TestCase):
    def test_snapshot_does_not_consume_buffer(self):
        mic=Microphone()
        mic._stream=Mock()
        samples=b"\x00\x01\x00\xff"*8000
        mic._buffer.extend(samples[:-2])
        self.assertEqual(mic.snapshot(),b"")
        mic._buffer.extend(samples[-2:])
        self.assertEqual(len(mic.snapshot()),32044)
        self.assertEqual(len(mic._buffer),32000)
        self.assertEqual(mic.stop(),pcm_to_wav(samples))

    def test_existing_worker_protocol_multiple_requests_and_final(self):
        # A real owned child with only the standard library and synthetic text.
        script="import sys,json; print(json.dumps({'event':'ready'}),flush=True)\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({'id':r['id'],'ok':True,'text':'reply '+str(r['id'])}),flush=True)"
        backend=ResidentMiniSTT(engine="openvino")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backend,"_launch_spec",return_value=([sys.executable,"-u","-c",script],Path(tmp)/"worker.py",{})) as launch:
                try:
                    self.assertEqual(backend.transcribe(PART,"de"),"reply 1")
                    pid=backend._process.pid
                    self.assertEqual(backend.transcribe(FINAL,"de"),"reply 2")
                    self.assertEqual(backend._process.pid,pid)
                    launch.assert_called_once()
                finally:
                    backend.close()
        self.assertIsNone(backend._process)

    def test_cancel_during_queue_receive_discards_message(self):
        backend=ResidentMiniSTT(engine="openvino")
        def receive(**kwargs):
            backend.cancel()
            return {"id":1,"ok":True,"text":"late"}
        backend._messages=Mock()
        backend._messages.get.side_effect=receive
        with self.assertRaisesRegex(SpeechFailure,"STT_CANCELLED"):
            backend._receive(1)

    def test_enhanced_partial_rejects_boolean_request_id(self):
        backend=ResidentMiniSTT(engine="openvino",enhanced=True,on_delta=Mock())
        backend._process=Mock()
        backend.ensure=Mock()
        backend._receive=Mock(return_value={"id":True,"event":"partial","text":"draft"})
        with self.assertRaisesRegex(SpeechFailure,"LOCAL_PARTIAL_BAD_REPLY"):
            backend.transcribe(PART,"de")
        backend._writer.join(1)
        backend.on_delta.assert_not_called()
        self.assertEqual(backend.native_chunks,0)

    def test_partial_preview_then_final_same_backend(self):
        backend=Mock()
        backend.transcribe.side_effect=["unfinished","complete sentence"]
        preview=threading.Event()
        values=[]
        def emit(text):
            values.append(text)
            preview.set()
        turn=LocalPartialTurn(backend,lambda:PART,"ru",emit,interval=.05)
        result=[]
        worker=threading.Thread(target=lambda:result.append(turn.run()))
        worker.start()
        try:
            self.assertTrue(preview.wait(2))
            self.assertTrue(turn.finalize(FINAL))
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(values,["unfinished"])
            self.assertEqual(result,["complete sentence"])
            self.assertEqual(backend.transcribe.call_count,2)
            self.assertEqual(backend.transcribe.call_args.args,(FINAL,"ru"))
            backend.ensure.assert_called_once()
            backend.close.assert_called_once()
        finally:
            if worker.is_alive():
                turn.cancel()
                worker.join(2)

    def test_short_final_cleanup_failure_preserves_final_not_preview(self):
        from voxbridge.long_dictation import IncompleteDictation
        from voxbridge.stt_process import STT_CLEANUP_FAILED
        backend = Mock()
        backend.transcribe.return_value = "confirmed final"
        backend.close.side_effect = SpeechFailure(STT_CLEANUP_FAILED)
        turn = LocalPartialTurn(backend, lambda: PART, "de", Mock())
        turn.finalize(FINAL)
        with self.assertRaisesRegex(IncompleteDictation, STT_CLEANUP_FAILED) as caught:
            turn.run()
        self.assertEqual(caught.exception.text, "confirmed final")

    def test_oneshot_cleanup_failure_preserves_final_even_if_close_sets_cancel(self):
        from voxbridge.stt import MiniOneShotSTT
        from voxbridge.long_dictation import IncompleteDictation
        from voxbridge.stt_process import STT_CLEANUP_FAILED
        backend = MiniOneShotSTT(engine="openvino")
        child = Mock()
        child.transcribe_final.return_value = "confirmed final"
        def fail():
            backend.cancel()
            raise SpeechFailure(STT_CLEANUP_FAILED)
        child.close.side_effect = fail
        with patch("voxbridge.local_partial.ResidentMiniSTT", return_value=child):
            with self.assertRaisesRegex(IncompleteDictation, STT_CLEANUP_FAILED) as caught:
                backend.transcribe(FINAL, "de")
        self.assertEqual(caught.exception.text, "confirmed final")

    def test_cleanup_failure_does_not_label_invalid_final_confirmed(self):
        from voxbridge.long_dictation import IncompleteDictation
        from voxbridge.stt_process import STT_CLEANUP_FAILED
        from voxbridge.stt import MiniOneShotSTT
        child = Mock()
        child.transcribe_final.return_value = "<|de|> bad"
        child.close.side_effect = SpeechFailure(STT_CLEANUP_FAILED)
        with patch("voxbridge.local_partial.ResidentMiniSTT", return_value=child):
            with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED) as caught:
                MiniOneShotSTT(engine="openvino").transcribe(FINAL, "de")
        self.assertNotIsInstance(caught.exception, IncompleteDictation)
        backend = Mock()
        backend.transcribe.return_value = "a"*30
        backend.close.side_effect = SpeechFailure(STT_CLEANUP_FAILED)
        turn = LocalPartialTurn(backend, lambda: PART, "de", Mock())
        turn.finalize(FINAL)
        with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED) as caught:
            turn.run()
        self.assertNotIsInstance(caught.exception, IncompleteDictation)

    def test_implausible_preview_is_not_published_or_reused(self):
        backend = Mock()
        invalid = " ".join("longword"+str(i) for i in range(20))
        backend.transcribe.side_effect = [invalid, "final text"]
        preview = Mock()
        turn = LocalPartialTurn(backend, lambda: PART, "de", preview, interval=.05)
        def snapshot():
            # Submit final only after the initial preview decode has returned.
            if backend.transcribe.called:
                turn.finalize(PART)
            return PART
        turn.snapshot = snapshot
        self.assertEqual(turn.run(), "final text")
        self.assertEqual(backend.transcribe.call_count, 2)
        preview.assert_not_called()

    def test_stop_during_warmup_skips_partial(self):
        backend=Mock()
        backend.transcribe.return_value="final"
        snap=Mock(return_value=PART)
        turn=LocalPartialTurn(backend,snap,"de",Mock(),interval=.05)
        self.assertTrue(turn.finalize(FINAL))
        self.assertEqual(turn.run(),"final")
        snap.assert_not_called()
        backend.transcribe.assert_called_once_with(FINAL,"de")

    def test_cancel_discards_late_preview(self):
        backend=Mock()
        entered=threading.Event()
        release=threading.Event()
        def transcribe(*args):
            entered.set()
            release.wait(2)
            return "late hypothesis"
        backend.transcribe.side_effect=transcribe
        emit=Mock()
        turn=LocalPartialTurn(backend,lambda:PART,"de",emit,interval=.05)
        errors=[]
        def run():
            try:
                turn.run()
            except SpeechFailure as exc:
                errors.append(str(exc))
        worker=threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            turn.cancel()
        finally:
            release.set()
            worker.join(2)
        emit.assert_not_called()
        self.assertEqual(errors,["STT_CANCELLED"])
        backend.close.assert_called_once()

    def test_unchanged_snapshot_not_redecoded(self):
        backend=Mock()
        backend.transcribe.return_value="preview"
        emitted=threading.Event()
        turn=LocalPartialTurn(backend,lambda:PART,"de",lambda _:emitted.set(),interval=.05)
        results=[]
        worker=threading.Thread(target=lambda:results.append(turn.run()))
        worker.start()
        try:
            self.assertTrue(emitted.wait(2))
            time.sleep(.15)
            backend.transcribe.assert_called_once()
            turn.finalize(FINAL)
            worker.join(2)
            self.assertEqual(backend.transcribe.call_count,2)
        finally:
            if worker.is_alive():
                turn.cancel()
                worker.join(2)

    def test_finish_uses_existing_turn_without_second_worker(self):
        s=Session()
        g=s.begin(1,2)
        turn=Mock()
        turn.finalize.return_value=True
        app=NS(session=s,settings=Settings(stt_backend="mini_worker"),_local_stt_armed=True,
               _shutting_down=False,_partial_turn=turn,_dictation_worker_generation=g,
               recorder=Mock(),deadline=Mock(),status=Mock(),executor=Mock(),notify=Mock(),
               _approve_dictation=Mock())
        app.recorder.stop.return_value=FINAL
        complete_microphone_stop(app)
        self.assertEqual(s.snapshot().state,State.RECOGNIZING)
        turn.finalize.assert_called_once_with(FINAL)
        app.executor.submit.assert_not_called()
        app._approve_dictation.assert_not_called()

    def test_stale_preview_and_final_preview_never_update_status(self):
        s=Session()
        g=s.begin(1,2)
        app=NS(session=s,_shutting_down=False,_dictation_worker_generation=g,status=Mock())
        VoxBridge._partial_ready(app,g-1,"old")
        app.status.update_state.assert_not_called()
        VoxBridge._partial_ready(app,g,"draft")
        app.status.update_state.assert_called_once()
        s.advance(g,State.RECORDING,State.RECOGNIZING)
        VoxBridge._partial_ready(app,g,"late")
        self.assertEqual(app.status.update_state.call_count,1)

    def test_worker_error_during_recording_releases_session(self):
        s=Session()
        g=s.begin(1,2)
        app=NS(session=s,_dictation_worker_generation=g,_partial_turn=Mock(),
               _shutting_down=False,deadline=Mock(),recorder=Mock(),status=Mock(),notify=Mock(),_edit_identity=object())
        app.mic = Mock()
        app.mic_deadline = Mock()
        VoxBridge.on_completed(app,g,"","LOCAL_PARTIAL_NOT_READY")
        self.assertEqual(s.snapshot().state,State.IDLE)
        app.mic.cancel.assert_called_once()
        app.notify.assert_called_once_with("LOCAL_PARTIAL_NOT_READY")
        self.assertIsNone(app._partial_turn)

    def test_final_reuses_only_identical_valid_audio(self):
        backend=Mock()
        backend.transcribe.return_value="same complete text"
        turn=LocalPartialTurn(backend,lambda:PART,"ru",lambda _:turn.finalize(PART),interval=.05)
        self.assertEqual(turn.run(),"same complete text")
        backend.transcribe.assert_called_once_with(PART,"ru")
        summary=turn.summary()
        self.assertIn("same audio reuse=True",summary)
        self.assertIn("prefix=3/3",summary)
        self.assertIn("1 emitted/1 valid/1 decoded",summary)
        self.assertNotIn("same complete text",summary)

    def test_invalid_partial_never_reused_even_for_same_audio(self):
        backend=Mock()
        def first_decode(wav,language):
            turn.finalize(PART)
            return "a"*20
        responses=iter([first_decode,lambda *_:"valid final"])
        backend.transcribe.side_effect=lambda *args:next(responses)(*args)
        emit=Mock()
        turn=LocalPartialTurn(backend,lambda:PART,"ru",emit,interval=.05)
        self.assertEqual(turn.run(),"valid final")
        self.assertEqual(backend.transcribe.call_count,2)
        emit.assert_not_called()
        self.assertIn("0 emitted/0 valid/1 decoded",turn.summary())
        self.assertIn("same audio reuse=False",turn.summary())

    def test_invalid_partial_config(self):
        with self.assertRaises(ValueError):
            Settings(local_partial_enabled=1).validate()

class CudaDecoderTests(unittest.TestCase):
    def test_cuda_preview_and_final_use_distinct_beams(self):
        script = ("import sys,json; print(json.dumps({'event':'ready'}),flush=True)\n"
                  "for line in sys.stdin:\n"
                  " r=json.loads(line); o=r['options']; print(json.dumps({'id':r['id'],'ok':True,'text':str(o['beam_size'])+' '+o['hotwords']}),flush=True)")
        backend=ResidentMiniSTT(engine="cuda",hotwords="VLAN")
        with tempfile.TemporaryDirectory() as tmp, patch.object(backend,"_launch_spec",
                return_value=([sys.executable,"-u","-c",script],Path(tmp)/"worker.py",{})):
            try:
                self.assertEqual(backend.transcribe(PART,"de"),"1 VLAN")
                self.assertEqual(backend.transcribe_final(PART,"de"),"5 VLAN")
            finally:
                backend.close()

    def test_final_redecodes_identical_audio_with_quality_beam(self):
        backend=ResidentMiniSTT(engine="cuda")
        done=threading.Event()
        with patch.object(backend,"ensure"), patch.object(backend,"close"), \
             patch.object(backend,"transcribe",return_value="preview words") as preview, \
             patch.object(backend,"transcribe_final",return_value="final words") as final:
            turn=LocalPartialTurn(backend,lambda:PART,"de",lambda _:done.set(),interval=.05)
            results=[]
            worker=threading.Thread(target=lambda:results.append(turn.run()))
            worker.start()
            try:
                self.assertTrue(done.wait(2))
                self.assertTrue(turn.finalize(PART))
                worker.join(2)
                self.assertEqual(results,["final words"])
                preview.assert_called_once_with(PART,"de")
                final.assert_called_once_with(PART,"de")
            finally:
                if worker.is_alive():
                    turn.cancel()
                    worker.join(2)
