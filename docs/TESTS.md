# Prüfung und Abnahmestand

## Automatisierte Prüfungen

Aus dem Quellordner nach Installation:

```powershell
.\.venv\Scripts\python.exe scripts\run_offline_tests.py
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\smoke_app_offscreen.py
.\.venv\Scripts\python.exe scripts\smoke_captions_mock.py
.\.venv\Scripts\python.exe scripts\demo_captions.py --self-test
.\.venv\Scripts\python.exe scripts\check_wheel.py --clean-venv
```

Die Tests verwenden synthetische Daten und nachgebildete Schnittstellen. Sie starten keine Modelle und führen keine kostenpflichtigen API-Aufrufe aus. Der Wheel-Build kann Build-Abhängigkeiten aus dem Paketindex beziehen.

## Abnahmematrix

| Prüfung | Stand |
| --- | --- |
| Offline-Regression | Automatisierter Test |
| Start, Dialoge, Abbruch und Beenden | Qt-Offscreen-Smoke; Tray-Start, Fenster ausblenden, Beenden und verzögerte Freigabe |
| Untertitel und Übersetzungsaufträge | Mock- und Protokolltests |
| Unabhängige Installation aller Python-Abhängigkeiten | Offline-Wheelhouse, geprüft auf Host und in Windows Sandbox |
| Native Hotkey-Registrierung, Konflikt, Freigabe | In Windows Sandbox geprüft |
| OpenAI, Groq, Cartesia | Begrenzte echte API-Tests mit synthetischen Daten |
| Reale Mikrofone, Systemaudio, Lautsprecher | Ausstehende native Abnahme |
| Notepad, Word und Browser | Kompatibilitätsprüfung ausstehend |
| CUDA, GPU-LLM und lokales TTS | Separate Modell-/GPU-Abnahme erforderlich |
| Lokale Echtzeitübersetzung | Noch nicht freigegeben |
| Lizenz und verteilbares EXE/MSI | Nicht abgeschlossen |

## Native Prüfschritte

Vor einer Live-Demonstration nur ein leeres Testdokument verwenden.

- Einmalige Eingabe: Hotkey für Aufnahme, kurzer synthetischer Testsatz, Stopp; Ergebnis im ursprünglichen Feld prüfen.
- Fokuswechsel während der Aufnahme: kein Text im falschen Fenster.
- Geschütztes oder schreibgeschütztes Feld: keine automatische Eingabe.
- Sprache DE/RU: richtige Aussprache und Textsprache.
- Untertitel: eigene Startfreigabe, korrekte Trennung von Original und Übersetzung, Stopp beendet Capture.
- Abbruch und Fehler: keine verwaiste Audioaufnahme, kein blockierter Hotkey, keine unerwartete Providerumschaltung.

`scripts/test_direct_input.py --arm` ermöglicht eine einzelne ausdrücklich gestartete synthetische Texteingabe in ein leeres Testfeld. Der Test prüft keine echte Spracherkennung. Sichtbares Ergebnis kontrollieren.

Mock-Tests, synthetische API-Eingaben und eine VM ohne physische Geräte sind keine vollständige Produktabnahme. Die Ergebnisse gelten nur für die geprüften Konfigurationen.
