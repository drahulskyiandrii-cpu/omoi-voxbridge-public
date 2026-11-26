"""Logical-focus dictation target contracts, synthetic only."""
import json
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS

from voxbridge.edit_target import (focused_edit_identity, EditIdentity, run_edit_job,
                                   EDIT, DOCUMENT, IS_PASSWORD, VALUE_READONLY, TEXT_READONLY)
from voxbridge.selection import SelectionTarget
from voxbridge.injection import InjectionError, insert_text
from voxbridge.app import VoxBridge
from voxbridge.session import Session, State


TARGET = SelectionTarget(100, 101, 900, 901)


def mock_child(sender, stop, target, expected, text):
    try:
        if stop.is_set():
            return
        if text is not None and text != "synthetic single paragraph":
            sender.send_bytes(b'{"error":"UNEXPECTED_TEST_TEXT"}')
        else:
            sender.send_bytes(json.dumps({"runtime_id":[42,1],"element_pid":902}).encode("ascii"))
    finally:
        sender.close()


def slow_child(sender, stop, target, expected, text):
    try:
        stop.wait(5)
    finally:
        sender.close()


class LogicalTargetTests(unittest.TestCase):
    def setUp(self):
        self.automation=Mock()
        self.element=Mock(CurrentHasKeyboardFocus=True, CurrentIsEnabled=True,
                          CurrentControlType=EDIT, CurrentProcessId=902, CurrentNativeWindowHandle=0,
                          CurrentIsPassword=False)
        self.element.GetRuntimeId.return_value=(42,1)
        self.element.GetCurrentPropertyValueEx.side_effect=lambda prop, _: False if prop in (IS_PASSWORD,VALUE_READONLY) else None
        self.root=Mock(CurrentNativeWindowHandle=100,CurrentProcessId=900,CurrentIsPassword=False)
        self.root.GetCurrentPropertyValueEx.return_value=False
        self.automation.GetFocusedElement.return_value=self.element
        self.automation.RawViewWalker.GetParentElement.return_value=self.root
        self.automation.CompareElements.return_value=True
        self.normal=Mock()
        self.current=Mock()

    def verify(self, expected=None):
        return focused_edit_identity(self.automation,TARGET,verify_current=self.current,
            require_normal=self.normal,text_interface=object(),expected=expected)

    def test_browser_renderer_with_native_ancestry(self):
        identity=self.verify()
        self.assertEqual(identity,EditIdentity(TARGET,(42,1),902))
        self.normal.assert_called_once_with(902)
        self.element.GetCurrentPattern.assert_not_called()
        self.assertEqual(self.current.call_count,2)

    def test_document_readonly_attribute_not_content(self):
        self.element.CurrentControlType=DOCUMENT
        self.element.GetCurrentPropertyValueEx.side_effect=lambda prop,_: False if prop==IS_PASSWORD else None
        pattern=self.element.GetCurrentPattern.return_value.QueryInterface.return_value
        ranges=pattern.GetSelection.return_value
        ranges.Length=1
        caret=ranges.GetElement.return_value
        caret.GetAttributeValue.return_value=False
        self.assertEqual(self.verify().runtime_id,(42,1))
        caret.GetAttributeValue.assert_called_once_with(TEXT_READONLY)
        caret.GetText.assert_not_called()
        pattern.DocumentRange.GetText.assert_not_called()
        caret.GetAttributeValue.return_value=True
        with self.assertRaisesRegex(InjectionError,"READONLY"):
            self.verify()

    def test_default_password_property_on_conforming_edit_provider(self):
        self.element.GetCurrentPropertyValueEx.side_effect=lambda prop,_: None if prop==IS_PASSWORD else False
        self.assertEqual(self.verify().runtime_id,(42,1))
        self.element.CurrentIsPassword=True
        with self.assertRaisesRegex(InjectionError,"PROTECTED"):
            self.verify()

    def test_explicit_password_or_readonly_blocks(self):
        for password,readonly in ((True,False),(False,True)):
            self.element.GetCurrentPropertyValueEx.side_effect=lambda prop,_: password if prop==IS_PASSWORD else readonly
            with self.assertRaises(InjectionError):
                self.verify()

    def test_changed_logical_field_same_native_window(self):
        old=self.verify()
        self.element.GetRuntimeId.return_value=(42,2)
        with self.assertRaisesRegex(InjectionError,"TEXT_FIELD_CHANGED"):
            self.verify(old)

    def test_unrelated_element_rejected(self):
        self.root.CurrentNativeWindowHandle=999
        self.automation.RawViewWalker.GetParentElement.side_effect=[self.root,None]
        with self.assertRaisesRegex(InjectionError,"TARGET_UNVERIFIED"):
            self.verify()

    def test_wrong_control_and_lost_focus(self):
        self.element.CurrentControlType=50000
        with self.assertRaisesRegex(InjectionError,"NO_EDITABLE"):
            self.verify()
        self.element.CurrentControlType=EDIT
        self.automation.CompareElements.return_value=False
        with self.assertRaisesRegex(InjectionError,"TEXT_FIELD_CHANGED"):
            self.verify()

    def test_password_ancestor_blocks(self):
        self.root.GetCurrentPropertyValueEx.return_value=True
        with self.assertRaisesRegex(InjectionError,"PROTECTED"):
            self.verify()

    def test_invalid_runtime_id_rejected(self):
        self.element.GetRuntimeId.return_value=(1,"private")
        with self.assertRaisesRegex(InjectionError,"IDENTITY_UNVERIFIED"):
            self.verify()


