import unittest
from voxbridge.cartesia_realtime import InkTurnMapper, InkEventError
from voxbridge.captions_core import CaptionCoordinator


class InkMapperTests(unittest.TestCase):
    def setUp(self):
        self.c=CaptionCoordinator()
        self.g=self.c.begin(source_language="de",target_language="ru")
        self.m=InkTurnMapper(self.c,self.g)

    def event(self,kind,**fields):
        self.m.accept({"type":kind,"request_id":"synthetic-id",**fields})

    def test_chunks_preserve_spaces_and_translate_only_flushed_turn(self):
        self.event("transcript",text="Der",is_final=True)
        self.event("transcript",text=" R",is_final=False)
        self.event("transcript",text=" Router",is_final=True)
        self.event("transcript",text=" ist bereit.",is_final=True)
        self.assertEqual(self.c.snapshot().partial,"Der Router ist bereit.")
        self.assertIsNone(self.c.take_translation())
        self.m.begin_flush()
        self.event("flush_done")
        job=self.c.take_translation()
        self.assertEqual(job.text,"Der Router ist bereit.")
        self.assertTrue(self.c.finish_translation(job,"OK"))
        self.assertEqual(self.m.committed,1)

    def test_nonfinal_revisions_replace_not_append(self):
        self.event("transcript",text="Hallo",is_final=False)
        self.event("transcript",text="Guten Morgen",is_final=False)
        self.assertEqual(self.c.snapshot().partial,"Guten Morgen")
        self.m.begin_flush()
        self.event("transcript",text="Guten Morgen.",is_final=True)
        self.event("flush_done")
        self.assertEqual(self.c.snapshot().lines[0].original,"Guten Morgen.")

    def test_multiple_turns_and_close(self):
        for text in ("Erster Satz.","Zweiter Satz."):
            self.event("transcript",text=text,is_final=True)
            self.m.begin_flush()
            self.event("flush_done")
        self.event("done")
        self.assertEqual([l.original for l in self.c.snapshot().lines],["Erster Satz.","Zweiter Satz."])
        self.assertTrue(self.m.closed)

    def test_unexpected_flush_and_duplicate_begin(self):
        with self.assertRaisesRegex(InkEventError,"INK_UNEXPECTED_FLUSH"):
            self.event("flush_done")
        self.m.begin_flush()
        with self.assertRaisesRegex(InkEventError,"INK_INVALID_FLUSH"):
            self.m.begin_flush()

    def test_unfinalized_partial_is_not_promoted(self):
        self.event("transcript",text="tentative",is_final=False)
        self.m.begin_flush()
        with self.assertRaisesRegex(InkEventError,"INK_UNEXPECTED_FLUSH"):
            self.event("flush_done")
        self.assertEqual(self.c.snapshot().lines,())

    def test_request_mismatch_cancel_and_invalid_text(self):
        self.event("transcript",text="OK",is_final=True)
        with self.assertRaisesRegex(InkEventError,"INK_REQUEST_MISMATCH"):
            self.m.accept({"type":"done","request_id":"other"})
        with self.assertRaisesRegex(InkEventError,"INK_INVALID_TRANSCRIPT"):
            self.event("transcript",text="bad\x00",is_final=True)
        self.c.stop()
        with self.assertRaisesRegex(InkEventError,"INK_STALE_SESSION"):
            self.event("done")

    def test_overflow_and_close_with_pending(self):
        self.event("transcript",text="x"*2000,is_final=True)
        with self.assertRaisesRegex(InkEventError,"INK_TEXT_LIMIT"):
            self.event("transcript",text="x",is_final=True)
        with self.assertRaisesRegex(InkEventError,"INK_UNFLUSHED_CLOSE"):
            self.event("done")


