"""Explicit standalone runtime root; no discovery, writes or model loading."""
from __future__ import annotations

import os
from pathlib import Path


def standalone_root(environ=None):
    """None retains the legacy development profile for existing installations.

    An absolute configured root opts into independent runtime defaults.
    Invalid configuration never silently selects a sibling project.
    """
    env = os.environ if environ is None else environ
    value = env.get("VOXBRIDGE_RUNTIME_ROOT")
    if value is None:
        return None
    try:
        path = Path(value)
        if not value.strip() or not path.is_absolute():
            raise ValueError
        return path.resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("RUNTIME_ROOT_INVALID") from None
