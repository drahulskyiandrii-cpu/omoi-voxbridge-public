"""Explicit native hotkey ownership test; no keys sent, capture or network."""
import argparse
import os
import time

from voxbridge.hotkeys import HotkeyListener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register-test-hotkey", action="store_true", required=True,
                        help="Temporarily register Ctrl+Alt+F11")
    parser.parse_args()
    if os.name != "nt":
        parser.error("Windows required")
    # The callback intentionally does nothing. Do not record foreground HWNDs.
    first = HotkeyListener("Ctrl+Alt+F11", lambda _: None)
    second = HotkeyListener("Ctrl+Alt+F11", lambda _: None)
    try:
        first.start()
        if not first.running:
            raise RuntimeError("HOTKEY_LISTENER_EXITED")
        try:
            second.start()
        except RuntimeError as exc:
            if str(exc) != "HOTKEY_IN_USE_OR_UNAVAILABLE":
                raise
        else:
            raise RuntimeError("HOTKEY_DUPLICATE_REGISTERED")
        if not second.wait_closed(1):
            raise RuntimeError("HOTKEY_CLEANUP_PENDING")
        start = time.perf_counter()
        first.stop()
        if not first.wait_closed(1):
            raise RuntimeError("HOTKEY_CLEANUP_PENDING")
        print("RELEASE_MS", round((time.perf_counter() - start)*1000, 2))
        second.start()
        if not second.running:
            raise RuntimeError("HOTKEY_LISTENER_EXITED")
        second.stop()
        if not second.wait_closed(1):
            raise RuntimeError("HOTKEY_CLEANUP_PENDING")
        print("NATIVE_REGISTER_CONFLICT_RELEASE_REREGISTER_PASS")
        return 0
    finally:
        first.stop()
        second.stop()
        first.wait_closed(1)
        second.wait_closed(1)


if __name__ == "__main__":
    raise SystemExit(main())
