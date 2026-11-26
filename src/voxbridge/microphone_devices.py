"""Microphone metadata and explicit input-device resolution (no capture).

PortAudio indices are session-local. Persist a fingerprint of exact device
name + host API, never an index; ambiguous or missing choices fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


class MicrophoneError(RuntimeError):
    pass


def valid_microphone_id(value):
    return isinstance(value, str) and (value == "" or re.fullmatch(r"[0-9a-f]{64}", value) is not None)


@dataclass(frozen=True)
class InputDevice:
    device_id: str
    index: int
    name: str
    host_api: str
    aliases: tuple[str, ...] = ()

    @property
    def label(self):
        return self.name

    def matches(self, device_id):
        return device_id == self.device_id or device_id in self.aliases


def list_inputs(sd=None):
    if sd is None:
        import sounddevice as sd
    try:
        devices = sd.query_devices()
        hosts = sd.query_hostapis()
        if len(devices) > 1024 or len(hosts) > 128:
            raise ValueError()
        result = []
        for index, device in enumerate(devices):
            if int(device["max_input_channels"]) <= 0:
                continue
            name = device["name"]
            host_index = device["hostapi"]
            if type(host_index) is not int or not 0 <= host_index < len(hosts):
                raise ValueError()
            host = hosts[host_index]["name"]
            if not all(isinstance(s, str) and 0 < len(s) <= 1024 and "\x00" not in s for s in (name, host)):
                raise ValueError()
            identity = hashlib.sha256(json.dumps([name, host], ensure_ascii=True).encode("ascii")).hexdigest()
            result.append(InputDevice(identity, index, name, host))
    except Exception:
        raise MicrophoneError("MIC_DEVICE_DISCOVERY_FAILED") from None
    return result


def _real_name(name):
    """Exclude routing aliases, loopback and playback pseudo-inputs."""
    value = name.casefold()
    return not any(token in value for token in (
        "soundmapper", "primary sound capture", "primärer soundaufnahmetreiber",
        "stereo mix", "stereomix", "loopback", "what u hear",
        "lautsprecher", "speakers", "wave out mix",
    ))


def _compatible(sd, device):
    try:
        sd.check_input_settings(device=device.index, samplerate=16000,
                                channels=1, dtype="int16")
        return True
    except Exception:
        return False


def selectable_inputs(sd=None):
    """One active Windows capture endpoint, not one entry per driver API.

    WASAPI names are the full endpoint names. Use a compatible equivalent
    DirectSound/MME input when WASAPI cannot accept 16 kHz mono. Never merge
    ambiguous same-name endpoints or infer identities from loose substrings.
    """
    if sd is None:
        import sounddevice as sd
    devices = list_inputs(sd)
    wasapi = [d for d in devices if d.host_api == "Windows WASAPI" and _real_name(d.name)]
    hosts = sd.query_hostapis()
    windows = any(h["name"] == "Windows WASAPI" for h in hosts)
    if not windows:
        anchors = [d for d in devices if _real_name(d.name) and d.host_api != "Windows WDM-KS"]
        counts = {}
        for device in anchors:
            counts[device.device_id] = counts.get(device.device_id, 0) + 1
        return tuple(d for d in anchors if counts[d.device_id] == 1 and _compatible(sd, d)), sum(n for n in counts.values() if n > 1)

    result, ambiguous = [], 0
    names = [d.name.casefold() for d in wasapi]
    for anchor in wasapi:
        name = anchor.name.casefold()
        if names.count(name) != 1:
            ambiguous += 1
            continue
        group = [anchor]
        for device in devices:
            if device.host_api not in ("Windows DirectSound", "MME") or not _real_name(device.name):
                continue
            candidate = device.name.casefold()
            exact = candidate == name
            truncated = (device.host_api == "MME" and len(device.name) == 31
                         and name.startswith(candidate)
                         and sum(n.startswith(candidate) for n in names) == 1)
            if exact or truncated:
                group.append(device)
        # Duplicate names in one API are not a reliable identity.
        counts = {d.host_api: sum(e.host_api == d.host_api for e in group) for d in group}
        unique = [d for d in group if counts[d.host_api] == 1]
        ambiguous += len(group) - len(unique)
        compatible = [d for d in unique if _compatible(sd, d)]
        if not compatible:
            continue
        order = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2}
        chosen = min(compatible, key=lambda d: order[d.host_api])
        # Use the proven physical endpoint name, even if MME truncated it.
        result.append(InputDevice(chosen.device_id, chosen.index, anchor.name,
                                  chosen.host_api, tuple(d.device_id for d in unique
                                                        if d.device_id != chosen.device_id)))
    return tuple(result), ambiguous


def resolve_input(sd, device_id):
    if not valid_microphone_id(device_id):
        raise MicrophoneError("MIC_DEVICE_INVALID")
    if not device_id:
        return None
    raw = list_inputs(sd)
    exact = [d for d in raw if d.device_id == device_id]
    if not exact:
        raise MicrophoneError("MIC_DEVICE_UNAVAILABLE")
    if len(exact) != 1:
        raise MicrophoneError("MIC_DEVICE_AMBIGUOUS")
    devices, _ = selectable_inputs(sd)
    matches = [d for d in devices if d.matches(device_id)]
    if len(matches) == 1:
        return matches[0].index
    if len(matches) > 1:
        raise MicrophoneError("MIC_DEVICE_AMBIGUOUS")
    raise MicrophoneError("MIC_DEVICE_UNAVAILABLE")
