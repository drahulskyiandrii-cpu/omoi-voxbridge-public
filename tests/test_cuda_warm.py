"""Explicit CUDA session lifecycle; all workers synthetic."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from voxbridge.cuda_warm import WarmCudaController
from voxbridge.local_partial import LocalPartialTurn, ResidentMiniSTT
from voxbridge.audio import pcm_to_wav
from voxbridge.stt import SpeechFailure
from voxbridge.config import Settings, load_settings, save_settings

WAV=pcm_to_wav(b"\x00\x01"*16000)

class InlineExecutor:
    def __init__(self):self.jobs=[]
    def submit(self,fn,*args):self.jobs.append((fn,args))
    def run(self):
        fn,args=self.jobs.pop(0);fn(*args)

class WarmControllerTests(unittest.TestCase):
    def fixture(self):
        pool=InlineExecutor();backend=Mock()
        backend.ready.return_value=True
        factory=Mock(return_value=backend)
        return WarmCudaController(pool,factory=factory),pool,backend,factory

    def test_explicit_activation_and_serial_cleanup(self):
        r,p,b,f=self.fixture()
        self.assertEqual(r.status(),("off",""))
        with self.assertRaisesRegex(SpeechFailure,"EXPLICIT"):
            r.activate(8000)
        f.assert_not_called()
        self.assertTrue(r.activate(8000,approved=True))
        self.assertEqual(r.status()[0],"loading")
        self.assertIsNone(r.current())
        b.ensure.assert_not_called()
        self.assertFalse(r.activate(8000,approved=True))
        p.run()
        self.assertIs(r.current(),b)
        self.assertTrue(r.deactivate())
        self.assertEqual(r.status()[0],"closing")
        self.assertIsNone(r.current())
        b.cancel.assert_called_once()
        p.run()
        b.close.assert_called_once()
        self.assertEqual(r.status()[0],"off")

    def test_cancel_while_loading_does_not_publish_ready(self):
        r,p,b,_=self.fixture()
        r.activate(8000,approved=True)
        r.deactivate()
        p.run()
        self.assertIsNone(r.current())
        self.assertEqual(r.status()[0],"closing")
        p.run()
        self.assertEqual(r.status()[0],"off")

    def test_worker_failure_preserves_error_no_silent_reload(self):
        r,p,b,_=self.fixture()
        b.ensure.side_effect=SpeechFailure("GPU_BUSY_SKIP_MODEL_LOAD")
        r.activate(8000,approved=True);p.run()
        self.assertEqual(r.status(),("failed","GPU_BUSY_SKIP_MODEL_LOAD"))
        self.assertIsNone(r.current())
        self.assertFalse(r.activate(8000,approved=True))

    def test_failed_start_and_failed_cleanup_publish_failure(self):
        r, p, b, _ = self.fixture()
        b.ensure.side_effect = SpeechFailure("START_FAILED")
        b.close.side_effect = RuntimeError("cleanup")
        r.activate(8000, approved=True)
        p.run()
        self.assertEqual(r.status(), ("failed", "CUDA_STT_CLEANUP_FAILED"))
        self.assertIsNone(r.current())

    def test_rejected_cleanup_schedule_can_be_retried(self):
        r, p, b, _ = self.fixture()
        r.activate(8000, approved=True)
        p.run()
        with patch.object(p, "submit", side_effect=RuntimeError):
            self.assertFalse(r.deactivate())
        self.assertEqual(r.status(), ("failed", "CUDA_STT_CLEANUP_SCHEDULE_FAILED"))
        self.assertTrue(r.deactivate())
        p.run()
        self.assertEqual(r.status(), ("off", ""))

    def test_cleanup_failure_keeps_backend_for_explicit_retry(self):
        r, p, b, _ = self.fixture()
        r.activate(8000, approved=True)
        p.run()
        b.close.side_effect = [RuntimeError("cleanup"), None]
        self.assertTrue(r.deactivate())
        p.run()
        self.assertEqual(r.status(), ("failed", "CUDA_STT_CLEANUP_FAILED"))
        self.assertTrue(r.deactivate())
        p.run()
        self.assertEqual(r.status(), ("off", ""))

    def test_success_retains_warm_backend_failure_closes(self):
        b=ResidentMiniSTT(engine="cuda")
        with patch.object(b,"ensure"),patch.object(b,"close") as close,patch.object(b,"transcribe_final",return_value="synthetic"):
            for language in ("de","ru"):
                turn=LocalPartialTurn(b,lambda:b"",language,Mock(),keep_resident=True)
                turn.finalize(WAV)
                self.assertEqual(turn.run(),"synthetic")
            close.assert_not_called()
            turn=LocalPartialTurn(b,lambda:b"","ru",Mock(),keep_resident=True)
            turn.cancel()
            with self.assertRaises(SpeechFailure):turn.run()
            close.assert_called_once()

    def test_language_specific_vocabulary_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"settings.json"
            s=Settings(stt_hotwords="VLAN",stt_hotwords_de="Firewall, Netzwerk",stt_hotwords_ru="server")
            save_settings(s,p);s=load_settings(p)
            self.assertEqual(s.vocabulary("de"),"Firewall, Netzwerk")
            self.assertEqual(s.vocabulary("ru"),"server")
            self.assertEqual(Settings(stt_hotwords="VLAN").vocabulary("ru"),"VLAN")
            with self.assertRaises(ValueError):Settings(stt_hotwords_de="a"*193).validate()

class IndependentWorkerTests(unittest.TestCase):
    def test_decode_uses_selected_language_and_beam(self):
        import json
        from types import SimpleNamespace
        from voxbridge.cuda_session_worker import decode
        model=Mock()
        model.transcribe.return_value=([SimpleNamespace(text="Synthetic")],SimpleNamespace(language="de"))
        with patch("voxbridge.cuda_session_worker.samples_from_wav",return_value="pcm"):
            reply=decode(model,json.dumps({"id":1,"audio":"fake","language":"de","options":{"beam_size":5,"hotwords":"VLAN"}}))
        self.assertTrue(reply["ok"])
        model.transcribe.assert_called_once_with("pcm",language="de",task="transcribe",beam_size=5,temperature=0,condition_on_previous_text=False,vad_filter=False,hotwords="VLAN")

    def test_no_live_mini_source_dependency(self):
        from voxbridge.stt import MiniOneShotSTT
        from voxbridge.cuda_warm import WarmCudaSTT
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            python=root/"python.exe";python.touch()
            model=root/"model";model.mkdir();(model/"model.bin").touch()
            overlay=root/"overlay";overlay.mkdir()
            with patch.dict(os.environ,{"LOCALAPPDATA":tmp,"OMOI_MINI_CUDA_STT_PYTHON":str(python),"OMOI_MINI_CUDA_STT_MODEL":str(model),"OMOI_MINI_CUDA_STT_OVERLAY":str(overlay)}),patch("voxbridge.stt.require_free_memory"),patch("voxbridge.stt.Path.home",return_value=root):
                for b in (MiniOneShotSTT(),WarmCudaSTT()):
                    cmd,worker,env=b._launch_spec()
                    self.assertEqual(worker.name,"cuda_session_worker.py")
                    self.assertTrue(worker.is_file())
                    self.assertNotIn("cuda_worker.py"," ".join(cmd))