class IsolatedTargetTests(unittest.TestCase):
    def test_spawn_identity_and_single_paragraph_contract(self):
        stop=threading.Event()
        ident=run_edit_job(TARGET,stop,_worker=mock_child)
        self.assertEqual(ident,EditIdentity(TARGET,(42,1),902))
        self.assertEqual(run_edit_job(TARGET,stop,expected=ident,text="synthetic\nsingle\tparagraph",
                                     _worker=mock_child),ident)

    def test_timeout_preflight_and_insertion_uncertain(self):
        with self.assertRaisesRegex(InjectionError,"EDIT_CHECK_TIMEOUT"):
            run_edit_job(TARGET,threading.Event(),timeout=.2,_worker=slow_child)
        with self.assertRaisesRegex(InjectionError,"PARTIAL_INSERT_POSSIBLE"):
            run_edit_job(TARGET,threading.Event(),expected=EditIdentity(TARGET,(42,1),902),
                         text="synthetic",timeout=.2,_worker=slow_child)

    def test_one_shot_test_uses_same_verified_target_and_fixed_sample(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location("direct_input_test_script",
            Path(__file__).resolve().parents[1]/"scripts"/"test_direct_input.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        capture=Mock(return_value=TARGET)
        identity=EditIdentity(TARGET,(42,1),902)
        job=Mock(return_value=identity)
        stop=threading.Event()
        module.exercise(100,stop,capture=capture,job=job)
        capture.assert_called_once_with(100)
        self.assertEqual(job.call_count,2)
        self.assertEqual(job.call_args.kwargs,{"expected":identity,"text":module.SAMPLE,"timeout":15})

    def test_pre_cancel_no_child(self):
        stop=threading.Event()
        stop.set()
        with patch("voxbridge.edit_target.multiprocessing.get_context") as ctx:
            with self.assertRaisesRegex(InjectionError,"EDIT_CANCELLED"):
                run_edit_job(TARGET,stop)
        ctx.assert_not_called()


class AppEditTests(unittest.TestCase):
    def fixture(self):
        s=Session()
        g=s.begin(TARGET.hwnd,TARGET.focus)
        s.advance(g,State.RECORDING,State.VERIFYING_TARGET)
        a=NS(session=s,_edit_worker_generation=g,_edit_identity=None,_shutting_down=False,
             _edit_cancel=threading.Event(),recorder=Mock(),status=Mock(),deadline=Mock(),
             settings=NS(hotkey="Ctrl+Alt+Space",max_record_seconds=25),
             notify=Mock(),signals=Mock())
        a.mic_deadline=Mock()
        a.mic=Mock()
        def start(generation):
            a.recorder.start()
            VoxBridge._microphone_ready(a,generation,"")
        a.mic.start.side_effect=start
        a._edit_ready=lambda *args: VoxBridge._edit_ready(a,*args)
        a._insertion_ready=lambda *args: VoxBridge._insertion_ready(a,*args)
        return a,g

    def test_preflight_required_before_mic(self):
        a,g=self.fixture()
        ident=EditIdentity(TARGET,(42,1),902)
        with patch("voxbridge.app.capture_target",return_value=TARGET):
            a._edit_ready(g,ident,"")
        a.recorder.start.assert_called_once()
        self.assertEqual(a.session.snapshot().state,State.RECORDING)
        self.assertEqual(a._edit_identity,ident)

    def test_failed_preflight_no_mic(self):
        a,g=self.fixture()
        a._edit_ready(g,None,"UIA_NO_EDITABLE_FIELD")
        a.recorder.start.assert_not_called()
        self.assertEqual(a.session.snapshot().state,State.IDLE)

    def test_cancelled_preflight_no_late_recording(self):
        a,g=self.fixture()
        a.session.cancel()
        a._edit_ready(g,EditIdentity(TARGET),"")
        a.recorder.start.assert_not_called()
        self.assertIsNone(a._edit_worker_generation)

    def test_successful_insert_no_popup_and_stale_error_no_popup(self):
        a,g=self.fixture()
        a.session.advance(g,State.VERIFYING_TARGET,State.INSERTING)
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            a._insertion_ready(g,"synthetic","")
            fallback.assert_not_called()
            self.assertEqual(a.session.snapshot().state,State.IDLE)
            a._edit_worker_generation=g
            a.session.cancel()
            a._insertion_ready(g,"synthetic","EDIT_CHECK_FAILED")
            fallback.assert_not_called()

    def test_failure_fallback_only_on_current_generation(self):
        a,g=self.fixture()
        a.session.advance(g,State.VERIFYING_TARGET,State.INSERTING)
        with patch("voxbridge.app.show_manual_fallback") as fallback:
            a._insertion_ready(g,"synthetic","TEXT_FIELD_CHANGED")
            fallback.assert_called_once_with("synthetic","TEXT_FIELD_CHANGED")
        self.assertEqual(a.session.snapshot().state,State.IDLE)


class CompatibilityTests(unittest.TestCase):
    setUp = LogicalTargetTests.setUp
    verify = LogicalTargetTests.verify
    def test_related_native_anchor_allows_real_intermediate_window(self):
        self.root.CurrentNativeWindowHandle=110
        self.automation.RawViewWalker.GetParentElement.side_effect=[self.root,None]
        link=Mock(return_value=True)
        identity=focused_edit_identity(self.automation,TARGET,verify_current=self.current,
            require_normal=self.normal,text_interface=object(),native_link=link)
        self.assertEqual(identity.runtime_id,(42,1))
        link.assert_called_once_with(110,TARGET)

    def test_unrelated_native_anchor_not_accepted(self):
        self.root.CurrentNativeWindowHandle=110
        self.automation.RawViewWalker.GetParentElement.side_effect=[self.root,None]
        with self.assertRaisesRegex(InjectionError,"TARGET_UNVERIFIED"):
            focused_edit_identity(self.automation,TARGET,verify_current=self.current,
                require_normal=self.normal,text_interface=object(),native_link=lambda *_:False)

    def test_text_pattern2_fallback(self):
        self.element.CurrentControlType=DOCUMENT
        self.element.GetCurrentPropertyValueEx.side_effect=lambda p,_: False if p==IS_PASSWORD else None
        pattern=Mock()
        self.element.GetCurrentPattern.side_effect=lambda p: None if p==10014 else pattern
        ranges=pattern.QueryInterface.return_value.GetSelection.return_value
        ranges.Length=1
        ranges.GetElement.return_value.GetAttributeValue.return_value=False
        self.assertEqual(self.verify().runtime_id,(42,1))
        self.assertEqual([c.args[0] for c in self.element.GetCurrentPattern.call_args_list],[10014,10024])

class LocalManualFallbackTests(unittest.TestCase):
    fixture = AppEditTests.fixture
    def local_fixture(self,backend="mini_worker"):
        a,g=self.fixture()
        a.settings.stt_backend=backend
        a.settings.local_partial_enabled=True
        a._local_stt_armed=True
        a._dictation_target=TARGET
        a._start_local_partial=Mock()
        return a,g

    def test_unverified_target_still_starts_cuda_recording(self):
        a,g=self.local_fixture()
        with patch("voxbridge.app.capture_target",return_value=TARGET):
            a._edit_ready(g,None,"UIA_TARGET_UNVERIFIED")
        a.recorder.start.assert_called_once()
        a._start_local_partial.assert_called_once_with(g)
        self.assertIsNone(a._edit_identity)
        self.assertEqual(a.session.snapshot().state,State.RECORDING)
        self.assertEqual(a._manual_insert_reason,"UIA_TARGET_UNVERIFIED")
        a.session.advance(g,State.RECORDING,State.INSERTING)
        with patch("voxbridge.app.show_manual_fallback") as fallback, patch("voxbridge.app.run_edit_job") as job:
            VoxBridge.try_insert(a,g,"Synthetic result")
            fallback.assert_called_once_with("Synthetic result","UIA_TARGET_UNVERIFIED")
            job.assert_not_called()
        self.assertEqual(a.session.snapshot().state,State.IDLE)

    def test_cloud_or_protected_target_still_blocked(self):
        for backend,error in (("openai_cloud","UIA_TARGET_UNVERIFIED"),
                              ("mini_worker","UIA_PROTECTED_OR_UNVERIFIED"),
                              ("mini_worker","PASSWORD_FIELD_BLOCKED"),
                              ("mini_worker","READONLY_FIELD_BLOCKED")):
            a,g=self.local_fixture(backend)
            with patch("voxbridge.app.capture_target",return_value=TARGET):
                a._edit_ready(g,None,error)
            a.recorder.start.assert_not_called()
            a._start_local_partial.assert_not_called()

    def test_changed_target_or_unarmed_local_does_not_record(self):
        for armed in (True,False):
            a,g=self.local_fixture()
            a._local_stt_armed=armed
            with patch("voxbridge.app.capture_target",return_value=SelectionTarget(100,102,900,901)):
                a._edit_ready(g,None,"UIA_TARGET_UNVERIFIED")
            a.recorder.start.assert_not_called()
            a._start_local_partial.assert_not_called()
