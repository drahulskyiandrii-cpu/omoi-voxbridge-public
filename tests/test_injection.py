"""Synthetic Win32 boundary tests: no real SendInput or focus access."""
import unittest
from unittest.mock import Mock, patch
from voxbridge.injection import (
    InjectionError, insert_text, key_events, _verify_target, _native_edit_style,
    ES_PASSWORD, ES_READONLY,
)


class InjectionTests(unittest.TestCase):
    def setUp(self):
        self.u = Mock()
        self.u.IsWindow.return_value = True
        self.u.GetForegroundWindow.return_value = 100
        self.u.GetAsyncKeyState.return_value = 0
        self.u.SendInput.side_effect = lambda n, arr, size: n
        self.focus = patch("voxbridge.injection._focused_is_password", return_value=(False, 101)).start()
        self.style = patch("voxbridge.injection._native_edit_style", return_value=0).start()
        self.owner = patch("voxbridge.injection._require_normal_target", return_value=900).start()
        patch("voxbridge.injection.ctypes.WinDLL", return_value=self.u).start()
        self.addCleanup(patch.stopall)

    def test_recheck_focus_for_each_batch(self):
        self.assertEqual(insert_text(100, "x"*100, 101), 100)
        self.assertEqual(self.u.SendInput.call_count, 4)
        self.assertEqual(self.focus.call_count, 4)
        self.assertEqual(self.style.call_count, 4)
        self.assertEqual(self.owner.call_count, 4)

    def test_focus_missing_changed_or_password_never_sends(self):
        for focus, target in (((False,101),0), ((False,102),101), ((True,101),101)):
            self.focus.return_value = focus
            with self.assertRaises(InjectionError):
                insert_text(100, "Synthetic", target)
        self.u.SendInput.assert_not_called()

    def test_shift_and_unknown_control_blocked(self):
        self.u.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == 0x10 else 0
        with self.assertRaisesRegex(InjectionError, "MODIFIER"):
            insert_text(100, "Synthetic", 101)
        self.u.GetAsyncKeyState.side_effect = None
        self.style.side_effect = InjectionError("UNVERIFIED_TEXT_CONTROL")
        with self.assertRaisesRegex(InjectionError, "UNVERIFIED_TEXT_CONTROL"):
            insert_text(100, "Synthetic", 101)
        self.u.SendInput.assert_not_called()

    def test_focus_change_after_first_batch_reports_partial(self):
        self.focus.side_effect = [(False,101),(False,102)]
        with self.assertRaisesRegex(InjectionError, "PARTIAL_INSERT_POSSIBLE"):
            insert_text(100, "x"*100, 101)
        self.u.SendInput.assert_called_once()

    def test_process_change_after_first_batch_reports_partial(self):
        self.owner.side_effect = [900,901]
        with self.assertRaisesRegex(InjectionError,"PARTIAL_INSERT_POSSIBLE"):
            insert_text(100,"x"*100,101)
        self.u.SendInput.assert_called_once()

    def test_partial_send_reports_partial_no_retry(self):
        self.u.SendInput.side_effect = None
        self.u.SendInput.return_value = 1
        with self.assertRaisesRegex(InjectionError,"PARTIAL_INSERT_POSSIBLE"):
            insert_text(100, "Synthetic", 101)
        self.u.SendInput.assert_called_once()

    def test_surrogate_pair_not_split_across_batches(self):
        self.assertEqual(insert_text(100, "x"*31+chr(0x1f642)+"y", 101),34)
        counts = [c.args[0] for c in self.u.SendInput.call_args_list]
        self.assertEqual(counts,[62,6])

    def test_uia_logical_guard_used_before_every_batch(self):
        logical=Mock()
        self.style.side_effect=AssertionError("not a native edit")
        self.assertEqual(insert_text(100,"x"*70,101,logical_verify=logical),70)
        self.assertEqual(logical.call_count,3)
        self.assertEqual(self.u.SendInput.call_count,3)
        self.style.assert_not_called()

    def test_uia_logical_field_change_stops_after_first_batch(self):
        logical=Mock(side_effect=[None,InjectionError("TEXT_FIELD_CHANGED")])
        with self.assertRaisesRegex(InjectionError,"PARTIAL_INSERT_POSSIBLE"):
            insert_text(100,"x"*70,101,logical_verify=logical)
        self.u.SendInput.assert_called_once()

    def test_invalid_controls_rejected(self):
        for ch in (chr(0),chr(27),chr(8)):
            with self.assertRaises(InjectionError):
                key_events("a"+ch)
        self.u.SendInput.assert_not_called()


class NativeStyleTests(unittest.TestCase):
    def test_unknown_readonly_password_and_valid_edit(self):
        u = Mock()
        for name, style, error in (("Chrome_RenderWidgetHostHWND",0,"UNVERIFIED_TEXT_CONTROL"),
                                   ("Edit",ES_PASSWORD,"PASSWORD_FIELD_BLOCKED"),
                                   ("Edit",ES_READONLY,"READONLY_FIELD_BLOCKED"),
                                   ("Edit",0,None),
                                   ("RichEditD2DPT",ES_PASSWORD,"PASSWORD_FIELD_BLOCKED"),
                                   ("RichEditD2DPT",ES_READONLY,"READONLY_FIELD_BLOCKED"),
                                   ("RichEditD2DPT",0,None)):
            def get_class(hwnd, buf, size):
                buf.value = name
                return len(name)
            u.GetClassNameW.side_effect = get_class
            u.GetWindowLongW.return_value = style
            if error:
                with self.assertRaisesRegex(InjectionError,error):
                    _native_edit_style(u,101)
            else:
                self.assertEqual(_native_edit_style(u,101),0)

    def test_modern_notepad_routes_to_native_replace_not_sendinput(self):
        import threading
        from voxbridge.edit_target import _native_or_uia, EditIdentity
        from voxbridge.selection import SelectionTarget
        target=SelectionTarget(100,101,900,800)
        u=Mock()
        def get_class(hwnd, buf, size):
            buf.value="RichEditD2DPT"
            return len(buf.value)
        u.GetClassNameW.side_effect=get_class
        u.GetWindowLongW.return_value=0
        with patch("voxbridge.edit_target._require_current"), \
             patch("voxbridge.edit_target.ctypes.WinDLL",return_value=u), \
             patch("voxbridge.edit_target.replace_native_text") as native, \
             patch("voxbridge.edit_target.insert_text") as keys, \
             patch("voxbridge.edit_target._automation") as uia:
            self.assertEqual(_native_or_uia(target,EditIdentity(target),threading.Event(),"Synthetic"),EditIdentity(target))
        native.assert_called_once()
        self.assertEqual(native.call_args.args,(100,101,"Synthetic"))
        keys.assert_not_called()
        uia.assert_not_called()