class InkTransportTests(unittest.TestCase):
    def setUp(self):
        import threading
        from voxbridge.session_limits import RealtimeUsage
        self.c=CaptionCoordinator()
        self.g=self.c.begin(source_language="de",target_language="ru")
        self.stop=threading.Event()
        self.usage=RealtimeUsage(10)
        self.voice=(1000).to_bytes(2,"little",signed=True)*4800
        self.voice2=(2000).to_bytes(2,"little",signed=True)*4800
        self.quiet=bytes(9600)

    def run_transport(self,source,socket,**kwargs):
        from voxbridge.cartesia_realtime import run_ink_transcription
        opts=dict(approved=True,stop=self.stop,max_seconds=10,usage=self.usage,
                  api_key="synthetic",socket_factory=lambda *a,**k:socket)
        opts.update(kwargs)
        return run_ink_transcription(self.c,self.g,source,**opts)

    def fake_socket(self,delay=5):
        import json
        from collections import deque
        class Socket:
            def __init__(self):
                self.sent=[]
                self.events=deque()
                self.wait=0
                self.flushes=0
                self.closed=False
            def send(self,raw):
                if isinstance(raw,bytes) and self.wait:
                    raise AssertionError("audio sent before flush acknowledgement")
                self.sent.append(raw)
                if raw=="finalize":
                    self.flushes+=1
                    self.events.append(dict(type="transcript",is_final=True,text=f"Turn {self.flushes}"))
                    self.events.append(dict(type="flush_done"))
                    self.wait=delay
                elif raw=="close":
                    self.events.append(dict(type="done"))
            def recv(self,timeout):
                if self.wait:
                    self.wait-=1
                    raise TimeoutError()
                if not self.events:raise TimeoutError()
                event=self.events.popleft()
                return json.dumps(dict(request_id="fake",**event))
            def close(self):self.closed=True
        return Socket()

    def test_audio_during_flush_kept_in_order(self):
        sock=self.fake_socket(delay=5)
        chunks=[self.voice]+[self.quiet]*3+[self.voice2]+[self.quiet]*3
        self.assertEqual(self.run_transport(iter(chunks),sock),2)
        self.assertEqual(sock.sent,[self.voice,*([self.quiet]*3),"finalize",self.voice2,*([self.quiet]*3),"finalize","close"])
        self.assertEqual([l.original for l in self.c.snapshot().lines],["Turn 1","Turn 2"])
        self.assertEqual(self.usage.snapshot().attempted_audio_samples,8*4800)
        self.assertTrue(sock.closed)

    def test_no_consent_or_missing_key_no_socket(self):
        from unittest.mock import Mock
        from voxbridge.realtime_transport import RealtimeTransportFailure
        factory=Mock()
        with self.assertRaisesRegex(RealtimeTransportFailure,"INK_CLOUD_CONSENT_REQUIRED"):
            self.run_transport([],None,approved=False,socket_factory=factory)
        with self.assertRaisesRegex(RealtimeTransportFailure,"CARTESIA_API_KEY"):
            self.run_transport([],None,api_key="",socket_factory=factory)
        factory.assert_not_called()

    def test_cancel_does_not_flush_or_upload_more(self):
        sock=self.fake_socket()
        def source():
            yield self.voice
            self.stop.set()
            yield self.voice2
        self.assertEqual(self.run_transport(source(),sock),0)
        self.assertEqual(sock.sent,[self.voice])
        self.assertTrue(sock.closed)
        self.assertFalse(self.c.snapshot().active)

    def test_pending_overflow_fails_no_silent_loss(self):
        from voxbridge.realtime_transport import RealtimeTransportFailure
        sock=self.fake_socket(delay=10000)
        with self.assertRaisesRegex(RealtimeTransportFailure,"INK_PENDING_AUDIO_LIMIT"):
            self.run_transport(iter([self.voice]+[self.quiet]*3+[self.voice2]*11),sock)
        self.assertEqual(sock.sent,[self.voice,*([self.quiet]*3),"finalize"])
        self.assertTrue(sock.closed)

    def test_empty_capture_closes_without_commit(self):
        sock=self.fake_socket()
        self.assertEqual(self.run_transport([self.quiet],sock),0)
        self.assertEqual(sock.sent,["close"])
        self.assertTrue(sock.closed)
