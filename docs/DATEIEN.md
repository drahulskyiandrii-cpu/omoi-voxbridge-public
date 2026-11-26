# Ordner und Dateien einfach erklärt

## 1. Wo beginne ich?

Alle relativen Pfade beginnen im Projektordner `OMOI-VoxBridge`. Der Ordner kann an einem beliebigen Ort liegen; die Beispiele benötigen keinen bestimmten Benutzernamen.

```text
OMOI-VoxBridge/
  README.md             Einstieg
  pyproject.toml         Paket und Abhängigkeiten
  src/
    voxbridge/           Programmcode
  tests/                Automatische Tests
  scripts/              Installation, Start und Prüfungen
  docs/                 Dokumentation
  AGENTS.md             Technische Arbeitsregeln
  LICENSE_STATUS.md     Lizenzstatus
  THIRD_PARTY_NOTICES.md Fremdbibliotheken
  SOURCE_MANIFEST.json  Versionsstand und Prüfsummen
  .gitignore            Ausgeschlossene lokale Dateien
```

`src` enthält die eigentliche Anwendung. `tests` prüft den Code, `scripts` führt Hilfsaufgaben aus, `docs` erklärt das Projekt. Die Ordner sind getrennt, damit die Anwendung ohne Forschungsdateien installiert werden kann.

## 2. Die fünf wichtigsten Dateien

| Relativer Pfad | Bedeutung |
| --- | --- |
| `src/voxbridge/app.py` | Hauptsteuerung: verbindet alle Funktionen |
| `src/voxbridge/config.py` | Einstellungen und deren Prüfung |
| `src/voxbridge/session.py` | Zustände einer Aufnahme und Abbruch |
| `src/voxbridge/control_center.py` | Bedienoberfläche |
| `src/voxbridge/captions_core.py` | Verwaltung der Untertitel |

Für eine Code-Vorstellung genügen zunächst diese Dateien. Ein guter Ablauf: Start → Sitzungszustand → Audio → STT → geprüftes Zielfeld.

## 3. Wo liegen Einstellungen und Modelle?

Der Quellcode enthält keine privaten Profile, API-Schlüssel oder Modellgewichte. Die Startskripte einer unabhängigen Wheel-Installation verwenden die folgenden Windows-Pfade:

```text
%LOCALAPPDATA%\Programs\OMOI-VoxBridge\
  Start-VoxBridge.cmd
  Doctor.cmd
  venv\

%LOCALAPPDATA%\OMOI-VoxBridge-Standalone\
  settings.json
  captions.json
  cloud_permissions.json
  cartesia_voices.json
  local-runtime\
```

Die Profildateien entstehen bei Bedarf. Das Verzeichnis `local-runtime` wird nicht automatisch mit Modellen gefüllt. Die bestehende Entwicklungsvariante nutzt standardmäßig `%LOCALAPPDATA%\OMOI-VoxBridge`. Mit `VOXBRIDGE_PROFILE_ROOT` kann ein absoluter Profilpfad und mit `VOXBRIDGE_RUNTIME_ROOT` ein absoluter Laufzeitpfad angegeben werden. API-Schlüssel kommen aus Umgebungsvariablen, nicht aus diesen JSON-Dateien.

`%LOCALAPPDATA%` ist eine Windows-Umgebungsvariable. Der Pfad lässt sich direkt in die Adressleiste des Explorers eingeben. Er hängt nicht vom Namen eines Projektordners ab.

## 4. Wichtige Hilfsskripte

| Relativer Pfad | Aufgabe |
| --- | --- |
| `scripts/install-dev.ps1` | Erstellt bzw. verwendet die projektbezogene .venv und installiert Abhängigkeiten. |
| `scripts/start-dev.ps1` | Startet die Entwicklungsvariante. |
| `scripts/install_standalone.py` | Installiert ein geprüftes Wheel in einen neuen separaten Ordner. |
| `scripts/demo_captions.py` | Zeigt Beispielsätze im Untertitel-Fenster, ohne API oder Modelle. |
| `scripts/run_offline_tests.py` | Führt Regressionstests aus. |
| `scripts/check_wheel.py` | Baut und prüft das Python-Paket. |
| `scripts/prepare_sandbox.py` | Bereitet einen isolierten Sandbox-Test vor. |
| `scripts/sandbox_acceptance.ps1` | Prüft Installation und grundlegende Funktionen in der Sandbox. |
| `scripts/smoke_app_offscreen.py` | Testet den Anwendungsstart ohne sichtbare Oberfläche. |
| `scripts/smoke_captions_mock.py` | Testet Untertitel mit simulierten Übersetzungen. |
| `scripts/check_hotkey_lifecycle.py` | Prüft ausdrücklich registrierte Test-Hotkeys. |
| `scripts/test_direct_input.py` | Einmaliger, ausdrücklich aktivierter Test der Texteingabe. |
| `scripts/start_mcp_stdio.py` | Startet die explizite MCP-Textschnittstelle. |

