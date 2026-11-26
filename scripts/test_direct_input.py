"""One-shot, explicitly armed real caret-insertion test. No audio/API/ML.

User must click a disposable editable field and press Ctrl+Alt+F10.
Never auto-submit a message. No clipboard, saved text, or private content read.
"""
from __future__ import annotations

import argparse
import os
import queue
import threading
import time

from voxbridge.edit_target import run_edit_job
from voxbridge.hotkeys import HotkeyListener
from voxbridge.injection import InjectionError
from voxbridge.selection import capture_target, SelectionFailure

SAMPLE = "VoxBridge Test: Gr\u00fc\u00dfe, \u041f\u0440\u0438\u0432\u0435\u0442."
HOTKEY = "Ctrl+Alt+F10"


def exercise(hwnd, cancel, *, capture=capture_target, job=run_edit_job):
    target = capture(hwnd)
    identity = job(target, cancel)
    # Allow release of hotkey modifiers; final job rechecks target and modifiers.
    if cancel.wait(.2):
        raise InjectionError("EDIT_CANCELLED")
    job(target, cancel, expected=identity, text=SAMPLE, timeout=15)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Authorize one synthetic insertion after pressing Ctrl+Alt+F10.")
    args = parser.parse_args(argv)
    if os.name != "nt" or not args.arm:
        parser.error("Windows and explicit --arm required")
    pending = queue.Queue(maxsize=1)
    cancel = threading.Event()

    def on_hotkey(hwnd):
        try:
            pending.put_nowait(hwnd)
        except queue.Full:
            pass

    listener = HotkeyListener(HOTKEY, on_hotkey)
    print("Click a disposable editable field, place caret, press Ctrl+Alt+F10 and release keys.")
    print("The test expires after 60 seconds. No microphone or cloud use.", flush=True)
    try:
        listener.start()
        try:
            hwnd = pending.get(timeout=60)
        except queue.Empty:
            print("TEST_NO_HOTKEY")
            return 2
        finally:
            listener.stop()
            if not listener.wait_closed():
                raise RuntimeError('HOTKEY_CLEANUP_PENDING')
        exercise(hwnd, cancel)
        print("DIRECT_INPUT_EVENTS_SENT; verify visible text at caret; not automatic acceptance.")
        return 0
    except (InjectionError, SelectionFailure, RuntimeError, OSError) as exc:
        code = str(exc)
        print(code if code.isascii() and code.isupper() and len(code) <= 80 else "DIRECT_INPUT_TEST_FAILED")
        return 2
    except KeyboardInterrupt:
        cancel.set()
        print("TEST_CANCELLED")
        return 2
    finally:
        cancel.set()
        listener.stop()


if __name__ == "__main__":
    raise SystemExit(main())
