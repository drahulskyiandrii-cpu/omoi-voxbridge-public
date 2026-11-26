"""CPU/Qt contract: no simultaneous dictation and live cloud captions."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from voxbridge.app import VoxBridge
from voxbridge.session import State


class DictationInterlockTests(unittest.TestCase):
    def test_hotkey_does_not_start_recording_during_live_captions(self):
        target = NS(
            session=NS(snapshot=lambda: NS(state=State.IDLE)),
            live_captions_dialog=NS(_busy=True),
            recorder=Mock(),
            notify=Mock(),
        )
        VoxBridge.on_hotkey(target, 345)
        target.recorder.start.assert_not_called()
        target.notify.assert_called_once()
        self.assertIn("gesperrt", target.notify.call_args.args[0])

    def test_hotkey_does_not_start_when_cloud_worker_finishing_after_stop(self):
        target = NS(
            session=NS(snapshot=lambda: NS(state=State.IDLE)),
            live_captions_dialog=NS(_busy=True, _source_done=True,
                                    _translation_alive=True),
            recorder=Mock(),
            notify=Mock(),
        )
        VoxBridge.on_hotkey(target, 345)
        target.recorder.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
