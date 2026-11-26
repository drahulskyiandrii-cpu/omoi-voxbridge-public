"""Offline readiness checks. Never starts a GUI, device, network or model."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from importlib.metadata import version, PackageNotFoundError
import json
import os
from pathlib import Path
import sys

from .config import load_settings
from .cloud_permissions import load_permissions
from .caption_preferences import load_caption_preferences
from .runtime_paths import standalone_root


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def collect(*, config_dir: Path | None = None, environ=None, package_version=version):
    env = os.environ if environ is None else environ
    results = []

    def add(name, status, detail):
        results.append(Check(name, status, detail))

    add("windows", "ready" if os.name == "nt" else "blocked", "Windows required")
    add("python", "ready" if sys.version_info >= (3, 11) else "blocked",
        ".".join(map(str, sys.version_info[:3])))
    # QtMultimedia is in Addons; Addons pins matching Essentials/shiboken6.
    # The PySide6 meta-package is not necessary and may pull PDF/WebEngine.
    try:
        qt_parts = [str(package_version(name)) for name in
                    ("PySide6-Addons", "PySide6-Essentials", "shiboken6")]
    except PackageNotFoundError:
        add("PySide6", "blocked", "required Qt components not installed")
    except Exception:
        add("PySide6", "blocked", "Qt component metadata unavailable")
    else:
        if len(set(qt_parts)) != 1:
            add("PySide6", "blocked", "Qt component versions do not match")
        else:
            add("PySide6", "ready", qt_parts[0][:80])

    for name, required in (("sounddevice", True),
                           ("openai", True), ("cartesia", True),
                           ("numpy", False), ("soundcard", False),
                           ("websockets", False), ("comtypes", os.name == "nt"), ("mcp", False)):
        try:
            installed = package_version(name)
        except PackageNotFoundError:
            add(name, "blocked" if required else "optional_missing", "not installed")
        except Exception:
            add(name, "blocked" if required else "optional_missing", "metadata unavailable")
        else:
            # Only metadata is inspected, not the package or provider.
            add(name, "ready", str(installed)[:80])

    try:
        root = standalone_root(env)
    except ValueError:
        add("runtime profile", "blocked", "invalid absolute runtime root")
    else:
        add("runtime profile", "configured",
            "standalone; explicit assets required" if root is not None else
            "legacy compatibility; standalone root not configured")

    for label, key in (("OpenAI credential", "OPENAI_API_KEY"),
                       ("Groq credential", "GROQ_API_KEY"),
                       ("Cartesia credential", "CARTESIA_API_KEY"),
                       ("Anthropic credential", "ANTHROPIC_API_KEY")):
        configured = bool(env.get(key))
        add(label, "configured" if configured else "not_configured",
            "presence only; access not tested")

    def read(name, filename, loader):
        try:
            value = loader(None if config_dir is None else config_dir / filename)
        except (OSError, ValueError, TypeError):
            add(name, "blocked", "invalid or unreadable; not overwritten")
            return None
        add(name, "ready", "valid or default; no writes")
        return value

    settings = read("settings", "settings.json", load_settings)
    permissions = read("cloud_permissions", "cloud_permissions.json", load_permissions)
    read("caption_preferences", "captions.json", load_caption_preferences)
    if settings is not None:
        shared_missing = settings.stt_backend == "shared" and len(env.get("OMOI_SPEECH_CORE_STT_TOKEN", "")) < 32
        groq_missing = settings.stt_backend == "groq_cloud" and not env.get("GROQ_API_KEY")
        add("STT mode", "blocked" if shared_missing or groq_missing else "configured",
            "shared token missing" if shared_missing else
            "Groq credential missing" if groq_missing else
            settings.stt_backend + "; manual dictation only; inference untested")
        add("Microphone", "configured", "explicit device" if settings.microphone_id else "system default; availability not tested")
        add("Cartesia voice", "configured" if settings.cartesia_voice_id else "not_configured",
            "presence only; ownership and language quality not tested")
    if permissions is not None:
        add("standing permissions", "ready", f"{sum(asdict(permissions).values())} of {len(asdict(permissions))} enabled")
    add("native audio/hotkeys", "untested", "requires interactive acceptance")
    add("cloud model access", "untested", "requires authorized live requests")
    add("local models", "untested", "no inference, GPU probe or download")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = collect()
    if args.json:
        print(json.dumps({"mode":"offline", "checks":[asdict(x) for x in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{check.status:17} {check.name:24} {check.detail}")
    return 2 if any(c.status == "blocked" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
