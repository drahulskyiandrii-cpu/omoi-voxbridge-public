"""Offline regression for local engine routing, saved vision tasks, voice metadata and HTTP PCM."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, MagicMock, patch
from voxbridge.audio import pcm_to_wav
from voxbridge.stt import MiniOneShotSTT, SpeechFailure
from voxbridge.config import Settings, save_settings, load_settings
from voxbridge.app import VoxBridge
from voxbridge.session import Session, State
from voxbridge.cartesia_voices import OwnedVoice
from voxbridge.voice_cache import load_voice_cache, save_voice_cache
from voxbridge.voice_ui import VoiceDialog
from voxbridge.vision import OpenAIVision
from voxbridge.vision_ui import VisionDialog
from voxbridge.tts_stream import stream_bytes_to_callback, stream_to_callback
from voxbridge.analysis_providers import ProviderFailure
from test_vision import PNG_1PX
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap
from PySide6.QtCore import QCoreApplication, QEvent

VOICE="11111111-1111-4111-8111-111111111111"
AUDIO=pcm_to_wav(bytes(6400))


def complete_microphone_stop(app):
    generation = app.session.snapshot().generation
    app.mic = Mock()
    app.mic.busy = False
    app.mic_deadline = Mock()
    VoxBridge.finish_recording(app)
    app.mic.finish.assert_called_once()
    VoxBridge._microphone_stopped(app, generation, app.recorder.stop.return_value, "")


class LocalBackendTests(unittest.TestCase):
    def test_openvino_cpu_reuses_sdk_without_mini_source_gpu_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            worker=home/"Desktop"/"OMOI-Mini"/"local-stt"
            (worker/"node_modules"/"openvino-genai-node").mkdir(parents=True)
            model=home/"model"
            model.mkdir()
            # Verify the real configuration and shared worker routing without inference.
            with patch("voxbridge.stt.Path.home",return_value=home), patch.dict(os.environ,{"OMOI_MINI_STT_MODEL":str(model),"OPENAI_API_KEY":"dummy-secret","HF_TOKEN":"dummy-secret","NODE_OPTIONS":"--require untrusted"}), patch("voxbridge.stt.shutil.which",return_value="node.exe"), patch("voxbridge.stt.require_free_memory") as gpu:
                backend = MiniOneShotSTT(engine="openvino")
                command, target, env = backend._launch_spec()
                self.assertEqual(command, ["node.exe", str(target)])
                self.assertEqual(target.name, "openvino_worker.mjs")
                self.assertNotEqual(target.parent, worker)
                self.assertEqual(env["VOXBRIDGE_OPENVINO_SDK_DIR"], str(worker.resolve()))
                self.assertEqual(env["VB_OPENVINO_PROFILE"], "baseline")
                self.assertEqual(env["OMOI_MINI_STT_DEVICE"],"CPU")
                self.assertEqual(env["HF_HUB_OFFLINE"],"1")
                for key in ("OPENAI_API_KEY","HF_TOKEN","NODE_OPTIONS"):
                    self.assertNotIn(key,env)
                with patch("voxbridge.local_partial.ResidentMiniSTT.transcribe_final",return_value="Hallo") as decode, patch("voxbridge.local_partial.ResidentMiniSTT.close") as close:
                    self.assertEqual(backend.transcribe(AUDIO,"de"),"Hallo")
                    decode.assert_called_once_with(AUDIO,"de")
                    close.assert_called_once()
            gpu.assert_not_called()

    def test_cuda_guard_before_process_creation(self):
        with patch("voxbridge.stt.require_free_memory",side_effect=SpeechFailure("VRAM_LOW")) as guard, patch("voxbridge.stt_process.subprocess.Popen") as popen:
            with self.assertRaisesRegex(SpeechFailure,"VRAM_LOW"):
                MiniOneShotSTT(engine="cuda").transcribe(AUDIO,"ru")
            guard.assert_called_once_with(8000)
            popen.assert_not_called()

    def test_local_route_does_not_ask_cloud_or_create_cloud_client(self):
        s=Session()
        g=s.begin(12,34)
        proc=Mock()
        proc.transcribe.return_value="Hallo"
        executor=Mock()
        executor.submit.side_effect=lambda f: (self.assertEqual(f(),"Hallo"),Mock())[1]
        app=NS(session=s,deadline=Mock(),recorder=Mock(),status=Mock(),settings=Settings(stt_backend="mini_cpu"),
               _shutting_down=False,_local_stt_armed=True,_approve_dictation=Mock(),_dictation_worker_generation=None,
               executor=executor,active_backend=None,signals=Mock(),notify=Mock())
        app.recorder.stop.return_value=AUDIO
        with patch("voxbridge.app.MiniOneShotSTT",return_value=proc) as factory,patch("voxbridge.app.OpenAIFileSTT") as cloud:
            complete_microphone_stop(app)
        cloud.assert_not_called()
        app._approve_dictation.assert_not_called()
        factory.assert_called_once_with(8000,engine="openvino")
        proc.transcribe.assert_called_once_with(AUDIO,"de")

    def test_mini_english_blocked_without_process(self):
        with patch("voxbridge.stt_process.subprocess.Popen") as proc:
            with self.assertRaises(SpeechFailure):
                MiniOneShotSTT(engine="openvino").transcribe(AUDIO,"en")
            proc.assert_not_called()


class PresetTests(unittest.TestCase):
    def test_task_save_reload_and_hotkey_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"settings.json"
            s=Settings(region_task="Translate only",region_task2_hotkey="Ctrl+Alt+E",region_task2="Answer only")
            save_settings(s,p)
            self.assertEqual(load_settings(p),s)
        for kw in ({"region_task2_hotkey":"Ctrl+Alt+Q","region_task2":"x"},{"region_task2":"x"},{"region_task2_hotkey":"Ctrl+Alt+E"},{"region_task":"a"*601}):
            with self.assertRaises(ValueError):
                Settings(**kw).validate()

    def test_task_prompt_does_not_inherit_technical_assistant_instructions(self):
        client=Mock()
        client.responses.create.return_value=NS(status="completed",output_text="Only translation",usage=NS(input_tokens=10,output_tokens=4))
        self.assertEqual(OpenAIVision(client=client).analyze_task_png(PNG_1PX,"Translate into Russian"),"Only translation")
        kw=client.responses.create.call_args.kwargs
        self.assertIn("Output only",kw["instructions"])
        self.assertNotIn("German IT exercises",kw["instructions"])
        self.assertEqual(kw["input"][0]["content"][0]["text"],"Translate into Russian")

    def test_region_action_passes_correct_preset(self):
        dialog=Mock()
        app=NS(_shutting_down=False,_dictation_idle=lambda:True,_tts_idle=lambda:True,
               live_captions_dialog=None,vision_dialog=dialog,settings=Settings(region_task2_hotkey="Ctrl+Alt+E",region_task2="Translate only"),notify=Mock())
        VoxBridge._region_action(app,"task2")
        dialog.start_shortcut.assert_called_once_with("analyze",task="Translate only")


class VoiceCacheTests(unittest.TestCase):
    def test_cache_roundtrip_and_different_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"voices.json"
            voices=[OwnedVoice("Synthetic",VOICE,"ru")]
            save_voice_cache(voices,p,key="test-key")
            self.assertEqual(load_voice_cache(p,key="test-key"),voices)
            self.assertEqual(load_voice_cache(p,key="other-key"),[])
            self.assertNotIn("test-key",p.read_text(encoding="utf-8"))

    def test_invalid_write_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"voices.json"
            voices=[OwnedVoice("Synthetic",VOICE,"ru")]
            save_voice_cache(voices,p,key="test-key")
            with self.assertRaises(ValueError):
                save_voice_cache(voices*2,p,key="test-key")
            self.assertEqual(load_voice_cache(p,key="test-key"),voices)


class QtCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_reopen_and_failed_refresh_preserve_list_and_selected(self):
        voices=[OwnedVoice("Synthetic",VOICE,"ru")]
        fetcher=Mock()
        dlg=VoiceDialog(fetcher=fetcher,cache_loader=lambda:voices,cache_saver=Mock())
        try:
            with patch("voxbridge.voice_ui.load_settings",return_value=Settings(cartesia_voice_id=VOICE)):
                dlg.show()
                self.assertEqual(dlg.choices.currentData(),VOICE)
                dlg._worker_generation=0
                dlg._loaded(0,[],"CARTESIA_VOICES_NETWORK_ERROR")
                self.assertEqual(dlg.choices.count(),1)
                dlg.close()
                dlg.show()
                self.assertEqual(dlg.choices.currentData(),VOICE)
                fetcher.assert_not_called()
        finally:
            dlg.close()
            dlg.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


class PCMTransportTests(unittest.TestCase):
    def test_raw_bytes_arbitrary_chunks_align_and_no_sse_retry(self):
        client=MagicMock()
        response=client.tts.with_streaming_response.generate.return_value.__enter__.return_value
        response.iter_bytes.return_value=[b"a",b"bcd",b"ef"]
        provider=NS(client=client,model="sonic-latest",voice_id=VOICE)
        chunks=[]
        self.assertEqual(stream_bytes_to_callback(provider,"Synthetic","ru",chunks.append),6)
        self.assertEqual(b"".join(chunks),b"abcdef")
        self.assertTrue(all(len(c)%2==0 for c in chunks))
        client.tts.generate_sse.assert_not_called()
        kw=client.tts.with_streaming_response.generate.call_args.kwargs
        self.assertFalse(kw["save"])
        self.assertEqual(kw["output_format"]["container"],"raw")

    def test_raw_incomplete_no_retry(self):
        client=MagicMock()
        client.tts.with_streaming_response.generate.return_value.__enter__.return_value.iter_bytes.return_value=[b"a"]
        with self.assertRaisesRegex(ProviderFailure,"STREAM_INCOMPLETE"):
            stream_bytes_to_callback(NS(client=client,model="sonic-latest",voice_id=VOICE),"Synthetic","ru",Mock())
        client.tts.generate_sse.assert_not_called()

    def test_sse_payment_error_is_specific_and_sanitized(self):
        client=Mock()
        client.tts.generate_sse.return_value=[NS(type="error",status_code=402,message="private")]
        with self.assertRaisesRegex(ProviderFailure,"PAYMENT_REQUIRED") as exc:
            stream_to_callback(NS(client=client,model="sonic-latest",voice_id=VOICE),"Synthetic","ru",Mock())
        self.assertNotIn("private",str(exc.exception))
