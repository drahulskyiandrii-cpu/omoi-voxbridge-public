# Architektur

## Komponenten

| Schicht | Dateien | Verantwortung |
| --- | --- | --- |
| Oberfläche | `control_center.py`, `desktop_settings_ui.py`, `live_captions_ui.py`, `tts_ui.py` | Qt-Dialoge, Anzeige, Bedienung |
| Steuerung | `app.py`, `session.py`, `config.py` | Hotkeys, Sitzungen, Konfiguration |
| Windows | `hotkeys.py`, `injection.py`, `edit_target.py`, `selection.py` | Native Hotkeys, Zielidentität, ausgewählter Text |
| Audio | `audio.py`, `capture_isolation.py`, `wasapi_capture.py`, `output_isolation.py` | Mikrofon, Systemaudio, getrennte Ein- und Ausgabeprozesse |
| Inferenz | `stt.py`, `cloud_providers.py`, `local_llm.py`, `qwen_local_runtime.py` | Lokale und externe Provider |
| Untertitel | `realtime_transport.py`, `cartesia_realtime.py`, `captions_core.py`, `captions_overlay.py` | Streaming, Segmentierung, Übersetzungsaufträge, Anzeige |
| Schnittstellen | `mcp_stdio.py`, `mcp_tools.py` | Begrenzte explizite Textoperationen |
| Ressourcen | `windows_owned_job.py`, `runtime_paths.py`, `cloud_budget.py`, `diagnostics.py` | Prozessgruppen, Pfade, Zeitlimits, Diagnose |

Alle Module liegen unter `src/voxbridge/`.

## Beispiel: Diktat

1. Hotkey erfasst Fenster und Fokus des ursprünglichen Textfelds.
2. Die Zielprüfung verwendet Win32-Metadaten oder UI Automation.
3. Eine Aufnahme wird erst nach gültiger Zielprüfung gestartet; ein unbestätigtes Ziel kann je nach Modus nur manuelle lokale Ausgabe erlauben.
4. Ein ausgewählter STT-Adapter verarbeitet die Aufnahme. Cloud-STT erfordert Freigabe.
5. Die Steuerung prüft Sitzungs-ID und Textfeld erneut. Native EDIT-Steuerelemente erhalten Text über EM_REPLACESEL, andere verifizierte Eingabefelder über Unicode-SendInput.
6. Bei einem Fokuswechsel oder geschützten Feld wird die automatische Eingabe abgelehnt.

`session.py` speichert Zustand und Generation. Ein verspätetes Ergebnis einer abgebrochenen Sitzung kann nicht in eine neue Sitzung übernommen werden.

## Beispiel: Live-Untertitel

WASAPI-Loopback erfasst ausdrücklich freigegebenes Systemaudio. Der Transport sendet PCM-Audio an den ausgewählten Streaming-STT-Provider. Vorläufige Wörter werden angezeigt; erst abgeschlossene Segmente erzeugen Übersetzungsaufträge. Der Koordinator begrenzt wartende Aufträge. Qt liest ausschließlich einen Snapshot. Das Overlay verarbeitet keine Modelle.

## Prozess- und Datenschutzgrenzen

- Qt-Thread: Bedienung, keine blockierende Inferenz.
- Worker: Gerätezugriff, Netzwerk und Modellberechnung mit Abbruch- und Zeitgrenzen.
- Windows Job Objects: nur kontrollierte eigene Prozesse; externe Dienste werden nicht beendet.
- Anbieterbezogene Freigaben: standardmäßig aus; keine stillschweigende Ausweichroute.
- API-Schlüssel: Prozessumgebung statt Konfigurationsdateien. Audiodaten und Text sind standardmäßig flüchtig.
- MCP: explizit übergebener Text, kein Zugriff auf Bildschirm, Zwischenablage, Dateien oder Shell.

Diese Mechanismen reduzieren Risiken, garantieren aber keine Kompatibilität mit sämtlichen Windows-Anwendungen. Die verbleibende OS-seitige Fokus-Race bei SendInput ist ein bekanntes technisches Limit.
