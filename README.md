# OMOI VoxBridge - private source review snapshot

**Development snapshot, not a public release.** Local Qwen and legacy STT
asset provisioning and real Windows/GPU acceptance are not yet complete.
No model weights, private voice files, API credentials or development history
are included. Do not redistribute until the license and acceptance gates pass.

## Zweck / Funktion

VoxBridge ist eine Windows-Anwendung für Spracheingabe, Sprachausgabe,
Live-Untertitel und KI-gestützte Text- oder Bildanalyse. Python und PySide6
stellen die Oberfläche und Steuerung bereit. Die Anwendung trennt
Benutzereingaben, Provider, Windows-Geräte und Hintergrundprozesse.

- STT: Spracherkennung für Diktat, lokale oder ausdrücklich genehmigte Cloud.
- TTS: ausgewählten oder kopierten Text vorlesen; Sprache automatisch wählen.
- OCR: Text aus einer ausgewählten Bildschirmregion erkennen.
- LLM: Text oder Bild mit einem konfigurierten Modell analysieren.
- MCP: ausdrücklich übergebene Texte für externe Agenten vorbereiten.

Lokale Übersetzungskandidaten sind noch nicht für Live-Untertitel freigegeben.
Cloud-Aufrufe benötigen Freigabe; optionale lokale Vorladung ist abschaltbar.

## Installieren und starten (Windows 11, Python 3.11)

```powershell
.\scripts\install-dev.ps1 -Plan -IncludeMcp
.\scripts\install-dev.ps1 -IncludeMcp
.\.venv\Scripts\voxbridge-standalone.exe
```

Installation benötigt Python-Paketabhängigkeiten. Sie lädt keine Modellgewichte
und startet keine Modelle. Für lokale Modelle sind separat qualifizierte Assets
nötig. Der Qwen-Worker ist noch nicht unabhängig bereitstellbar. Verwenden Sie
keine fremden Stimmen oder Gewichte ohne deren Nutzungsrechte.

Qt wird mit passenden Addons, Essentials und shiboken6 installiert.
Qt PDF und WebEngine sind für VoxBridge nicht erforderlich.

## Unabhaengige Installation aus dem Wheel

Voraussetzung: 64-Bit Python 3.11 auf Windows. Das gepruefte Wheel und
dessen SHA256-Pruefsumme separat bereitstellen. Keine Administratorrechte,
kein Autostart, kein Zugriff auf vorhandene Entwicklungsumgebungen.
Standardziel: LocalAppData/Programs/OMOI-VoxBridge. Das Ziel darf noch
nicht existieren; vorhandene Installationen werden nie ueberschrieben.

    py -3.11 -I scripts/install_standalone.py --wheel C:/Downloads/omoi_voxbridge-0.1.0-py3-none-any.whl --sha256 SHA256 --plan

Nach Kontrolle denselben Befehl ohne --plan ausfuehren. Optional
--wheelhouse C:/Downloads/wheelhouse fuer eine vollstaendige lokale
Wheelsammlung (keine Netzwerkabhaengigkeiten waehrend der Installation).
Ohne wheelhouse werden Python-Abhaengigkeiten von PyPI bezogen.
Start-VoxBridge.cmd und Doctor.cmd liegen im neuen Installationsordner.
Die beiden Startskripte verwenden ein separates Profil unter
LocalAppData/OMOI-VoxBridge-Standalone. Die Entwicklungsprofile und
bestehenden Berechtigungen werden nicht uebernommen oder veraendert. Zum Entfernen erst
VoxBridge normal beenden und dann nur den eigenen Installationsordner
entfernen. Benutzerprofile und Modellordner nicht automatisch loeschen.
Dies ist ein Python-basierter Installer, kein selbstenthaltendes EXE/MSI.

Windows Sandbox: scripts/sandbox_acceptance.ps1 ist ein expliziter
Test fuer eine frische VM, keine normale Installation. Die VM soll
keinen Netzwerk-, GPU-, Mikrofon- oder Clipboard-Zugriff haben.
Nur ein eigens vorbereiteter schreibgeschuetzter Eingabeordner und
ein separater Ergebnisordner werden eingebunden. Keine privaten
Modelle, Anwendungsprofile oder Zugangsdaten freigeben.
scripts/prepare_sandbox.py --help beschreibt die Vorbereitung aus einem
lokalen, bereits geprueften Python-Installer und einem Wheelhouse.
Die historische Sandbox-Basis Python 3.11.9 ist nur ein Teststand,
keine Empfehlung zur Installation einer veralteten Python-Version.

## Lokale Laufzeit (separat vorbereitete Komponenten)

