"""Selection-only synthetic UIA and IPC tests. No actual windows or clipboard."""
import multiprocessing
import threading
import time
import unittest
from unittest.mock import Mock

from voxbridge.selection import (
    SelectionTarget, SelectionFailure, read_selection, capture_selection,
    UIA_IS_PASSWORD, UIA_TEXT_PATTERN,
)
from voxbridge.analysis import MAX_TEXT_CHARS

TARGET = SelectionTarget(100,101,900,800)


def worker_text(sender, target):
    sender.send_bytes(b"Tsynthetic selection")
    sender.close()


def worker_hang(sender, target):
    try:
        time.sleep(20)
    finally:
        sender.close()


def worker_private_error(sender, target):
    sender.send_bytes(b"Eprivate diagnostic")
    sender.close()


class SelectionContractTests(unittest.TestCase):
    def setup_fake(self):
        self.auto = Mock()
        self.element = Mock()
        self.element.GetCurrentPropertyValueEx.return_value = False
        self.element.CurrentProcessId = TARGET.process_id
        self.element.CurrentHasKeyboardFocus = True
        self.element.CurrentNativeWindowHandle = TARGET.focus
        self.auto.GetFocusedElement.return_value = self.element
        self.auto.CompareElements.return_value = True
        self.pattern = self.element.GetCurrentPattern.return_value.QueryInterface.return_value
        self.ranges = self.pattern.GetSelection.return_value
        self.ranges.Length = 1
        self.selected = self.ranges.GetElement.return_value
        self.selected.GetEnclosingElement.return_value = self.element
        self.selected.GetChildren.return_value.Length = 0
        self.selected.GetText.return_value = "synthetic"
        self.selected.Compare.return_value = True
        self.verify = Mock(return_value=TARGET)

    def read(self):
        return read_selection(self.auto, TARGET, verify_target=self.verify,
                              text_pattern_interface=object())

    def test_only_selection_read_bounded_and_no_document_or_clipboard(self):
        self.setup_fake()
        self.assertEqual(self.read(), "synthetic")
        self.selected.GetText.assert_called_once_with(MAX_TEXT_CHARS + 1)
        self.element.GetCurrentPropertyValueEx.assert_called_with(UIA_IS_PASSWORD, True)
        self.element.GetCurrentPattern.assert_called_once_with(UIA_TEXT_PATTERN)
        self.pattern.DocumentRange.assert_not_called()

    def test_password_unknown_and_other_process_never_read(self):
        for password in (True,None,0,object()):
            self.setup_fake()
            self.element.GetCurrentPropertyValueEx.return_value = password
            with self.assertRaisesRegex(SelectionFailure,"PROTECTED_OR_UNKNOWN"):
                self.read()
            self.selected.GetText.assert_not_called()
        self.setup_fake()
        self.element.CurrentProcessId = 901
        with self.assertRaisesRegex(SelectionFailure,"PROTECTED_OR_UNKNOWN"):
            self.read()
        self.selected.GetText.assert_not_called()

    def test_multiple_ranges_embedded_control_and_wrong_container(self):
        for case in ("multiple","embedded","enclosing"):
            self.setup_fake()
            if case == "multiple":
                self.ranges.Length=2
            elif case == "embedded":
                self.selected.GetChildren.return_value.Length=1
            else:
                self.auto.CompareElements.return_value=False
            with self.assertRaises(SelectionFailure):
                self.read()
            self.selected.GetText.assert_not_called()

    def test_focus_change_before_and_after_read(self):
        other=SelectionTarget(200,201,902,800)
        self.setup_fake()
        self.verify.side_effect=[TARGET,other]
        with self.assertRaisesRegex(SelectionFailure,"TARGET_CHANGED"):
            self.read()
        self.selected.GetText.assert_not_called()
        self.setup_fake()
        self.verify.side_effect=[TARGET,TARGET,other]
        with self.assertRaisesRegex(SelectionFailure,"TARGET_CHANGED"):
            self.read()
        self.selected.GetText.assert_called_once()

    def test_selection_changed_or_oversized_rejected(self):
        self.setup_fake()
        self.selected.Compare.return_value=False
        with self.assertRaisesRegex(SelectionFailure,"TARGET_CHANGED"):
            self.read()
        for text in ("", "x"*(MAX_TEXT_CHARS+1), "x"+chr(0)):
            self.setup_fake()
            self.selected.GetText.return_value=text
            with self.assertRaisesRegex(SelectionFailure,"INVALID_TEXT"):
                self.read()


class SelectionIPCTests(unittest.TestCase):
    def assert_reaped(self):
        self.assertFalse(any(p.name=="voxbridge-selection"
                             for p in multiprocessing.active_children()))

    def test_synthetic_ipc_success(self):
        self.assertEqual(capture_selection(TARGET,threading.Event(),_worker=worker_text),
                         "synthetic selection")
        self.assert_reaped()

    def test_timeout_reaps_only_owned_child(self):
        with self.assertRaisesRegex(SelectionFailure,"TIMEOUT"):
            capture_selection(TARGET,threading.Event(),timeout=.3,_worker=worker_hang)
        self.assert_reaped()

    def test_precancel_and_sanitized_invalid_packet(self):
        stop=threading.Event()
        stop.set()
        with self.assertRaisesRegex(SelectionFailure,"CANCELLED"):
            capture_selection(TARGET,stop,_worker=worker_hang)
        with self.assertRaisesRegex(SelectionFailure,"PROTOCOL"):
            capture_selection(TARGET,threading.Event(),_worker=worker_private_error)
        self.assert_reaped()
