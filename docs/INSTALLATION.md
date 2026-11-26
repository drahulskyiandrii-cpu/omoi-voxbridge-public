# Installation und Konfiguration

Windows 11, Python 3.11 x64. Keine automatische Modellinstallation.

## Unabhängige Installation aus dem Wheel

Voraussetzung: 64-Bit Python 3.11 auf Windows. Das geprüfte Wheel und
dessen SHA256-Prüfsumme separat bereitstellen. Keine Administratorrechte,
kein Autostart, kein Zugriff auf vorhandene Entwicklungsumgebungen.
Standardziel: LocalAppData/Programs/OMOI-VoxBridge. Das Ziel darf noch
nicht existieren; vorhandene Installationen werden nie überschrieben.

    py -3.11 -I scripts/install_standalone.py --wheel C:/Downloads/omoi_voxbridge-0.1.0-py3-none-any.whl --sha256 SHA256 --plan

Nach Kontrolle denselben Befehl ohne --plan ausführen. Optional
--wheelhouse C:/Downloads/wheelhouse für eine vollständige lokale
Wheelsammlung (keine Netzwerkabhängigkeiten während der Installation).
Ohne wheelhouse werden Python-Abhängigkeiten von PyPI bezogen.
Start-VoxBridge.cmd und Doctor.cmd liegen im neuen Installationsordner.
Die beiden Startskripte verwenden ein separates Profil unter
LocalAppData/OMOI-VoxBridge-Standalone. Die Entwicklungsprofile und
bestehenden Berechtigungen werden nicht übernommen oder verändert. Zum Entfernen erst
VoxBridge normal beenden und dann nur den eigenen Installationsordner
entfernen. Benutzerprofile und Modellordner nicht automatisch löschen.
Dies ist ein Python-basierter Installer, kein selbstenthaltendes EXE/MSI.

## Start und Desktop-Verknüpfungen

Nach erfolgreicher unabhängiger Installation können Verknüpfungen erstellt werden:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create_shortcuts.ps1
```

Optional mit -SourceDir und einem absoluten Pfad zur geprüften
Quellcode-Fassung (mit SOURCE_MANIFEST.json). Das Skript erzeugt
OMOI VoxBridge, Diagnose und optional Praesentation auf dem Desktop.
Abweichende vorhandene Verknüpfungen werden nicht überschrieben.
Es startet keine Anwendung und aktiviert keinen Autostart.
Der unabhängige Start verwendet standardmäßig das separate Profil
OMOI-VoxBridge-Standalone und startet im Infobereich. Eine Sperrdatei
verhindert mehrere unabhängige Instanzen mit demselben Profil.
Doppelklick auf das Tray-Symbol öffnet das Kontrollzentrum; das Schließkreuz
blendet es aus. Beenden im Tray-Menü stoppt die Anwendung. Bei verzögerter
Freigabe zeigt das Tray die wartenden Komponenten an und beendet sich
nach deren Freigabe automatisch.

## Windows Sandbox

Windows Sandbox: scripts/sandbox_acceptance.ps1 ist ein expliziter
Test für eine frische VM, keine normale Installation. Die VM soll
keinen Netzwerk-, GPU-, Mikrofon- oder Clipboard-Zugriff haben.
Nur ein eigens vorbereiteter schreibgeschützter Eingabeordner und
ein separater Ergebnisordner werden eingebunden. Keine privaten
Modelle, Anwendungsprofile oder Zugangsdaten freigeben.
scripts/prepare_sandbox.py --help beschreibt die Vorbereitung aus einem
lokalen, signaturgeprüften Python-Installer oder PSF-NuGet-Paket und einem Wheelhouse.
Mit --python-nupkg lässt sich die MSI-Installation in der Test-VM vermeiden.
Die historische Sandbox-Basis Python 3.11.9 ist nur ein Teststand,
keine Empfehlung zur Installation einer veralteten Python-Version.

## Lokale Laufzeit (separat vorbereitete Komponenten)

Der installierte Startbefehl voxbridge-standalone verwendet standardmäßig
einen eigenen Laufzeitordner unter dem VoxBridge-Profil. Mit
VOXBRIDGE_RUNTIME_ROOT kann ein anderer absoluter Ordner gewählt werden.
Der bisherige Startbefehl bleibt nur für bestehende Entwicklungsumgebungen.
Im unabhängigen Modus gibt es keinen automatischen Rückgriff auf Mini-Assets.

Alle Pfade sind Beispiele. Die Meldung OPENVINO_WORKER_NOT_INSTALLED
bedeutet, dass Node, der SDK-Ordner oder der Modellordner fehlt.
Das Programm installiert diese Komponenten nicht automatisch. Modelle und externe Laufzeiten werden nicht
mitgeliefert. Variablen in PowerShell vor dem Start setzen; nur die für
das gewählte Backend erforderlichen Werte angeben.

```powershell
$env:VOXBRIDGE_RUNTIME_ROOT = 'D:\SpeechRuntime'

# CPU STT: Node >=21 on PATH; tested Node 24.20.0, SDK 2026.4.0
# SDK includes node_modules/openvino-genai-node
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


## Cloud-Konfiguration

API-Schlüssel werden aus der Prozessumgebung gelesen: OPENAI_API_KEY, GROQ_API_KEY, CARTESIA_API_KEY und optional ANTHROPIC_API_KEY. Nicht in Git speichern. Die Freigabe für Diktat, Sprachausgabe, Analyse, Systemaudio und Übersetzung ist pro Provider getrennt. Groq verarbeitet abgeschlossene Aufnahmen. Cartesia Ink Preview ist experimentell. Cloud-Übersetzung bei Cartesia-STT verwendet weiterhin eine separate OpenAI-Freigabe.
