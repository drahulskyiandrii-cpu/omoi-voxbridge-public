"""Read-only NVIDIA VRAM guard; never kills or modifies another model service."""
from __future__ import annotations
import csv
import io
import subprocess
from dataclasses import dataclass

class GpuBusy(RuntimeError):
    pass

@dataclass(frozen=True)
class GpuSnapshot:
    total_mib: int
    used_mib: int
    utilization_pct: int

    @property
    def free_mib(self) -> int:
        return max(0, self.total_mib - self.used_mib)

def parse_snapshot(output: str) -> GpuSnapshot:
    data = list(csv.reader(io.StringIO(output.strip())))
    # CUDA uses one selected device; without an explicit mapping a multi-GPU
    # inventory cannot safely tell us which device will receive the model.
    if len(data) != 1 or len(data[0]) != 3:
        raise GpuBusy("GPU_STATUS_UNAVAILABLE")
    try:
        total, used, utilization = (int(s.strip().split()[0]) for s in data[0][:3])
    except (ValueError, IndexError):
        raise GpuBusy("GPU_STATUS_UNAVAILABLE") from None
    if total < 1 or not 0 <= used <= total or not 0 <= utilization <= 100:
        raise GpuBusy("GPU_STATUS_UNAVAILABLE")
    return GpuSnapshot(total, used, utilization)

def snapshot() -> GpuSnapshot:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits"], check=True,
                            capture_output=True, text=True, timeout=3,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise GpuBusy("GPU_STATUS_UNAVAILABLE") from None
    return parse_snapshot(r.stdout)

def require_free_memory(min_free_mib: int = 8000, max_util_pct: int = 45) -> GpuSnapshot:
    now = snapshot()
    if now.free_mib < min_free_mib or now.utilization_pct > max_util_pct:
        raise GpuBusy("GPU_BUSY_SKIP_MODEL_LOAD")
    return now