Weitere Informationen: [Installation](INSTALLATION.md), [Architektur](ARCHITEKTUR.md), [Projektvorstellung](PROJEKTVORSTELLUNG.md), [Tests](TESTS.md).

## 5. Vollständige Karte des Programmcodes

Alle folgenden Dateien liegen unter `src/voxbridge/`. Die Gruppen beschreiben Verantwortlichkeiten; sie sind keine zusätzlichen Unterordner.

### Start und Steuerung

| Datei | Aufgabe |
| --- | --- |
| `__init__.py` | Paketkennung und Version. |
| `__main__.py` | Start mit python -m voxbridge. |
| `app.py` | Verbindet Tray, Hotkeys und Funktionen. |
| `config.py` | Lädt, prüft und speichert Einstellungen. |
| `session.py` | Verwaltet Sitzungszustand und Generation. |
| `control_center.py` | Zentrales Bedienfenster. |
| `desktop_settings_ui.py` | Dialog für lokale Einstellungen. |
| `status_ui.py` | Zeigt Aufnahmezustand und manuelle Textausgabe. |
| `private_dialog.py` | Räumt flüchtige Dialoginhalte beim Schließen auf. |
| `diagnostics.py` | Prüft Voraussetzungen ohne Geräte oder Modelle zu starten. |

### Windows und Eingabe

| Datei | Aufgabe |
| --- | --- |
| `hotkeys.py` | Registriert globale Tastenkombinationen. |
| `injection.py` | Fügt Unicode-Text in ein geprüftes Ziel ein. |
| `edit_target.py` | Prüft das fokussierte Eingabefeld. |
| `selection.py` | Liest ausdrücklich ausgewählten Text über UI Automation. |
| `selection_controller.py` | Steuert Auswahlaufträge und deren Abbruch. |
| `copied_text.py` | Liest auf ausdrücklichen Wunsch kopierten Text. |
| `windows_ocr.py` | Startet die lokale Windows-Texterkennung. |
| `windows_ocr.ps1` | PowerShell-Worker für Windows OCR. |

### Mikrofon und Spracherkennung

| Datei | Aufgabe |
| --- | --- |
| `audio.py` | Nimmt PCM-Audio im Arbeitsspeicher auf. |
| `microphone_devices.py` | Ermittelt verfügbare Eingabegeräte. |
| `microphone_lifecycle.py` | Steuert Öffnen und Schließen des Mikrofons. |
| `device_discovery.py` | Ermittelt Gerätemetadaten in einem getrennten Prozess. |
| `stt.py` | Adapter für lokale Spracherkennung. |
| `stt_enhancements.py` | Bereitet Audio und optionale Fachbegriffe auf. |
| `dictation_quality.py` | Erkennt offensichtlich fehlerhafte Transkripte. |
| `long_dictation.py` | Teilt lange Aufnahmen in begrenzte Abschnitte. |
| `local_partial.py` | Koordiniert lokale Zwischen- und Endergebnisse. |
| `stt_process.py` | Startet und beendet eigene STT-Prozesse. |
| `openvino_worker.mjs` | Node.js-Worker für OpenVINO. |
| `cuda_session_worker.py` | Worker für Faster-Whisper auf CUDA. |
| `cuda_warm.py` | Verwaltet ausdrücklich gestartete CUDA-Vorladung. |

### Vorlesen und Audioausgabe

| Datei | Aufgabe |
| --- | --- |
| `tts_ui.py` | Steuert Vorlesen, Modellwahl und Abbruch. |
| `tts_language.py` | Wählt die passende Ausgabesprache. |
| `tts_stream.py` | Verarbeitet PCM-Audio aus Cartesia-Streams. |
| `audio_output.py` | Steuert die Audioausgabe. |
| `output_isolation.py` | Überwacht den getrennten Ausgabeprozess. |
| `qwen_local_runtime.py` | Verwaltet die lokale Qwen-TTS-Laufzeit. |
| `qwen_local_worker.py` | Startet den qualifizierten externen Qwen-Worker. |
| `speech_core_tts.py` | Empfängt lokale PCM-Streams. |
| `cartesia_voices.py` | Ruft freigegebene Stimmen-Metadaten ab. |
| `voice_cache.py` | Speichert nur Stimmen-Metadaten lokal. |
| `voice_ui.py` | Dialog zur Auswahl von Cartesia-Stimmen. |

