# Third-party dependencies

The repository contains application source. Third-party Python packages
are installed separately; model weights are not included. The following direct dependency metadata was observed in
the development environment, not a complete legal or transitive audit.

| Distribution | Tested version | Installed package license metadata |
| --- | --- | --- |
| PySide6-Addons, PySide6-Essentials, shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| sounddevice | 0.5.6 | MIT |
| comtypes | 1.5.0 | MIT |
| openai | 2.54.0 | Apache-2.0 |
| cartesia | 4.2.0 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| soundcard | 0.4.6 | BSD 3-clause |
| websockets | 17.4 | BSD-3-Clause |
| mcp | 2.3.0 | MIT |

Version ranges are in pyproject.toml. Qt distribution obligations and
transitive notices must be reviewed before shipping a bundled installer.
OpenVINO, Faster-Whisper, llama.cpp, Qwen, Gemma and their specific weights,
adapters and runtimes have independent terms. No private voice recordings,
third-party models or external worker source are included in the source repository.
Distribution requires the applicable rights and license notices. See LICENSE_STATUS.md.
