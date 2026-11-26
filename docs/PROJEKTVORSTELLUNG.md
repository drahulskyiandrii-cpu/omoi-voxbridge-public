# Projektvorstellung – OMOI VoxBridge

## Kurzvortrag (ca. 5 Minuten)

**1. Ausgangssituation (45 Sekunden)**

Ich möchte Sprache direkt in Windows-Anwendungen verwenden können, ohne für jede Aufgabe ein separates Programm zu öffnen. Deshalb entwickle ich OMOI VoxBridge. Die Anwendung soll Sprache in Text umwandeln, Texte vorlesen, Untertitel anzeigen und bei technischen Texten helfen.

**2. Ziel und Funktionsumfang (45 Sekunden)**

Die Funktionen sind über ein Tray-Symbol und Tastenkombinationen erreichbar. Ich kann zwischen lokalen Modellen und Cloud-Diensten wählen. Cloud-Aufrufe werden nur nach einer ausdrücklichen Freigabe ausgeführt.

**3. Technischer Aufbau (90 Sekunden)**

Ich verwende Python für die Programmlogik und PySide6 für die Oberfläche. Windows-APIs ermöglichen globale Hotkeys, die Prüfung von Eingabefeldern und den Audiozugriff. Die Verarbeitung findet in Hintergrundthreads und getrennten Prozessen statt. Die Oberfläche bleibt dadurch bedienbar.

Die verschiedenen Modelle sind über Adapter eingebunden. Damit kann ich den Provider wechseln, ohne die komplette Benutzeroberfläche umzubauen. Ein Sitzungsobjekt verhindert, dass Ergebnisse einer alten Aufnahme in eine neue Sitzung gelangen.

**4. Demonstration (90 Sekunden)**

Zuerst zeige ich den Start per Verknüpfung und die Diagnose. Danach öffne ich die Offline-Untertitel-Demo. Sie zeigt vorbereitete deutsche Sätze und russische Übersetzungen. Zum Schluss erkläre ich anhand der Dateien `session.py` und `captions_core.py`, wie die Anwendung Zustände und Ergebnisse verwaltet.

**5. Ergebnis und Ausblick (30 Sekunden)**

Die Anwendung lässt sich unabhängig installieren. Automatisierte Tests und eine Windows-Sandbox prüfen die grundlegende Funktion. Reale Mikrofon- und Lautsprecherprüfungen sowie GPU-Tests sind separate Abnahmeschritte. Mein Ziel ist eine stabile, verständliche Anwendung mit austauschbaren Schnittstellen.

## Reproduzierbare Vorführung ohne Internet

- Diagnose: `.\.venv\Scripts\voxbridge-doctor.exe`
- Untertitel: `.\.venv\Scripts\python.exe scripts\demo_captions.py`
- Tests: `.\.venv\Scripts\python.exe scripts\smoke_captions_mock.py`
- Optional: Quellcode von `src/voxbridge/session.py` öffnen und `begin`, `advance`, `cancel` erklären.

Die Demo zeigt **vorbereitete Beispielsätze**, nicht live erzeugte Übersetzungen. Für eine Live-Vorführung müssen Gerät, Provider, Freigabe und Zielanwendung zuvor separat geprüft sein.

## Fachbegriffe für Rückfragen

| Begriff | Kurze Erklärung |
| --- | --- |
| STT | Speech-to-Text: Sprache in Text umwandeln |
| TTS | Text-to-Speech: Text als Audio ausgeben |
| WASAPI | Windows-Schnittstelle für Audio, auch Systemaudio |
| UI Automation | Schnittstelle zur Prüfung von Oberflächenelementen |
| Adapter | Einheitliche Schnittstelle für unterschiedliche Provider |
| Thread | Separater Ausführungsablauf innerhalb eines Prozesses |
| IPC | Datenaustausch zwischen Prozessen |
| Regressionstest | Prüfung, ob bestehende Funktionen nach Änderungen weiter funktionieren |
| Job Object | Windows-Mechanismus zur kontrollierten Verwaltung eigener Prozesse |
| MCP | Protokoll für begrenzte Werkzeuge externer KI-Clients |

## Warum ist das Systemintegration?

Das Projekt verbindet Betriebssystem, Benutzeroberfläche, Audiohardware, lokale Laufzeiten, Netzwerkprotokolle, Konfiguration, Zugriffsfreigaben und Diagnose. Nicht das Training eines eigenen KI-Modells steht im Mittelpunkt, sondern die nachvollziehbare und sichere Integration vorhandener Komponenten.
