"""One-shot metadata discovery in a fresh process.

SoundCard initializes COM at module import on the importing thread. Reusing
it from subsequent short-lived discovery threads can fail with COM not
initialized. A fresh owned process avoids that cross-thread dependency.
"""
import json
import multiprocessing as mp
import time

from .wasapi_capture import CaptureFailure, LoopbackDevice, list_loopback_devices


def _discover_child(sender):
    try:
        devices = list_loopback_devices()
        data = {"devices": [[d.id, d.name, d.is_default] for d in devices]}
    except Exception:
        data = {"error": "WASAPI_DEVICE_ENUMERATION_FAILED"}
    try:
        sender.send_bytes(json.dumps(data, ensure_ascii=True).encode("ascii"))
    except OSError:
        pass
    finally:
        sender.close()


def discover_loopback_devices(timeout=8.0, *, _worker=_discover_child):
    if not 0.1 <= timeout <= 15:
        raise CaptureFailure("WASAPI_DISCOVERY_TIMEOUT")
    ctx = mp.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_worker, args=(writer,), daemon=True, name="voxbridge-device-discovery")
    started = False
    try:
        child.start()
        started = True
        writer.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not reader.poll(.05):
                if not child.is_alive():
                    raise CaptureFailure("WASAPI_DISCOVERY_CHILD_EXITED")
                continue
            data = json.loads(reader.recv_bytes(200000))
            if not isinstance(data, dict) or set(data) != {"devices"}:
                raise CaptureFailure("WASAPI_DEVICE_ENUMERATION_FAILED")
            rows = data["devices"]
            if not isinstance(rows, list) or len(rows) > 64:
                raise CaptureFailure("WASAPI_INVALID_DEVICE_LIST")
            result = []
            for row in rows:
                if (not isinstance(row,list) or len(row)!=3 or not isinstance(row[0],str)
                        or not 1 <= len(row[0]) <= 512 or not isinstance(row[1],str)
                        or not 1 <= len(row[1]) <= 160 or type(row[2]) is not bool):
                    raise CaptureFailure("WASAPI_INVALID_DEVICE_LIST")
                result.append(LoopbackDevice(*row))
            return result
        raise CaptureFailure("WASAPI_DISCOVERY_TIMEOUT")
    except CaptureFailure:
        raise
    except Exception:
        raise CaptureFailure("WASAPI_DEVICE_ENUMERATION_FAILED") from None
    finally:
        reader.close()
        writer.close()
        if started:
            child.join(.2)
            if child.is_alive():
                child.terminate()
                child.join(1)
            if child.is_alive():
                child.kill()
                child.join(1)
            if not child.is_alive():
                child.close()
