"""Gated Windows launcher for STT; never starts the target before job membership."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    # -I removes the script directory from sys.path; import only our own helper.
    sys.path.insert(0, str(Path(__file__).parent))
    from windows_job_membership import in_named_job
    if (len(sys.argv) < 2 or sys.stdin.readline() != "GO\n"
            or not in_named_job(os.environ.get("VOXBRIDGE_STT_JOB", ""))):
        return 2
    # stdin/stdout are inherited. Parent sends no request until worker says ready.
    return subprocess.call(sys.argv[1:], shell=False)


if __name__ == "__main__":
    raise SystemExit(main())
