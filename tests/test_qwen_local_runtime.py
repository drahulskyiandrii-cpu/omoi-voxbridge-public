"""Qwen activation tests: no CUDA, API or model loads."""
import asyncio
import json
import os
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from voxbridge.config import Settings
from voxbridge.qwen_local_runtime import QwenRuntime, MODELS, child_env
from voxbridge.qwen_local_worker import AuthenticatedApp
from voxbridge.speech_core_tts import LocalTTSFailure
from voxbridge.tts_ui import CloudTTSDialog

class RuntimeTests(unittest.TestCase):
    def runtime(self):
        owners = Mock(side_effect=[(), (("127.0.0.1", 123),)] + [(("127.0.0.1", 123),)] * 40)
        job = Mock()
        job.contains_pid.return_value = True
        child = Mock()
        child.poll.return_value = None
        conn = Mock()
        conn.connect.side_effect = lambda: setattr(conn, 'sock', Mock())
        response = conn.getresponse.return_value
        def request(method, path, **kwargs):
            if not kwargs.get("headers", {}).get("Authorization"):
                response.status = 401
                return
            response.status = 200
            response.read.return_value = json.dumps({
                "service":"omoi-mini-local-tts", "model":MODELS["qwen_cf050"],
                "offline":True, "loaded":path == "/warmup", "state":"ready" if path == "/warmup" else "not_loaded"
            }).encode()
        conn.request.side_effect = request
        r = QwenRuntime(verify=Mock(return_value=(Path("python.exe"), Path("worker.py"))),
                        guard=Mock(), owners=owners, job_factory=Mock(return_value=job),
                        popen=Mock(return_value=child), connection=Mock(return_value=conn))
        return r, child, job, conn

    def test_preload_can_queue_one_read_without_second_activation(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = False
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime)
        try:
            dlg._local_loading = True
            self.assertTrue(dlg.start_shortcut("Synthetic", "de", "qwen_cf050"))
            self.assertFalse(dlg.start_shortcut("Second", "de", "qwen_cf050"))
            self.assertEqual(dlg._queued_playback[0], "Synthetic")
            with patch.object(dlg, "_start_playback") as start:
                dlg._model_completed("qwen_cf050", "")
            start.assert_called_once_with("Synthetic", "auto", "qwen_cf050", shortcut=True, fallback="de")
            self.assertIsNone(dlg._queued_playback)
            runtime.activate.assert_not_called()
        finally:
            dlg._local_loading = False
            dlg.close()

    def test_rejected_shortcut_cannot_keep_manual_queued_text_on_dismiss(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = False
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime,
                             keep_warm=True, shortcut_notify=Mock())
        dlg._local_loading = True
        try:
            self.assertTrue(dlg._start_playback("Synthetic", "de", "qwen_cf050", shortcut=False))
            self.assertFalse(dlg.start_shortcut("Other", "de", "qwen_cf050"))
            self.assertFalse(dlg._shortcut)
            dlg.close()
            self.assertIsNone(dlg._queued_playback)
            self.assertTrue(dlg._local_loading)
            runtime.close.assert_not_called()
        finally:
            dlg._local_loading = False
            dlg.close()

    def test_rejected_manual_play_cannot_cancel_queued_shortcut_on_dismiss(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = False
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime,
                             keep_warm=True, shortcut_notify=Mock())
        dlg._local_loading = True
        try:
            self.assertTrue(dlg.start_shortcut("Synthetic", "de", "qwen_cf050"))
            self.assertFalse(dlg._start_playback("Other", "de", "qwen_cf050", shortcut=False))
            self.assertTrue(dlg._shortcut)
            dlg.close()
            self.assertEqual(dlg._queued_playback[0], "Synthetic")
            self.assertTrue(dlg._local_loading)
        finally:
            dlg._queued_playback = None
            dlg._local_loading = False
            dlg.close()

    def test_prewarm_stays_on_when_window_closes_and_off_blocks_reads(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = False
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime, keep_warm=True)
        dlg._local_loading = True
        try:
            dlg.close()
            self.assertTrue(dlg._local_loading)
            runtime.close.assert_not_called()
            self.assertTrue(dlg.start_shortcut("Synthetic", "de", "qwen_cf050"))
            dlg._stop()
            self.assertIsNone(dlg._queued_playback)
            runtime.close.assert_not_called()
            dlg._local_loading = False
            dlg.set_tts_enabled(False)
            self.assertFalse(dlg.start_shortcut("Synthetic", "de", "qwen_cf050"))
            self.assertFalse(dlg.play.isEnabled())
        finally:
            dlg._local_loading = False
            dlg.close()

    def test_off_on_waits_for_cleanup_and_cancelled_activation(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = False
        notify = Mock()
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime,
                             keep_warm=True, shortcut_notify=notify)
        tasks = []
        def defer(*, target, **kwargs):
            return SimpleNamespace(start=lambda: tasks.append(target))
        with patch("voxbridge.tts_ui.threading.Thread", side_effect=defer):
            self.assertTrue(dlg.ensure_prewarm())
            cancelled = dlg._local_cancel
            dlg.set_tts_enabled(False)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(dlg.model_status, "Qwen closing")
            self.assertFalse(dlg.play.isEnabled())
            dlg.set_tts_enabled(True)
            self.assertEqual(len(tasks), 2)
            # Cleanup can complete before the activation thread acknowledges cancellation.
            tasks[1]()
            self.assertTrue(dlg._local_loading)
            self.assertEqual(len(tasks), 2)
            dlg._model_completed("qwen_cf050", "QWEN_CANCELLED")
            self.assertEqual(len(tasks), 3)
            self.assertTrue(dlg._local_loading)
            self.assertEqual(dlg.model_status, "Qwen loading")
            notify.assert_not_called()
            dlg.set_tts_enabled(False)
            tasks[3]()
            dlg._model_completed("qwen_cf050", "QWEN_CANCELLED")
            self.assertEqual(dlg.model_status, "OFF")
            self.assertEqual(len(tasks), 4)
            self.assertEqual(runtime.close.call_count, 2)
        dlg.close()

    def test_off_releases_ready_runtime_and_reports_cleanup_failure(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.ready.return_value = True
        dlg = CloudTTSDialog(transport_mode="qwen_cf050", local_runtime=runtime, keep_warm=True)
        dlg._local_used = True
        tasks = []
        with patch("voxbridge.tts_ui.threading.Thread",
                   side_effect=lambda *, target, **kw: SimpleNamespace(start=lambda: tasks.append(target))):
            dlg._stop()
            runtime.close.assert_not_called()
            dlg.set_tts_enabled(False)
            self.assertEqual(dlg.model_status, "Qwen closing")
            runtime.close.side_effect = RuntimeError("cleanup")
            tasks.pop()()
            self.assertEqual(dlg.model_status, "Qwen cleanup failed")
            self.assertFalse(dlg.play.isEnabled())
            runtime.close.side_effect = None
            runtime.ready.return_value = False
            dlg.set_tts_enabled(False)
            tasks.pop()()
            self.assertEqual(dlg.model_status, "OFF")
        dlg.close()

    def test_no_activation_without_explicit_approval(self):
        r, child, job, conn = self.runtime()
        with self.assertRaisesRegex(LocalTTSFailure, "EXPLICIT"):
            r.activate("qwen_cf050", threading.Event())
        r._popen.assert_not_called()
        self.assertFalse(r.ready("qwen_cf050"))

    def test_activation_pins_variant_and_unloads_owned_only(self):
        r, child, job, conn = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        self.assertTrue(r.ready("qwen_cf050"))
        self.assertFalse(r.ready("qwen_base"))
        env=r._popen.call_args.kwargs["env"]
        self.assertEqual(env["OMOI_MINI_QWEN_VARIANT"], "cf050")
        self.assertEqual(env["HF_HUB_OFFLINE"],"1")
        job.assign.assert_called_once_with(child)
        child.stdin.write.assert_called_once_with(b"GO\n")
        self.assertTrue(any(c.args[:2]==("POST","/warmup") for c in conn.request.call_args_list))
        r.close()
        self.assertFalse(r.ready("qwen_cf050"))
        job.terminate_and_close.assert_called_once()

    def test_close_during_preflight_prevents_late_spawn(self):
        r, _, _, _ = self.runtime()
        def verify(mode):
            r.close()
            return Path("python.exe"), Path("worker.py")
        r._verify = verify
        with self.assertRaisesRegex(LocalTTSFailure, "QWEN_CANCELLED"):
            r.activate("qwen_cf050", threading.Event(), approved=True)
        r._popen.assert_not_called()

    def test_pre_cancelled_activation_skips_verification(self):
        r, _, _, _ = self.runtime()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(LocalTTSFailure, "QWEN_CANCELLED"):
            r.activate("qwen_cf050", cancel, approved=True)
        r._verify.assert_not_called()

    def test_explicit_retry_after_worker_crash(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        child.poll.return_value = 1
        self.assertFalse(r.ready("qwen_cf050"))
        second = Mock()
        second.poll.return_value = None
        r._popen.return_value = second
        r._owners.side_effect = [()] + [(("127.0.0.1", 123),)] * 30
        r.activate("qwen_cf050", threading.Event(), approved=True)
        job.terminate_and_close.assert_called_once()
        self.assertTrue(r.ready("qwen_cf050"))
        r.close()

    def test_busy_port_does_not_attach_or_launch(self):
        r, _, _, _ = self.runtime()
        r._owners=Mock(return_value=(("127.0.0.1",321),))
        with self.assertRaisesRegex(LocalTTSFailure,"PORT_BUSY"):
            r.activate("qwen_cf050", threading.Event(), approved=True)
        r._popen.assert_not_called()
        r._connection.assert_not_called()

    def test_wrong_model_rejected_before_warmup(self):
        r, _, job, conn = self.runtime()
        with self.assertRaisesRegex(LocalTTSFailure,"MODEL_NOT_READY"):
            r.activate("qwen_base", threading.Event(), approved=True)
        self.assertFalse(any(c.args[:2]==("POST","/warmup") for c in conn.request.call_args_list))
        job.terminate_and_close.assert_called_once()

    def test_env_no_provider_keys(self):
        with patch.dict(os.environ,{"OPENAI_API_KEY":"mock", "CARTESIA_API_KEY":"mock", "HF_TOKEN":"mock"}):
            env=child_env("qwen_cf050","x"*32,"mockjob")
        for k in ("OPENAI_API_KEY","CARTESIA_API_KEY","HF_TOKEN"):
            self.assertNotIn(k,env)

    def test_unready_stream_rejected_before_network(self):
        r, _, _, _ = self.runtime()
        with self.assertRaisesRegex(LocalTTSFailure,"ENABLE_MODEL_FIRST"):
            r.stream("qwen_cf050","synthetic","de",Mock(),threading.Event())
        r._connection.assert_not_called()

    def test_successful_synthesis_reuses_same_owned_model(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        r._call = Mock(return_value={"service":"omoi-mini-local-tts",
            "model":MODELS["qwen_cf050"], "offline":True, "loaded":True, "state":"ready"})
        with patch("voxbridge.qwen_local_runtime.SpeechCoreTTS") as adapter:
            for _ in range(2):
                r.stream("qwen_cf050", "synthetic", "de", Mock(), threading.Event())
            self.assertEqual(adapter.return_value.stream.call_count, 2)
        self.assertTrue(r.ready("qwen_cf050"))
        job.terminate_and_close.assert_not_called()
        r.close()

    def test_cancelled_synthesis_reaps_model_before_retry(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        r._call = Mock(return_value={"service":"omoi-mini-local-tts",
            "model":MODELS["qwen_cf050"], "offline":True, "loaded":True, "state":"ready"})
        stop = threading.Event()
        def cancel(*args, **kwargs):
            stop.set()
            raise LocalTTSFailure("QWEN_CANCELLED")
        with patch("voxbridge.qwen_local_runtime.SpeechCoreTTS") as adapter:
            adapter.return_value.stream.side_effect = cancel
            with self.assertRaisesRegex(LocalTTSFailure, "QWEN_CANCELLED"):
                r.stream("qwen_cf050", "synthetic", "de", Mock(), stop)
            job.terminate_and_close.assert_called_once()
            self.assertFalse(r.ready("qwen_cf050"))
            self.assertIsNone(r._child)
            with self.assertRaisesRegex(LocalTTSFailure, "ENABLE_MODEL_FIRST"):
                r.stream("qwen_cf050", "next", "de", Mock(), threading.Event())
            adapter.return_value.stream.assert_called_once()

    def test_reviewed_worker_stop_retains_model_and_retries_busy(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        r._disconnect_safe = True
        r._call = Mock(return_value={"service":"omoi-mini-local-tts",
            "model":MODELS["qwen_cf050"], "offline":True, "loaded":True, "state":"ready"})
        stop = threading.Event()
        def cancel(*args, **kwargs):
            stop.set()
            raise LocalTTSFailure("LOCAL_TTS_CANCELLED")
        with patch("voxbridge.qwen_local_runtime.SpeechCoreTTS") as adapter:
            adapter.return_value.stream.side_effect = cancel
            with self.assertRaisesRegex(LocalTTSFailure, "LOCAL_TTS_CANCELLED"):
                r.stream("qwen_cf050", "synthetic", "de", Mock(), stop)
            self.assertTrue(r.ready("qwen_cf050"))
            job.terminate_and_close.assert_not_called()
            adapter.return_value.stream.side_effect = [LocalTTSFailure("LOCAL_TTS_BUSY"), 2]
            self.assertEqual(r.stream("qwen_cf050", "next", "ru", Mock(), threading.Event()), 2)
            self.assertIs(r._child, child)
        r.close()

    def test_synthesis_cleanup_failure_quarantines_runtime(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        r._call = Mock(side_effect=LocalTTSFailure("QWEN_REQUEST_TIMEOUT"))
        job.terminate_and_close.side_effect = OSError("private")
        with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_CLEANUP_FAILED$"):
            r.stream("qwen_cf050", "synthetic", "de", Mock(), threading.Event())
        self.assertIs(r._child, child)
        self.assertIs(r._job, job)
        with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_CLEANUP_FAILED$"):
            r.activate("qwen_cf050", threading.Event(), approved=True)
        r._popen.assert_called_once()

    def test_concurrent_synthesis_is_rejected_without_stopping_first(self):
        r, _, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        r._call = Mock(return_value={"service":"omoi-mini-local-tts",
            "model":MODELS["qwen_cf050"], "offline":True, "loaded":True, "state":"ready"})
        entered, release = threading.Event(), threading.Event()
        errors = []
        def stream(*args, **kwargs):
            entered.set()
            release.wait(3)
        def run():
            try:
                r.stream("qwen_cf050", "first", "de", Mock(), threading.Event())
            except Exception as exc:
                errors.append(str(exc))
        with patch("voxbridge.qwen_local_runtime.SpeechCoreTTS") as adapter:
            adapter.return_value.stream.side_effect = stream
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_SYNTHESIS_BUSY$"):
                    r.stream("qwen_cf050", "second", "de", Mock(), threading.Event())
                with self.assertRaisesRegex(LocalTTSFailure, "QWEN_ALREADY_ACTIVE"):
                    r.activate("qwen_cf050", threading.Event(), approved=True)
                adapter.return_value.stream.assert_called_once()
                job.terminate_and_close.assert_not_called()
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        r.close()

    def test_warmup_deadline_and_cancel_use_real_loopback_socket(self):
        import http.client
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        entered, release = threading.Event(), threading.Event()

        class StalledWarmup(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                entered.set()
                release.wait(3)

        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledWarmup)
        serving = threading.Thread(target=server.serve_forever,
                                   kwargs={"poll_interval": .02}, daemon=True)
        serving.start()
        r, _, job, _ = self.runtime()
        r._identity = Mock(return_value="x"*32)
        r._connection = lambda host, port, timeout: http.client.HTTPConnection(
            host, server.server_port, timeout=timeout)
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_REQUEST_TIMEOUT$"):
                r._call("/warmup", threading.Event(), timeout=.25)
            self.assertLess(time.monotonic()-start, 2)
            self.assertTrue(entered.is_set())
            entered.clear()
            stop = threading.Event()
            errors = []
            def run():
                try:
                    r._call("/warmup", stop, timeout=10)
                except LocalTTSFailure as exc:
                    errors.append(str(exc))
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            stop.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["QWEN_CANCELLED"])
            job.terminate_and_close.assert_not_called()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(1)

    def test_failed_job_teardown_retains_handle_and_refuses_activation(self):
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        job.terminate_and_close.side_effect = OSError("private native error")
        with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_CLEANUP_FAILED$"):
            r.close()
        self.assertIs(r._job, job)
        self.assertIs(r._child, child)
        self.assertFalse(r.ready("qwen_cf050"))
        with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_CLEANUP_FAILED$"):
            r.activate("qwen_cf050", threading.Event(), approved=True)
        r._popen.assert_called_once()
        job.terminate_and_close.side_effect = None
        r.close()
        self.assertIsNone(r._job)
        self.assertIsNone(r._child)
        child.wait.assert_called_once_with(timeout=2)

    def test_failed_child_wait_retains_exact_child_for_cleanup_retry(self):
        import subprocess
        r, child, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        child.wait.side_effect = subprocess.TimeoutExpired("private", 2)
        with self.assertRaisesRegex(LocalTTSFailure, "^QWEN_CLEANUP_FAILED$"):
            r.close()
        self.assertIsNone(r._job)
        self.assertIs(r._child, child)
        self.assertFalse(r.ready("qwen_cf050"))
        child.wait.side_effect = None
        r.close()
        self.assertIsNone(r._child)
        job.terminate_and_close.assert_called_once()

    def test_readiness_does_not_wait_for_native_teardown(self):
        import time
        r, _, job, _ = self.runtime()
        r.activate("qwen_cf050", threading.Event(), approved=True)
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(3)
        job.terminate_and_close.side_effect = blocked
        worker = threading.Thread(target=r.close)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            self.assertFalse(r.ready("qwen_cf050"))
            self.assertLess(time.monotonic()-start, .1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_auth_middleware_rejects_missing_duplicate_wrong_and_unknown(self):
        async def check(headers,path="/health",method="GET"):
            sent=[]
            async def app(scope,receive,send):
                await send({"type":"http.response.start","status":200})
            async def send(value): sent.append(value)
            await AuthenticatedApp(app,"x"*32)({"type":"http","method":method,"path":path,"headers":headers},None,send)
            return sent[0]["status"]
        valid=(b"authorization",b"Bearer "+b"x"*32)
        self.assertEqual(asyncio.run(check([])),401)
        self.assertEqual(asyncio.run(check([valid,valid])),401)
        self.assertEqual(asyncio.run(check([(b"authorization",b"wrong")])),401)
        self.assertEqual(asyncio.run(check([valid],"/other")),404)
        self.assertEqual(asyncio.run(check([valid])),200)

class ModelUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.runtime=Mock()
        self.runtime.ready.return_value=False
        self.factory=Mock()
        self.d=CloudTTSDialog(provider_factory=self.factory,permission_checker=lambda _:False,
                              transport_mode="qwen_cf050", local_runtime=self.runtime)
        self.addCleanup(self.d.close)
    def wait_until(self, predicate, seconds=3):
        import time
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_saved_choice_stays_cold_until_first_request(self):
        self.assertEqual(self.d.transport.currentData(),"qwen_cf050")
        self.assertEqual([self.d.transport.itemData(i) for i in range(self.d.transport.count())],["sse","qwen_cf050"])
        self.assertTrue(self.d.play.isEnabled())
        self.runtime.activate.assert_not_called()
        self.runtime.stream.assert_not_called()
        def activate(mode,cancel,approved=False):
            self.assertTrue(approved)
            self.runtime.ready.return_value=True
        self.runtime.activate.side_effect=activate
        def inline(*,target,**kwargs):return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.assertTrue(self.d.start_shortcut("Text","de","qwen_cf050"))
            self.assertTrue(self.d.start_shortcut("Second text","ru","qwen_cf050"))
        self.runtime.activate.assert_called_once()
        self.assertEqual(self.runtime.stream.call_count,2)
        self.factory.assert_not_called()

    def test_first_manual_play_and_no_activation_for_invalid_text(self):
        def inline(*,target,**kwargs):return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.d._play()
            self.runtime.activate.assert_not_called()
            self.d.text.setPlainText("Synthetic")
            self.d._play()
        self.runtime.activate.assert_called_once()
        self.runtime.stream.assert_called_once()
        self.factory.assert_not_called()

    def test_stop_during_loading_prevents_synthesis(self):
        def activate(mode,cancel,approved=False):
            self.d._stop()
            self.assertTrue(cancel.is_set())
        self.runtime.activate.side_effect=activate
        def inline(*,target,**kwargs):return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.assertTrue(self.d.start_shortcut("Text","de","qwen_cf050"))
        self.runtime.close.assert_called()
        self.runtime.stream.assert_not_called()
        self.factory.assert_not_called()

    def test_activation_error_does_not_fallback_to_cloud(self):
        self.runtime.activate.side_effect=LocalTTSFailure("GPU_BUSY_SKIP_MODEL_LOAD")
        def inline(*,target,**kwargs):return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.assertTrue(self.d.start_shortcut("Text","de","qwen_cf050"))
        self.assertIn("GPU_BUSY",self.d.status.text())
        self.runtime.stream.assert_not_called()
        self.factory.assert_not_called()

    def test_cold_load_not_cancelled_by_cloud_25_second_timeout(self):
        import time
        jobs=[]
        def queued(*,target,**kwargs):return SimpleNamespace(start=lambda:jobs.append(target))
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=queued):
            self.assertTrue(self.d.start_shortcut("Text","de","qwen_cf050"))
        self.d._progress_at=time.monotonic()-40
        self.d._pump()
        self.assertFalse(self.d._cancel.is_set())
        self.d._stop()
        jobs[0]()
        self.runtime.activate.assert_not_called()

    def test_button_explicit_activation_then_cloud_switch(self):
        def activate(mode,cancel,approved=False):
            self.assertTrue(approved)
            self.runtime.ready.side_effect=lambda m:m==mode
        self.runtime.activate.side_effect=activate
        def inline(*,target,**kwargs): return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.d.model_button.click()
        self.runtime.activate.assert_called_once()
        self.assertTrue(self.d.play.isEnabled())
        self.runtime.close.reset_mock()
        self.d.transport.setCurrentIndex(self.d.transport.findData("sse"))
        self.wait_until(lambda: not self.d._local_closing)
        self.runtime.close.assert_called_once()
        self.factory.assert_not_called()
    def test_deactivate_button_no_new_launch(self):
        self.runtime.ready.return_value=True
        self.d._sync_model_controls()
        self.runtime.close.side_effect=lambda: setattr(self.runtime.ready,"return_value",False)
        self.d.model_button.click()
        self.wait_until(lambda: not self.d._local_closing)
        self.runtime.close.assert_called_once()
        self.runtime.activate.assert_not_called()
        self.assertTrue(self.d.play.isEnabled())
    def test_deactivation_does_not_block_qt_or_unlock_early(self):
        import time
        from PySide6.QtCore import QTimer
        self.runtime.ready.return_value=True
        entered, release = threading.Event(), threading.Event()
        self.runtime.close.side_effect=lambda: (entered.set(), release.wait(3))
        beats=[]
        timer=QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: beats.append(1))
        timer.start()
        self.d._sync_model_controls()
        try:
            start=time.monotonic()
            self.d.model_button.click()
            self.assertLess(time.monotonic()-start,.2)
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.d.busy)
            self.assertFalse(self.d.play.isEnabled())
            self.assertFalse(self.d.start_shortcut("Synthetic","de","qwen_cf050"))
            self.wait_until(lambda: bool(beats))
            self.assertTrue(self.d._local_closing)
        finally:
            release.set()
            self.wait_until(lambda: not self.d._local_closing)
            timer.stop()
        self.assertTrue(self.d.play.isEnabled())
        self.runtime.activate.assert_not_called()

    def test_failed_cleanup_does_not_unlock_or_fall_back(self):
        self.runtime.ready.return_value=True
        self.runtime.close.side_effect=OSError("private error")
        self.d._sync_model_controls()
        self.d.model_button.click()
        self.wait_until(lambda: not self.d._local_closing)
        self.assertTrue(self.d.busy)
        self.assertEqual(self.d.status.text(),"QWEN_CLEANUP_FAILED")
        self.assertFalse(self.d.start_shortcut("Synthetic","de","sse"))
        self.factory.assert_not_called()
        self.runtime.activate.assert_not_called()

    def test_stop_all_unloads_idle_ready_model(self):
        self.d._local_used = True
        self.runtime.ready.return_value = True
        self.runtime.close.side_effect = lambda: setattr(self.runtime.ready, "return_value", False)
        self.d.stop_all()
        self.wait_until(lambda: not self.d._local_closing)
        self.runtime.close.assert_called_once()
        self.runtime.activate.assert_not_called()
        self.assertFalse(self.runtime.ready("qwen_cf050"))

    def test_local_stream_uses_only_local_runtime(self):
        self.runtime.ready.return_value=True
        self.runtime.stream.side_effect=lambda mode,text,lang,emit,stop: None
        def inline(*,target,**kwargs): return SimpleNamespace(start=target)
        with patch("voxbridge.tts_ui.threading.Thread",side_effect=inline):
            self.assertTrue(self.d.start_shortcut("Synthetic","de","qwen_cf050"))
        self.runtime.stream.assert_called_once()
        self.assertEqual(self.runtime.stream.call_args.args[:3],("qwen_cf050","Synthetic","de"))
        self.factory.assert_not_called()
    def test_config_accepts_both_variants(self):
        for mode in MODELS:
            self.assertEqual(Settings(tts_transport=mode).validate().tts_transport,mode)


class ImmutableWorkerTests(unittest.TestCase):
    def test_explicit_runtime_paths_without_sibling_checkout(self):
        import hashlib
        import tempfile
        from voxbridge.qwen_local_runtime import verify_files, runtime_files, voice_assets
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python, worker, reference = (root / name for name in ("python.exe", "worker.py", "voice.wav"))
            python.touch()
            worker.write_bytes(b"synthetic reviewed worker")
            reference.touch()
            adapter = root / "adapter.safetensors"
            adapter.touch()
            digest = hashlib.sha256(worker.read_bytes()).hexdigest()
            settings = {"LOCALAPPDATA": str(root / "profile"),
                        "VOXBRIDGE_QWEN_PYTHON": str(python),
                        "VOXBRIDGE_QWEN_WORKER": str(worker),
                        "VOXBRIDGE_QWEN_REFERENCE": str(reference),
                        "VOXBRIDGE_QWEN_CF50_ADAPTER": str(adapter),
                        "OPENAI_API_KEY": "synthetic"}
            with patch.dict(os.environ, settings, clear=True), \
                 patch("voxbridge.qwen_local_runtime.WORKER_SHAS", (digest,)), \
                 patch("voxbridge.qwen_local_runtime.subprocess.run") as git:
                selected_python, cached_worker = verify_files("qwen_base")
                env = child_env("qwen_base", "x" * 32, "test")
                self.assertEqual(voice_assets(runtime_files()[2]), (reference, adapter))
                git.assert_not_called()
            self.assertEqual(selected_python, python)
            self.assertEqual(cached_worker.read_bytes(), worker.read_bytes())
            self.assertEqual(env["OMOI_MINI_QWEN_REFERENCE"], str(reference))
            self.assertEqual(env["OMOI_MINI_QWEN_CF50_ADAPTER"], str(adapter))
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_explicit_worker_never_uses_git_fallback(self):
        import tempfile
        from voxbridge.qwen_local_runtime import immutable_worker
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "worker.py"
            worker.write_bytes(b"unreviewed")
            with patch("voxbridge.qwen_local_runtime.subprocess.run") as git:
                with self.assertRaisesRegex(LocalTTSFailure, "WORKER_CHANGED"):
                    immutable_worker(worker, Path(tmp) / "cache", allow_git_fallback=False)
                git.assert_not_called()

    def test_invalid_explicit_paths_never_use_legacy_default(self):
        from voxbridge.qwen_local_runtime import runtime_files, voice_assets
        with patch.dict(os.environ, {"VOXBRIDGE_QWEN_PYTHON": ""}):
            with self.assertRaisesRegex(LocalTTSFailure, "ASSET_PATH_INVALID"):
                runtime_files()
        with patch.dict(os.environ, {"VOXBRIDGE_QWEN_REFERENCE": "relative.wav"}):
            with self.assertRaisesRegex(LocalTTSFailure, "ASSET_PATH_INVALID"):
                voice_assets(Path("legacy"))

    def test_changed_worktree_uses_only_verified_committed_blob(self):
        import hashlib
        import tempfile
        from voxbridge.qwen_local_runtime import immutable_worker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            worker=root/"Mini"/"local-tts"/"qwen_server.py"
            worker.parent.mkdir(parents=True)
            worker.write_bytes(b"unapproved working tree")
            approved=b"audited synthetic bytes"
            digest=hashlib.sha256(approved).hexdigest()
            with patch("voxbridge.qwen_local_runtime.WORKER_SHAS",(digest,)), \
                 patch("voxbridge.qwen_local_runtime.PINNED_MINI_BLOB_SHA",digest), \
                 patch("voxbridge.qwen_local_runtime.subprocess.run",return_value=SimpleNamespace(stdout=approved)) as git:
                resolved=immutable_worker(worker,root/"cache")
                self.assertEqual(resolved.read_bytes(),approved)
                self.assertNotEqual(resolved,worker)
                self.assertEqual(worker.read_bytes(),b"unapproved working tree")
                git.assert_called_once()
            with patch("voxbridge.qwen_local_runtime.subprocess.run",return_value=SimpleNamespace(stdout=b"other")):
                with self.assertRaisesRegex(LocalTTSFailure,"WORKER_CHANGED"):
                    immutable_worker(worker,root/"cache")
