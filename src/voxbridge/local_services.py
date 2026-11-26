"""Read-only discovery of explicitly trusted loopback model services.

A borrowed process is never owned, restarted, or terminated by VoxBridge.
Identity includes executable and process creation time; verify again on the
connected socket before any private request. Legacy services are unencrypted
and unauthenticated: only explicitly enabled, known local installations.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time

from .loopback_http import loopback_response
from .windows_network import listening_endpoints, listener_owners, process_identity, process_arguments


class LocalServiceError(RuntimeError):
    pass


def normalized(path):
    return os.path.normcase(os.path.realpath(path))


@dataclass(frozen=True)
class LocalPeer:
    port: int
    pid: int
    identity: tuple

    def verify(self):
        if (listener_owners(self.port) != (("127.0.0.1", self.pid),)
                or process_identity(self.pid) != self.identity):
            raise LocalServiceError("LOCAL_SERVICE_IDENTITY_CHANGED")

    def get_json(self, path, cancel, *, timeout=2, token=None):
        deadline = time.monotonic() + timeout
        def check():
            if cancel.is_set():
                raise LocalServiceError("LOCAL_SERVICE_CANCELLED")
            if time.monotonic() >= deadline:
                raise LocalServiceError("LOCAL_SERVICE_TIMEOUT")
        headers = {"Connection": "close", "Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with loopback_response(self.port, "GET", path, headers=headers, check=check,
                                   verify_connected=self.verify, connect_timeout=min(timeout, 2)) as response:
                if response.status != 200:
                    raise LocalServiceError("LOCAL_SERVICE_NOT_READY")
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise LocalServiceError("LOCAL_SERVICE_RESPONSE_LIMIT")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise LocalServiceError("LOCAL_SERVICE_RESPONSE_INVALID")
                self.verify()
                return value
        except LocalServiceError:
            raise
        except Exception:
            raise LocalServiceError("LOCAL_SERVICE_UNAVAILABLE") from None


def trusted_peers(executables):
    """Scan local listeners, never send a request to unidentified executables."""
    allowed = {normalized(p) for p in executables if p.is_file()}
    if not allowed or os.name != "nt":
        return ()
    endpoints = listening_endpoints()
    result = []
    identities = {}
    for address, port, pid in endpoints:
        if address != "127.0.0.1" or sum(p == port for _, p, _ in endpoints) != 1:
            continue
        if pid not in identities:
            identities[pid] = process_identity(pid)
        identity = identities[pid]
        if identity is not None and identity[0] in allowed:
            result.append(LocalPeer(port, pid, identity))
    return tuple(result)


def mini_qwen_peers(python, worker, approved_hashes):
    """Legacy Mini starts a venv interpreter which can redirect to base Python."""
    if not python.is_file() or not worker.is_file():
        return ()
    try:
        if hashlib.sha256(worker.read_bytes()).hexdigest() not in approved_hashes:
            return ()
        candidates = [python]
        config = python.parent.parent / "pyvenv.cfg"
        for line in config.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "home":
                candidates.append(Path(value.strip()) / "python.exe")
                break
        result = []
        for peer in trusted_peers(candidates):
            args = process_arguments(peer.pid)
            # Require the exact reviewed script as an argument, not an arbitrary
            # Python process or an argument containing the script name.
            if len(args) >= 2 and normalized(args[1]) == normalized(worker):
                peer.verify()
                result.append(peer)
        return tuple(result)
    except (OSError, ValueError, LocalServiceError):
        return ()
