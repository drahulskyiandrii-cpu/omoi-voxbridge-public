"""Atomic session state and generation IDs; stale inference results are ignored."""
from __future__ import annotations
import threading
from dataclasses import dataclass
from enum import Enum

class State(str, Enum):
    IDLE = "idle"
    VERIFYING_TARGET = "verifying_target"
    STARTING_MIC = "starting_mic"
    RECORDING = "recording"
    STOPPING_MIC = "stopping_mic"
    AWAITING_APPROVAL = "awaiting_approval"
    RECOGNIZING = "recognizing"
    INSERTING = "inserting"

@dataclass(frozen=True)
class Snapshot:
    generation: int
    state: State
    target_hwnd: int
    focus_hwnd: int = 0

class Session:
    def __init__(self):
        self._lock = threading.RLock()
        self._generation = 0
        self._state = State.IDLE
        self._target_hwnd = 0
        self._focus_hwnd = 0

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._generation, self._state, self._target_hwnd, self._focus_hwnd)

    def begin(self, hwnd: int, focus_hwnd: int = 0) -> int:
        with self._lock:
            if self._state is not State.IDLE or hwnd <= 0:
                raise ValueError("SESSION_NOT_IDLE_OR_NO_TARGET")
            self._generation += 1
            self._state = State.RECORDING
            self._target_hwnd = hwnd
            self._focus_hwnd = focus_hwnd
            return self._generation

    def advance(self, generation: int, old: State, new: State) -> bool:
        with self._lock:
            if generation != self._generation or self._state is not old:
                return False
            self._state = new
            return True

    def finish(self, generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._state = State.IDLE
            self._target_hwnd = 0
            self._focus_hwnd = 0
            return True

    def cancel(self) -> int:
        with self._lock:
            self._generation += 1
            self._state = State.IDLE
            self._target_hwnd = 0
            self._focus_hwnd = 0
            return self._generation
