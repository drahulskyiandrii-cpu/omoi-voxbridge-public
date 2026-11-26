"""VoxBridge-owned CUDA recognizer. Reuses installed model/runtime, not Mini source."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import time
import wave


def emit(packet):
    print(json.dumps(packet, ensure_ascii=True), flush=True)


def startup():
    started = time.perf_counter()
    overlay_value = os.environ.get("OMOI_MINI_CUDA_STT_OVERLAY")
    model_value = os.environ.get("OMOI_MINI_CUDA_STT_MODEL")
    if not overlay_value or not model_value:
        raise RuntimeError("CUDA_STT_CONFIG_MISSING")
    overlay, model_path = Path(overlay_value), Path(model_value)
    if not overlay.is_absolute() or not model_path.is_absolute():
        raise RuntimeError("CUDA_STT_CONFIG_INVALID")
    if not overlay.is_dir() or not (model_path / "model.bin").is_file():
        raise RuntimeError("CUDA_STT_MODEL_MISSING")
    sys.path.insert(0, str(overlay))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import numpy as np
    import torch
    from faster_whisper import WhisperModel
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA_UNAVAILABLE")
    model = WhisperModel(str(model_path), device="cuda", device_index=0,
                         compute_type="float16", num_workers=1, cpu_threads=4,
                         local_files_only=True)
    if not model.model.is_multilingual:
        raise RuntimeError("MULTILINGUAL_WHISPER_REQUIRED")
    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32),
        language="de", beam_size=1, temperature=0,
        condition_on_previous_text=False, vad_filter=False)
    list(segments)
    return model, round((time.perf_counter() - started)*1000)


def samples_from_wav(encoded):
    import numpy as np
    raw = base64.b64decode(encoded, validate=True)
    if not 44 <= len(raw) <= 1048576:
        raise ValueError("length")
    with wave.open(io.BytesIO(raw), "rb") as reader:
        frames = reader.getnframes()
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(),
                reader.getcomptype()) != (16000, 1, 2, "NONE") or not 1600 <= frames <= 480000:
            raise ValueError("format")
        data = reader.readframes(frames)
        if len(data) != frames*2:
            raise ValueError("length")
    return np.frombuffer(data, dtype="<i2").astype(np.float32)/32768.0


def decode(model, line):
    rid = None
    try:
        request = json.loads(line)
        rid = request.get("id")
        if type(rid) is not int or rid < 1 or request.get("language") not in ("de", "ru"):
            raise ValueError("request")
        audio = samples_from_wav(request["audio"])
        options = request.get("options", {})
        if not isinstance(options, dict) or set(options) - {"beam_size", "hotwords", "vad_filter"}:
            raise ValueError("options")
        beam = options.get("beam_size", 5)
        terms = options.get("hotwords", "")
        vad = options.get("vad_filter", False)
        if (type(beam) is not int or beam not in (1, 5)
                or not isinstance(terms, str) or len(terms.encode("utf-8")) > 192
                or type(vad) is not bool):
            raise ValueError("options")
        started = time.perf_counter()
        segments, info = model.transcribe(audio, language=request["language"], task="transcribe",
            beam_size=beam, temperature=0, condition_on_previous_text=False, vad_filter=vad,
            **({"hotwords": terms} if terms else {}))
        text = " ".join(segment.text for segment in segments).strip()
        if getattr(info, "language", None) != request["language"] or len(text) > 3000 or "\x00" in text:
            raise ValueError("response")
        return {"id": rid, "ok": True, "text": text, "wallMs": round((time.perf_counter()-started)*1000)}
    except Exception:
        return {"id": rid, "ok": False, "error": "LOCAL_ASR_FAILED"}


def main():
    if sys.argv[1:]:
        return 2
    try:
        model, load_ms = startup()
    except Exception as exc:
        code = str(exc) if str(exc) in ("CUDA_UNAVAILABLE", "CUDA_STT_MODEL_MISSING",
                     "MULTILINGUAL_WHISPER_REQUIRED") else "CUDA_STT_RUNTIME_NOT_READY"
        emit({"event": "fatal", "error": code})
        return 2
    emit({"event": "ready", "loadMs": load_ms})
    while True:
        line = sys.stdin.readline(1500001)
        if not line:
            break
        if len(line) > 1500000 or not line.endswith("\n"):
            return 2
        emit(decode(model, line))
        del line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
