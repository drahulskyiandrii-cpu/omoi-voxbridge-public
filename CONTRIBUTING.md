# Contributing to OMOI VoxBridge

Windows 11, Python 3.11 and PySide6. Keep modules focused, readable and
independently testable. Document public interfaces in README.md.

- Preserve explicit provider-specific consent, transient input, bounded
  cancellation and validated configuration.
- Never perform blocking inference on the GUI thread.
- Offline tests must not load models, access physical capture devices
  or invoke billable APIs.
- Run offline regression and offscreen smoke tests after changes.
- Distinguish automated tests from native device and application acceptance.
- Review third-party licenses and local runtime requirements before release.