Der installierte Startbefehl voxbridge-standalone verwendet standardmäßig
einen eigenen Laufzeitordner unter dem VoxBridge-Profil. Mit
VOXBRIDGE_RUNTIME_ROOT kann ein anderer absoluter Ordner gewählt werden.
Der bisherige Startbefehl bleibt nur für bestehende Entwicklungsumgebungen.
Im unabhängigen Modus gibt es keinen automatischen Rückgriff auf Mini-Assets.

Alle Pfade sind Beispiele. Modelle und externe Laufzeiten werden nicht
mitgeliefert. Variablen in PowerShell vor dem Start setzen; nur die für
das gewählte Backend erforderlichen Werte angeben.

```powershell
$env:VOXBRIDGE_RUNTIME_ROOT = 'D:\SpeechRuntime'

# CPU STT: Node on PATH, SDK includes node_modules/openvino-genai-node
$env:VOXBRIDGE_OPENVINO_SDK_DIR = 'D:\SpeechRuntime\openvino-sdk'
$env:VOXBRIDGE_OPENVINO_MODEL_DIR = 'D:\SpeechRuntime\whisper-medium-int8-ov'

# CUDA STT: qualified separate Python, overlay and CT2 model.bin
$env:VOXBRIDGE_CUDA_PYTHON = 'D:\SpeechRuntime\cuda-venv\Scripts\python.exe'
$env:VOXBRIDGE_CUDA_MODEL_DIR = 'D:\SpeechRuntime\faster-whisper-small-ct2'
$env:VOXBRIDGE_CUDA_OVERLAY = 'D:\SpeechRuntime\faster-whisper-overlay'

# Local LLM: installed llama-server.exe, GGUF model directories
$env:VOXBRIDGE_LLAMA_SERVER = 'D:\SpeechRuntime\llama-server.exe'
$env:VOXBRIDGE_MODEL_DIRS = 'D:\SpeechRuntime\gguf'

# Local TTS: previously qualified worker, authorized voice and CF50 adapter
$env:VOXBRIDGE_QWEN_PYTHON = 'D:\SpeechRuntime\qwen-venv\Scripts\python.exe'
$env:VOXBRIDGE_QWEN_WORKER = 'D:\SpeechRuntime\qwen_server.py'
$env:VOXBRIDGE_QWEN_REFERENCE = 'D:\SpeechRuntime\qwen-profile\OMOI-Mini\tts-eval\private-voice-reference\voice.wav'
$env:VOXBRIDGE_QWEN_CF50_ADAPTER = 'D:\SpeechRuntime\adapter.safetensors'
```

Der interne Ordnername OMOI-Mini im separaten qwen-profile ist nur
eine Kompatibilitätsschnittstelle des geprüften externen Workers.
Es werden keine Dateien aus der vorhandenen Mini-Installation kopiert.
Die Qwen-Dateien müssen bereits geprüft sein; unbekannte Worker und
abweichende CF50-Adapter werden abgelehnt. Eine frei verteilbare, autonome
Qwen-Installation ist noch nicht freigegeben. Leere oder relative explizite
STT-/TTS-Pfade sind ungültig. Die CPU- und CUDA-Worker sind im Quellcode
enthalten. Für Bildanalyse mit Gemma ist ein passender Projektor erforderlich.
Windows OCR verwendet installierte Windows-Sprachpakete. Lokale Dienste
werden nur nach ausdrücklicher Konfiguration wiederverwendet; fremde Prozesse
werden nicht beendet.

## Installation aus einem Python-Wheel (privater Test)

