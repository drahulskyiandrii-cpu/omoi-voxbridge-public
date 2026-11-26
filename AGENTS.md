# VoxBridge source review rules

Private development snapshot; do not publish. Use Windows 11, Python 3.11+
and PySide6. Work in this project only. Read README.md. Keep UI operations
nonblocking, cancellation bounded and settings validated. Preserve explicit
cloud consent, transient input, model ownership and fail-closed errors.
Never download models, start paid APIs or load GPU models in offline tests.
Run scripts/run_offline_tests.py and offscreen smokes after edits.
Do not claim native acceptance from mocks. Independent Qwen provisioning,
license review and real device acceptance are release blockers.
