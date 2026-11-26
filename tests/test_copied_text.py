"""Clipboard fallback contract: synthetic memory only, never real clipboard."""
import ctypes
import threading
import unittest
from unittest.mock import Mock,patch
from voxbridge.copied_text import CopiedTextTarget, capture_copy_target, _read_text, capture_copied_text
from voxbridge.selection import SelectionFailure

class CopyTests(unittest.TestCase):
    def setup_fake(self,text):
        self.raw=ctypes.create_string_buffer(text.encode("utf-16-le")+b"\0\0")
        self.u=Mock()
        self.k=Mock()
        self.u.OpenClipboard.return_value=1
        self.u.IsClipboardFormatAvailable.return_value=1
        self.u.GetClipboardData.return_value=42
        self.k.GlobalSize.return_value=ctypes.sizeof(self.raw)
        self.k.GlobalLock.return_value=ctypes.addressof(self.raw)
        self.dll=patch("voxbridge.copied_text.ctypes.WinDLL",side_effect=lambda name,**kw:self.u if name=="user32" else self.k)
    def test_read_exact_copied_unicode_without_writes(self):
        self.setup_fake("Synthetic Grüß dich")
        with self.dll,patch("voxbridge.copied_text._sequence",return_value=9):
            self.assertEqual(_read_text(CopiedTextTarget(9)),"Synthetic Grüß dich")
        self.u.GetClipboardData.assert_called_once_with(13)
        self.u.CloseClipboard.assert_called_once()
        self.k.GlobalUnlock.assert_called_once_with(42)
        self.u.EmptyClipboard.assert_not_called()
        self.u.SetClipboardData.assert_not_called()
    def test_change_before_read_denied(self):
        self.setup_fake("synthetic")
        with self.dll,patch("voxbridge.copied_text._sequence",return_value=10):
            with self.assertRaisesRegex(SelectionFailure,"CHANGED"):
                _read_text(CopiedTextTarget(9))
        self.u.OpenClipboard.assert_not_called()
    def test_change_after_read_denied(self):
        self.setup_fake("synthetic")
        with self.dll,patch("voxbridge.copied_text._sequence",side_effect=[9,9,10]):
            with self.assertRaisesRegex(SelectionFailure,"CHANGED"):
                _read_text(CopiedTextTarget(9))
        self.u.CloseClipboard.assert_called_once()
    def test_non_text_and_overlimit_rejected(self):
        self.setup_fake("synthetic")
        self.u.IsClipboardFormatAvailable.return_value=0
        with self.dll,patch("voxbridge.copied_text._sequence",return_value=9):
            with self.assertRaisesRegex(SelectionFailure,"NO_TEXT"):
                _read_text(CopiedTextTarget(9))
        self.u.GetClipboardData.assert_not_called()
        self.setup_fake("x"*12001)
        with self.dll,patch("voxbridge.copied_text._sequence",return_value=9):
            with self.assertRaisesRegex(SelectionFailure,"INVALID_TEXT"):
                _read_text(CopiedTextTarget(9))
    def test_target_captures_sequence_only(self):
        with patch("voxbridge.copied_text._sequence",return_value=42),patch("voxbridge.copied_text._read_text") as read:
            self.assertEqual(capture_copy_target(None),CopiedTextTarget(42))
            read.assert_not_called()
    def test_cancelled_read_no_child(self):
        stop=threading.Event()
        stop.set()
        with self.assertRaisesRegex(SelectionFailure,"CANCELLED"):
            capture_copied_text(CopiedTextTarget(9),stop)
