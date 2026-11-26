"""Microphone choice and capture wiring tests. All PortAudio calls mocked."""
import io
import sys
import unittest
import wave
from unittest.mock import Mock, patch
from voxbridge.audio import Microphone, RATE
from voxbridge.config import Settings
from voxbridge.microphone_devices import list_inputs, selectable_inputs, resolve_input, MicrophoneError


def fake_sd():
    sd = Mock()
    sd.query_hostapis.return_value = [{"name":"Windows WASAPI"},{"name":"MME"}]
    sd.query_devices.return_value = [
        {"name":"Speaker", "hostapi":0, "max_input_channels":0},
        {"name":"USB mic", "hostapi":0, "max_input_channels":1},
        {"name":"Built-in mic", "hostapi":0, "max_input_channels":2},
    ]
    return sd


class DeviceTests(unittest.TestCase):
    def test_inputs_only_and_indices_re_resolved(self):
        sd=fake_sd()
        devices=list_inputs(sd)
        self.assertEqual([d.index for d in devices],[1,2])
        self.assertEqual(devices[0].label,"USB mic")
        key=devices[0].device_id
        sd.query_devices.return_value.reverse()
        self.assertEqual(resolve_input(sd,key),1)
        sd.query_devices.return_value.pop(0)
        self.assertEqual(resolve_input(sd,key),0)
        sd.RawInputStream.assert_not_called()

    def test_default_no_discovery(self):
        sd=fake_sd()
        self.assertIsNone(resolve_input(sd,""))
        sd.query_devices.assert_not_called()

    def test_duplicate_names_host_api_ambiguous(self):
        sd=fake_sd()
        key=list_inputs(sd)[0].device_id
        sd.query_devices.return_value.append(dict(sd.query_devices.return_value[1]))
        devices, count=selectable_inputs(sd)
        self.assertEqual(count,2)
        self.assertEqual(len(devices),1)
        with self.assertRaisesRegex(MicrophoneError,"AMBIGUOUS"):
            resolve_input(sd,key)

    def test_same_name_different_host_api_is_one_endpoint(self):
        sd=fake_sd()
        sd.query_devices.return_value[2]["name"]="USB mic"
        sd.query_devices.return_value[2]["hostapi"]=1
        devices,count=selectable_inputs(sd)
        self.assertEqual(count,0)
        self.assertEqual(len(devices),1)
        self.assertEqual(resolve_input(sd,list_inputs(sd)[1].device_id),devices[0].index)

    def test_missing_microphone_never_opens_default(self):
        sd=fake_sd()
        key=list_inputs(sd)[0].device_id
        sd.query_devices.return_value.pop(1)
        with patch.dict(sys.modules,{"sounddevice":sd}):
            with self.assertRaisesRegex(MicrophoneError,"UNAVAILABLE"):
                Microphone(microphone_id=key).start()
        sd.RawInputStream.assert_not_called()
        sd.check_input_settings.assert_not_called()

    def test_explicit_device_used_for_stream_and_format_check(self):
        sd=fake_sd()
        key=list_inputs(sd)[0].device_id
        with patch.dict(sys.modules,{"sounddevice":sd}):
            mic=Microphone(microphone_id=key)
            mic.start()
            self.assertEqual(sd.RawInputStream.call_args.kwargs["device"],1)
            sd.check_input_settings.assert_called_with(device=1,samplerate=RATE,channels=1,dtype="int16")
            callback=sd.RawInputStream.call_args.kwargs["callback"]
            callback(b"\x00\x01\x00\xff"*1600,3200,None,None)
            with wave.open(io.BytesIO(mic.stop()),"rb") as wav:
                self.assertEqual(wav.getframerate(),16000)
                self.assertEqual(wav.getnframes(),3200)
        sd.RawInputStream.return_value.close.assert_called_once()

    def test_unsupported_format_does_not_open_stream(self):
        sd=fake_sd()
        sd.check_input_settings.side_effect=RuntimeError("private driver error")
        with patch.dict(sys.modules,{"sounddevice":sd}):
            with self.assertRaisesRegex(MicrophoneError,"MIC_FORMAT_UNSUPPORTED"):
                Microphone().start()
        sd.RawInputStream.assert_not_called()

    def test_failed_start_clears_buffer_and_closes(self):
        sd=fake_sd()
        def fail():
            sd.RawInputStream.call_args.kwargs["callback"](bytes(6400),3200,None,None)
            raise RuntimeError("synthetic")
        sd.RawInputStream.return_value.start.side_effect=fail
        with patch.dict(sys.modules,{"sounddevice":sd}):
            mic=Microphone()
            with self.assertRaisesRegex(MicrophoneError,"MIC_START_FAILED"):
                mic.start()
        self.assertEqual(len(mic._buffer),0)
        self.assertIsNone(mic._stream)
        sd.RawInputStream.return_value.close.assert_called_once()

    def test_settings_reject_index_and_bad_fingerprint(self):
        for value in (1,"1","not-an-id","a"*65,None):
            with self.assertRaises(ValueError):
                Settings(microphone_id=value).validate()
        self.assertEqual(Settings().validate().microphone_id,"")

class EndpointFilterTests(unittest.TestCase):
    def fixture(self):
        sd=Mock()
        sd.query_hostapis.return_value=[{"name":x} for x in ("MME","Windows DirectSound","Windows WASAPI","Windows WDM-KS")]
        full="Microphone Array (Intel Smart Sound Technology)"
        def item(name,api):return {"name":name,"hostapi":api,"max_input_channels":2}
        sd.query_devices.return_value=[
            item("Microsoft Soundmapper - Input",0),item(full[:31],0),
            item("Primary Sound Capture Driver",1),item(full,1),item(full,2),
            item("Microphone Array 1 ()",3),item("Stereo Mix",2),
            item("Speakers",3),item("USB microphone",2),
        ]
        def check(**kwargs):
            if kwargs["device"] in (4,5,7):raise ValueError("unsupported")
        sd.check_input_settings.side_effect=check
        return sd

    def test_real_endpoints_only_compatible_alias_and_saved_id(self):
        sd=self.fixture()
        raw={d.index:d for d in list_inputs(sd)}
        devices,count=selectable_inputs(sd)
        self.assertEqual([d.index for d in devices],[3,8])
        self.assertEqual(devices[0].name,raw[4].name)
        for alias in (1,3,4):
            self.assertEqual(resolve_input(sd,raw[alias].device_id),3)
        for rejected in (0,2,5,6,7):
            with self.assertRaises(MicrophoneError):
                resolve_input(sd,raw[rejected].device_id)
        sd.RawInputStream.assert_not_called()

    def test_two_indistinguishable_wasapi_endpoints_not_merged(self):
        sd=self.fixture()
        raw={d.index:d for d in list_inputs(sd)}
        sd.query_devices.return_value.append(dict(sd.query_devices.return_value[4]))
        devices,count=selectable_inputs(sd)
        self.assertEqual([d.index for d in devices],[8])
        self.assertEqual(count,2)
        with self.assertRaises(MicrophoneError):
            resolve_input(sd,raw[3].device_id)

    def test_no_compatible_format_no_fake_option(self):
        sd=self.fixture()
        sd.check_input_settings.side_effect=ValueError("unsupported")
        self.assertEqual(selectable_inputs(sd)[0],())
