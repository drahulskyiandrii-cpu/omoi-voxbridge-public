"""Offline Windows regression runner, including optional SDK contracts.

Run from any directory. Optional --optional-target points to an isolated
pip --target directory inside this project's virtual environment.
Never starts a model, capture device, or billable API.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import site
import sys
import unittest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--optional-target", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "src"))
    if args.optional_target is not None:
        target = args.optional_target.resolve()
        if not target.is_dir():
            parser.error("optional target directory missing")
        site.addsitedir(str(target))
        sys.path.insert(0, str(target))
    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.defaultTestLoader.discover(str(repo / "tests"))
    )
    print("OFFLINE_REGRESSION", result.testsRun, "skipped", len(result.skipped),
          "passed" if result.wasSuccessful() else "failed")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
