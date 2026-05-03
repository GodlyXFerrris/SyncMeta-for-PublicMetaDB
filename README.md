# SyncMeta for PublicMetaDB

[![Deploy to Docker](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

SyncMeta ist eine selbstgehostete Web-App zur Synchronisierung deiner Watchlists und des Verlaufs von verschiedenen Quellen (SIMKL, AniList, Trakt, MDBList) in PublicMetaDB.

## Kernfunktionen

- **Multisource-Sync:** Synchronisiert Watchlists und den Verlauf von SIMKL, AniList, Trakt und MDBList.
- **Anime-Spezialisierung:** Verbesserte Handhabung von Anime-Listen, inklusive Prequel-Chain-Cache und PMDB-Mapping.
- **Watch History & Resume Progress:** Importiert den Verlauf und den Wiedergabefortschritt (Trakt).
- **Sichere Profile:** Jedes Benutzerprofil hat verschlüsselte Anmeldeinformationen und ist durch UUIDs und Passwörter geschützt.
- **Automatischer Hintergrund-Sync:** Konfigurierbarer automatischer Sync von Listen.

## Schnellstart

Die empfohlene Bereitstellung erfolgt über das vorgefertigte Docker-Image.

1.  **Repository klonen:**
    ```bash
    git clone https://github.com/Febsho/SyncMeta-for-PublicMetaDB
    cd SyncMeta-for-PublicMetaDB
    ```

2.  **SyncMeta starten:**
    ```bash
    docker compose up -d syncmeta
    ```
    Die Anwendung ist dann unter `http://127.0.0.1:8080` verfügbar.

3.  **Profil einrichten:**
    Im Web-UI kannst du ein Profil mit UUID und Passwort erstellen.

4.  **Konten verbinden & Listen wählen:**
    Verbinde deine API-Keys/Zugangsdaten für PublicMetaDB, SIMKL, AniList, Trakt und MDBList. Wähle aus, welche Listen synchronisiert werden sollen.

5.  **Manuelle Steuerung:**
    Über das Dashboard kannst du manuelle Syncs aller Listen, Watch History oder Resume Progress auslösen.

## Umgebungsvariablen

Die meisten Standardeinstellungen können über die `.env.example` oder direkt im Web-UI konfiguriert werden.

| Variable                   | Zweck                                           |
| :------------------------- | :---------------------------------------------- |
| `SYNCMETA_MASTER_KEY`      | Verschlüsselungsschlüssel für gespeicherte Anmeldeinformationen. |
| `PROFILE_STORE_FILE`       | Pfad zum JSON-Speicher der Profile.             |
| `SITE_ACCESS_PASSWORD`     | Optionales geteiltes Passwort zum Website-Zugriff. |
| `DISABLE_PROFILE_SCHEDULER`| Deaktiviert den automatischen Hintergrund-Sync. |

Für eine vollständige Liste der Variablen und ihrer Standardwerte siehe die `src/config.py`.

## Entwicklung

```bash
# Abhängigkeiten installieren
pip install -r requirements.txt

# Flask-App starten
python web.py
```
App ist dann unter `http://127.0.0.1:8080` verfügbar.

### Tests ausführen
```bash
python -m unittest discover -v
```

## Lizenz

Dieses Projekt steht unter der MIT-Lizenz. Details siehe `LICENSE` Datei.
