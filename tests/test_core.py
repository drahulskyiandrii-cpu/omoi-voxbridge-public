import unittest
from unittest.mock import patch
from voxbridge.audio import pcm_to_wav
from voxbridge.session import Session, State
from voxbridge.hotkeys import parse_hotkey, MOD_CONTROL, MOD_ALT, MOD_NOREPEAT
from voxbridge.injection import key_events, KEYEVENTF_UNICODE, KEYEVENTF_KEYUP, InjectionError
from voxbridge.gpu import parse_snapshot, require_free_memory, GpuSnapshot, GpuBusy

class TestSession(unittest.TestCase):
    def test_transitions(self):
        s = Session()
        g = s.begin(1234)
        self.assertEqual(s.snapshot().state, State.RECORDING)
        self.assertTrue(s.advance(g, State.RECORDING, State.RECOGNIZING))
        self.assertFalse(s.advance(g, State.RECORDING, State.INSERTING))
        self.assertTrue(s.advance(g, State.RECOGNIZING, State.INSERTING))
        self.assertTrue(s.finish(g))
        self.assertEqual(s.snapshot().state, State.IDLE)

    def test_cancel_rejects_stale_worker(self):
        s = Session()
        old = s.begin(1234)
        newer = s.cancel()
        self.assertGreater(newer, old)
        self.assertFalse(s.advance(old, State.RECORDING, State.RECOGNIZING))
        self.assertEqual(s.snapshot().target_hwnd, 0)

    def test_cannot_overlap(self):
        s = Session()
        with self.assertRaises(ValueError):
            s.begin(0)
        s.begin(100)
        with self.assertRaises(ValueError):
            s.begin(101)

class TestInput(unittest.TestCase):
    def test_hotkey(self):
        self.assertEqual(parse_hotkey("Ctrl+Alt+Space"), (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 32))
        self.assertEqual(parse_hotkey("Ctrl+Shift+D")[1], ord("D"))
        for text in ("Space", "Alt+Alt+F8", "Ctrl+INVALID", "Win+"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_hotkey(text)

    def test_layout_safe_function_keys_and_legacy_settings(self):
        from voxbridge.config import Settings, layout_safe_hotkeys
        from dataclasses import replace
        old = Settings()
        self.assertEqual(old.copy_read_hotkey, "Ctrl+Alt+Shift+S")
        safe = layout_safe_hotkeys(replace(old, language="ru"))
        self.assertEqual(safe.language, "ru")
        self.assertEqual(safe.hotkey, "Ctrl+Shift+F1")
        self.assertEqual(safe.read_selection_hotkey, "Ctrl+Shift+F6")
        self.assertEqual(safe.copy_read_hotkey, "Ctrl+Shift+F7")
        for i in range(1, 13):
            self.assertEqual(parse_hotkey(f"Ctrl+Shift+F{i}")[1], 0x6F+i)

    def test_unicode_surrogate(self):
        events = key_events("Б🙂")
        self.assertEqual(len(events), 6)
        self.assertEqual(events[0].u.ki.dwFlags, KEYEVENTF_UNICODE)
        self.assertEqual(events[1].u.ki.dwFlags, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        self.assertEqual(events[2].u.ki.wScan, 0xD83D)
        self.assertEqual(events[4].u.ki.wScan, 0xDE42)
        with self.assertRaises(InjectionError):
            key_events("")

    def test_wav(self):
        import io
        import wave
        raw = bytes([1, 0]) * 3200
        with wave.open(io.BytesIO(pcm_to_wav(raw)), "rb") as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.readframes(3200), raw)
        with self.assertRaises(ValueError):
            pcm_to_wav(b"x")

class TestGpu(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_snapshot("16311, 394, 20").free_mib, 15917)
        with self.assertRaises(GpuBusy):
            parse_snapshot("N/A")
    def test_ambiguous_or_invalid_gpu_inventory_fails_closed(self):
        for value in ("16000, 100, 0\n8000, 7900, 100", "8000, 9000, 0",
                      "8000, 0, 101", "8000, 0, 0, 1"):
            with self.subTest(value=value), self.assertRaises(GpuBusy):
                parse_snapshot(value)

    def test_guard(self):
        with patch("voxbridge.gpu.snapshot", return_value=GpuSnapshot(16311, 15000, 20)):
            with self.assertRaises(GpuBusy):
                require_free_memory()
        with patch("voxbridge.gpu.snapshot", return_value=GpuSnapshot(16311, 500, 75)):
            with self.assertRaises(GpuBusy):
                require_free_memory()
        with patch("voxbridge.gpu.snapshot", return_value=GpuSnapshot(16311, 500, 10)):
            self.assertGreater(require_free_memory().free_mib, 8000)
