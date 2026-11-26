"""Show the real caption overlay with fixed synthetic content, without I/O."""
from __future__ import annotations

import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="Render offscreen, verify both lanes and exit.")
    args = parser.parse_args(argv)
    if args.self_test:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication
    from voxbridge.captions_core import CaptionCoordinator
    from voxbridge.captions_overlay import CaptionsOverlay, populate_synthetic_demo

    app = QApplication([])
    coordinator = CaptionCoordinator()
    populate_synthetic_demo(coordinator)
    overlay = CaptionsOverlay(coordinator)
    overlay.title_label.setText("DEMO | Beispieldaten - kein Live-Audio")
    overlay.closed.connect(app.quit)
    try:
        overlay.show()
        if args.self_test:
            app.processEvents()
            snapshot = coordinator.snapshot()
            if (len(snapshot.lines) != 2
                    or not all(line.status == "complete" for line in snapshot.lines)
                    or not all(line.original in overlay.de_label.text()
                               and line.translation in overlay.ru_label.text()
                               for line in snapshot.lines)):
                raise RuntimeError("DEMO_RENDER_FAILED")
            overlay.close()
            if coordinator.snapshot().active:
                raise RuntimeError("DEMO_CLEANUP_FAILED")
            print("OFFLINE_DEMO_PASS")
            return 0
        return app.exec()
    finally:
        overlay.close()
        coordinator.stop()


if __name__ == "__main__":
    raise SystemExit(main())
