"""Main OMOI reuse contracts: fake SDK only, no models or devices."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from voxbridge.audio import pcm_to_wav
from voxbridge.config import Settings,save_settings,load_settings
from voxbridge.dictation_quality import inspect_transcript
from voxbridge.local_partial import ResidentMiniSTT
from voxbridge.stt_enhancements import correct_terms,condition_pcm

class EnhancementTests(unittest.TestCase):
    def test_settings_opt_in_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"config.json"
            s=Settings(enhanced_cpu_stt=True,stt_hotwords="OMOI, VLAN",stt_term_correction=True,stt_audio_conditioning=True)
            save_settings(s,path)
            self.assertEqual(load_settings(path),s)
        for invalid in ("x"*193,"\n","\x00","\ud800"):
            with self.assertRaises(ValueError):
                Settings(stt_hotwords=invalid).validate()
        self.assertFalse(Settings().enhanced_cpu_stt)

    def test_context_correction_preserves_ordinary_text_and_numbers(self):
        self.assertEqual(correct_terms("Whispel CPU"),("Whisper CPU",("whisper-whispel",)))
        for text in ("Whispel","install npm","OpenFlow 1.3 CPU","20.26.4"):
            self.assertEqual(correct_terms(text),(text,()))

    def test_conditioning_preserves_length_silence_and_headroom(self):
        self.assertEqual(condition_pcm(bytes(32000)),bytes(32000))
        x=b"\x00\x01\x00\xff"*8000
        y=condition_pcm(x)
        self.assertEqual(len(x),len(y))
        self.assertNotEqual(x,y)
        self.assertEqual(condition_pcm(b"\x00\x01"*16000),bytes(32000))

    def test_quality_guard_words_and_numeric_codes(self):
        self.assertEqual(inspect_transcript("yes "*20),"STT_WORD_REPETITION")
        self.assertEqual(inspect_transcript(" ".join("word"+str(i) for i in range(20))),"STT_TEXT_OK")
        self.assertEqual(inspect_transcript("12345 12345 12345"),"STT_TEXT_OK")

    def test_transcript_structural_validation_preserves_de_ru_and_literals(self):
        for text in ("Gr\u00fc\u00dfe, DNS 192.168.1.1", "\u041f\u0440\u0438\u0432\u0435\u0442, VLAN 42",
                     "a < b, 10 >= 2", "0011 11111111", "line 1\nline 2\tOK"):
            with self.subTest(text=text):
                self.assertEqual(inspect_transcript(text), "STT_TEXT_OK")
        for text in ("hello\x1bworld", "hidden\u202e text", "bad\ud800", "foo\x7fbar",
                     "<|de|> Hallo", "word <|endoftext|>"):
            with self.subTest(text=repr(text)):
                self.assertEqual(inspect_transcript(text), "STT_TEXT_INVALID")

    def test_enhanced_worker_reuses_installed_sdk_contract(self):
        node=shutil.which("node")
        if not node:
            self.skipTest("Node required for fake SDK protocol")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            sdk=root/"node_modules"/"openvino-genai-node"
            sdk.mkdir(parents=True)
            (sdk/"package.json").write_text(json.dumps({"type":"module","main":"index.js"}))
            (sdk/"index.js").write_text("""
export const StreamingStatus={RUNNING:0,CANCEL:2};
export async function ASRPipeline(model,device){
 if(device!=='CPU')throw Error('device');
 return {async generate(samples,options){
   if(samples.length!==16000||options.generationConfig.language!=='<|ru|>'||options.generationConfig.hotwords!=='OMOI, VLAN')throw Error('contract');
   options.streamer?.('first');
   return {texts:['final text'],scores:[-1.2],languages:['ru']};
 }};
}
""",encoding="utf-8")
            env=os.environ.copy()
            env.update(VOXBRIDGE_OPENVINO_SDK_DIR=str(root), OMOI_MINI_STT_MODEL=str(root),
                       OMOI_MINI_STT_DEVICE="CPU", VB_OPENVINO_PROFILE="enhanced")
            from voxbridge import local_partial
            actual=Path(local_partial.__file__).with_name("openvino_worker.mjs")
            drafts=[]
            backend=ResidentMiniSTT(engine="openvino",enhanced=True,hotwords="OMOI, VLAN",on_delta=drafts.append)
            with patch.object(backend,"_launch_spec",return_value=([node,str(actual)],actual,env)):
                try:
                    self.assertEqual(backend.transcribe(pcm_to_wav(bytes(32000)),"ru"),"final text")
                    self.assertEqual(drafts,["first"])
                    self.assertEqual(backend.native_chunks,1)
                    self.assertEqual(backend.last_score,-1.2)
                finally:
                    backend.close()


class BaselineWorkerTests(unittest.TestCase):
    def test_baseline_preserves_exact_generation_config_and_has_no_mini_source(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required for fake SDK protocol")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = root / "node_modules" / "openvino-genai-node"
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text(json.dumps({"type":"module","main":"index.js"}))
            (sdk / "index.js").write_text("""
export async function ASRPipeline(model, device, props) {
  if (device !== 'CPU' || Object.keys(props).length) throw Error('device');
  return {async generate(samples, options) {
    const cfg = options.generationConfig;
    if (Object.keys(cfg).length !== 4 || cfg.max_new_tokens !== 160 ||
        cfg.task !== 'transcribe' || cfg.return_timestamps !== false ||
        !['<|de|>','<|ru|>'].includes(cfg.language) || options.streamer ||
        samples.length !== 320000 || samples[0] !== -1 || samples[1] !== 32767/32768)
      throw Error('contract');
    return {texts: [cfg.language]};
  }};
}
""",encoding="utf-8")
            env = os.environ.copy()
            env.update(VOXBRIDGE_OPENVINO_SDK_DIR=str(root),
                       OMOI_MINI_STT_MODEL=str(root), OMOI_MINI_STT_DEVICE="CPU",
                       VB_OPENVINO_PROFILE="baseline")
            from voxbridge import local_partial
            actual = Path(local_partial.__file__).with_name("openvino_worker.mjs")
            backend = ResidentMiniSTT(engine="openvino")
            samples = pcm_to_wav(b"\x00\x80\xff\x7f" * 160000)
            with patch.object(backend, "_launch_spec", return_value=([node,str(actual)],actual,env)):
                try:
                    for language in ("de","ru"):
                        self.assertEqual(backend.transcribe_final(samples,language), "<|"+language+"|>")
                    self.assertFalse((root/"worker.mjs").exists())
                finally:
                    backend.close()

    def test_configurable_sdk_directory_does_not_require_mini_checkout(self):
        from voxbridge.stt import MiniOneShotSTT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"sdk"/"node_modules"/"openvino-genai-node").mkdir(parents=True)
            (root/"model").mkdir()
            with patch.dict(os.environ, {"VOXBRIDGE_OPENVINO_SDK_DIR":str(root/"sdk"),
                                        "OMOI_MINI_STT_MODEL":str(root/"model")}), \
                 patch("voxbridge.stt.Path.home", return_value=root), \
                 patch("voxbridge.stt.shutil.which", return_value="node.exe"):
                _, worker, env = MiniOneShotSTT(engine="openvino")._launch_spec()
                self.assertEqual(worker.name,"openvino_worker.mjs")
                self.assertEqual(Path(env["VOXBRIDGE_OPENVINO_SDK_DIR"]),root/"sdk")
                self.assertFalse((root/"Desktop").exists())