Das separat gebaute Wheel enthält nur das Python-Anwendungsprogramm.
In einem neuen, eigenen Ordner installieren (Beispiel: PowerShell):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install 'D:\Download\omoi_voxbridge-0.1.0-py3-none-any.whl[cloud,captions,mcp]'
.\.venv\Scripts\voxbridge-doctor.exe
.\.venv\Scripts\voxbridge-standalone.exe
```

Die Installation startet nichts automatisch. Zum Entfernen zuerst VoxBridge
über das Tray-Menü beenden und danach nur den selbst erstellten
Installationsordner entfernen. Einstellungen und lokale Modell-Assets
bleiben separat erhalten. Keine bestehenden Python-Umgebungen löschen.
Für ein Update ein neues Wheel in einer neuen Umgebung prüfen; die
bisherige Installation bis zur Abnahme behalten.

## Struktur

- `src/voxbridge/`: Anwendung, Qt-Dialoge, Einstellungen, Provider, Windows-Adapter.
- `tests/`: ausgewählte Produktionsregressionen mit synthetischen Daten.
- `scripts/`: Installation, Start, Offline-Tests, Wheel-Prüfung.
- `SOURCE_MANIFEST.json`: Commit und SHA-256 für alle exportierten Dateien.

`app.py` koordiniert die Funktionen; `session.py` verwaltet Zustände;
`stt.py` erkennt Sprache; `tts_ui.py` und `tts_language.py` koordinieren
Sprachausgabe und Sprachwahl. `output_isolation.py` kapselt Audioausgabe.
`config.py` prüft Einstellungen; `windows_owned_job.py` verwaltet eigene
Windows-Prozessgruppen. Externe Prozesse werden nicht beendet.

## Architektur und Projektvorstellung

Das Projekt demonstriert Windows-Systemintegration mit klar getrennten
Schnittstellen. Der Controller koordiniert Hotkeys, Tray und Sitzungen.
Die Provider implementieren die Inferenz. Eigene Worker werden kontrolliert
beendet. Ein Fehler darf weder zu einer unerwarteten Eingabe in ein anderes
Fenster noch zu einer unangekuendigten Cloud-Anfrage fuehren.

    Windows hotkey / tray
      -> Application controller and session state
        -> Audio input -> STT -> target validation -> text insertion
        -> Selection -> TTS -> isolated PCM playback
        -> Region capture -> OCR / LLM -> analysis dialog
        -> System audio -> realtime STT -> caption coordinator -> overlay
        -> Explicit MCP request -> bounded text / analysis interface
        -> Settings / consent / provider selection / diagnostics

## Funktions- und Abnahmestand

| Funktion | Implementierung | Noch zu pruefen |
| --- | --- | --- |
| Diktat | OpenAI; Groq; OpenVINO CPU; Faster-Whisper CUDA | reale Zielanwendungen, GPU |
| Vorlesen | Cartesia; separat qualifizierter Qwen-Worker | Audioausgabe, autonomer Qwen-Worker |
| Analyse | OCR; OpenAI; lokale GGUF-Modelle; optional Anthropic | reale Bildschirmregion, lokale Modelle |
| Live-Untertitel | OpenAI Realtime; Cartesia Ink Preview (experimentell); optional Cloud-Uebersetzung | WASAPI, Overlay, laengere Sitzungen |
| MCP | explizite stdio-Schnittstelle | externe Client-Abnahme |
| Offline-Uebersetzung / Streaming-STT | nicht freigegeben | Qualitaets- und Laufzeittests |

OpenAI und Cartesia wurden mit synthetischen Eingaben auf tatsaechliche
Erreichbarkeit geprueft. Das ist keine Garantie fuer zukuenftige Verfuegbarkeit,
Kontingente oder Audioqualitaet. Andere Provider erfordern gesonderte Abnahme.
Vor einem Aufruf muessen Zugangsdaten und die erforderliche Freigabe vorhanden
sein. Cloud-Schluessel werden zur Laufzeit ueber Umgebungsvariablen gelesen
(OPENAI_API_KEY, GROQ_API_KEY, CARTESIA_API_KEY, optional ANTHROPIC_API_KEY).
Groq Whisper Large V3 verarbeitet abgeschlossene Aufnahmen, keine Live-Untertitel.
Cartesia Ink Preview ist eine optionale experimentelle Live-STT-Schnittstelle.
Sie nutzt eine eigene Freigabe fuer Systemaudio; OpenAI bleibt Standard.
Bei aktivierter Cloud-Uebersetzung ist auch die OpenAI-Freigabe notwendig.
Die Preview-Version kann sich serverseitig aendern. Keine automatische Ausweichroute.
Die Groq-Freigabe ist getrennt von OpenAI. Kosten sind je nach Tarif moeglich.
Nicht in Dateien oder Git speichern.

## Freigabekriterien

Dieser private Quellcode-Stand enthaelt keine Forschungsmodelle, privaten
Sprachdaten, Entwicklungsprotokolle oder Installationsumgebungen.
Vor einer oeffentlichen Freigabe: Lizenz und Rechte bestaetigen; unabhaengige
Installation, reale Hotkeys und Zielanwendungen, Mikrofon, Systemaudio und
Wiedergabe pruefen; lokale GPU- und CPU-Backends separat qualifizieren;
bekannte Einschraenkungen dokumentieren. Bis dahin keine oeffentliche
Veroeffentlichung und keine Freigabe als fertiges Installationspaket.

## Offline-Prüfung

```powershell
.\.venv\Scripts\python.exe scripts\run_offline_tests.py
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\smoke_app_offscreen.py
.\.venv\Scripts\python.exe scripts\smoke_captions_mock.py
.\.venv\Scripts\python.exe scripts\check_wheel.py --clean-venv
# Optional: fresh installation with all Python extras (package index needed)
.\.venv\Scripts\python.exe scripts\check_wheel.py --full-runtime
```

Mock-Tests ersetzen keine Prüfung mit echtem Mikrofon, Lautsprecher, GPU oder
anderen Windows-Anwendungen.
