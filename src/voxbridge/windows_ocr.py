"""Offline Windows OCR for selected regions, with no disk image or cloud request."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import time

from .analysis_providers import ProviderFailure
from .vision import png_dimensions

LANGUAGE_TAGS = {"de": "de-DE", "ru": "ru", "en": "en-US"}


def recognize_png(png, language, cancel=None):
    png_dimensions(png)
    if os.name != "nt" or language not in LANGUAGE_TAGS:
        raise ProviderFailure("OCR_LANGUAGE_OR_PLATFORM_UNAVAILABLE")
    if cancel is not None and cancel.is_set():
        raise ProviderFailure("OCR_CANCELLED")
    root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    exe = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = Path(__file__).with_name("windows_ocr.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    payload = json.dumps({"language": LANGUAGE_TAGS[language],
                          "image": base64.b64encode(png).decode("ascii")}).encode("utf-8")
    allow = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP",
             "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    child = None
    try:
        child = subprocess.Popen([str(exe), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 15
        first = True
        while True:
            if cancel is not None and cancel.is_set():
                raise ProviderFailure("OCR_CANCELLED")
            if time.monotonic() >= deadline:
                raise ProviderFailure("OCR_TIMEOUT")
            try:
                raw, _ = child.communicate(input=payload if first else None, timeout=.15)
                break
            except subprocess.TimeoutExpired:
                first = False
        if len(raw) > 256000:
            raise ProviderFailure("OCR_RESPONSE_LIMIT")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ProviderFailure("OCR_WINDOWS_FAILED")
        code = result.get("error")
        if code in {"OCR_LANGUAGE_INVALID", "OCR_LANGUAGE_NOT_INSTALLED", "OCR_IMAGE_LIMIT",
                    "OCR_IMAGE_DIMENSIONS", "OCR_WINDOWS_FAILED"}:
            raise ProviderFailure(code)
        text = result.get("text")
        if child.returncode != 0 or not isinstance(text, str) or not text.strip() or len(text) > 24000:
            raise ProviderFailure("OCR_NO_TEXT")
        return text.strip()
    except ProviderFailure:
        raise
    except Exception:
        raise ProviderFailure("OCR_WINDOWS_FAILED") from None
    finally:
        if child is not None and child.poll() is None:
            try:
                child.kill()
                child.communicate(timeout=2)
            except Exception:
                raise ProviderFailure("PROVIDER_CLEANUP_FAILED") from None