### Live-Untertitel

| Datei | Aufgabe |
| --- | --- |
| `live_captions_ui.py` | Dialog für Start, Stopp und Einstellungen. |
| `wasapi_capture.py` | Erfasst Systemton über Windows WASAPI. |
| `capture_isolation.py` | Isoliert möglicherweise blockierende Audiotreiber. |
| `realtime_transport.py` | Überträgt Audioblöcke an OpenAI Realtime. |
| `realtime_events.py` | Verarbeitet Nachrichten des OpenAI-Protokolls. |
| `cartesia_realtime.py` | Experimenteller Streaming-Adapter für Cartesia Ink. |
| `realtime_socket.py` | Begrenzt und steuert WebSocket-Verbindungen. |
| `captions_core.py` | Verwaltet Textsegmente und Übersetzungsaufträge. |
| `caption_batching.py` | Gruppiert abgeschlossene Satzfragmente. |
| `captions_translate.py` | Bereitet Übersetzung vor und ruft freigegebene Adapter auf. |
| `captions_overlay.py` | Zeigt Original und Übersetzung an. |
| `caption_preferences.py` | Speichert nicht vertrauliche Untertitel-Einstellungen. |

### Analyse und MCP

| Datei | Aufgabe |
| --- | --- |
| `analysis.py` | Prüft Textaufträge und erstellt Prompts. |
| `analysis_providers.py` | Schnittstellen für Analyse-Provider einschließlich Anthropic. |
| `analysis_stream.py` | Begrenzt Zwischenresultate für die GUI. |
| `analysis_ui.py` | Dialog für Textanalyse. |
| `vision.py` | Verarbeitet freigegebene Bildanalyse-Aufträge. |
| `vision_ui.py` | Auswahl und Vorschau eines Bildschirmausschnitts. |
| `local_llm.py` | Verwaltet lokale GGUF-Modelle und llama-server. |
| `mcp_stdio.py` | Lokaler MCP-Server über Standardein- und -ausgabe. |
| `mcp_tools.py` | Explizite Textwerkzeuge für externe MCP-Clients. |

### Verbindungen, Ressourcen und Freigaben

| Datei | Aufgabe |
| --- | --- |
| `cloud_providers.py` | Cloud-Adapter für STT, Analyse und TTS. |
| `cloud_http.py` | Begrenzter, abbrechbarer HTTP-Transport. |
| `cloud_budget.py` | Zeit- und Datenlimits je Anfrage. |
| `cloud_permissions.py` | Speichert getrennte Cloud-Freigaben. |
| `cloud_permissions_ui.py` | Dialog für Cloud-Freigaben und Widerruf. |
| `session_limits.py` | Begrenzt Anfragen und Audio pro Sitzung. |
| `runtime_paths.py` | Löst den expliziten lokalen Laufzeitpfad auf. |
| `local_services.py` | Prüft ausdrücklich konfigurierte lokale Dienste. |
| `loopback_http.py` | Zeitbegrenzte HTTP-Verbindung zu lokalen Diensten. |
| `gpu.py` | Prüft GPU-Speicher, ohne fremde Prozesse zu beenden. |
| `windows_network.py` | Prüft die Prozesszuordnung lokaler TCP-Ports. |
| `windows_owned_job.py` | Verwaltet eigene Windows-Prozessgruppen. |
| `windows_job_membership.py` | Prüft die Zugehörigkeit zu einer Prozessgruppe. |
| `owned_stt_launcher.py` | Startet den Zielprozess erst nach geprüfter Job-Zuordnung. |

## 6. Was gehört nicht in Git?

Virtuelle Python-Umgebungen (`.venv`), Cache-Dateien (`__pycache__`), gebaute Pakete (`dist`), Zugangsdaten, Aufnahmen, Modellgewichte und lokale Konfigurationen. `.gitignore` schließt typische lokale Dateien aus; der Quellcode-Export verwendet zusätzlich eine explizite Dateiliste. Die Prüfsummen im Manifest machen den exportierten Stand nachvollziehbar.
