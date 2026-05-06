# SyncMeta for PublicMetaDB

[![Deploy to Docker](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Selbstgehostete Web-App, die Watchlists, Watch-History und Resume-Fortschritt von SIMKL, AniList, Trakt und MDBList automatisch in [PublicMetaDB](https://publicmetadb.com) synchronisiert.

## Kernfunktionen

- **Multi-Source-Sync** – Watchlists und History von SIMKL, AniList, Trakt und MDBList in einem Lauf
- **Watch History** – Importiert abgeschlossene Titel aus SIMKL und Trakt; Einträge mit ≥ 80 % Trakt-Fortschritt werden direkt als gesehen markiert
- **Resume Progress** – Speichert den Trakt-Wiedergabefortschritt als PMDB-Resumepunkt
- **PMDB-Watchlist** – Führt Plan-to-Watch-Einträge aus mehreren Quellen in der nativen PMDB-Watchlist zusammen
- **Anime-Spezialisierung** – Prequel-Chain-Cache, Fribb-Mapping, AniList-/MAL-Auflösung und PMDB-Episode-Fallback
- **Multi-Profil** – Jedes Profil hat eigene, AES-verschlüsselte Zugangsdaten und ein eigenes Passwort
- **Admin-Panel** – Profilübersicht, manuelle Syncs, Queue-Ansicht (via `ADMIN_PASSWORD`)
- **Hintergrund-Scheduler** – Automatischer Sync im konfigurierbaren Intervall

## Schnellstart

```bash
git clone https://github.com/Febsho/SyncMeta-for-PublicMetaDB
cd SyncMeta-for-PublicMetaDB
cp .env.example .env   # Werte anpassen (siehe unten)
docker compose up -d syncmeta
```

Die App ist dann unter `http://127.0.0.1:8080` erreichbar.

1. **Profil erstellen** – Im Web-UI UUID und Passwort vergeben
2. **Quellen verbinden** – API-Keys für PublicMetaDB, SIMKL, AniList, Trakt und/oder MDBList eintragen
3. **Listen & History wählen** – Statusfilter, History-Sync und Resume-Sync aktivieren
4. **Sync auslösen** – Manuell per Dashboard oder automatisch per Scheduler

## Wichtige Umgebungsvariablen

Alle Sync-Einstellungen (API-Keys, Listen, History) werden pro Profil im Web-UI gespeichert. Die folgenden Server-Variablen werden einmalig in der `.env` gesetzt:

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `SYNCMETA_MASTER_KEY` | Empfohlen | Verschlüsselungsschlüssel für Profildaten. Stabil halten – sonst gehen gespeicherte Zugangsdaten verloren. Wird automatisch generiert, wenn leer. |
| `ADMIN_PASSWORD` | Optional | Aktiviert das Admin-Panel (`/admin`). Ohne diesen Wert ist das Panel deaktiviert. |
| `SITE_ACCESS_PASSWORD` | Optional | Globales Zugriffspasswort vor dem Laden der App. |
| `SYNCMETA_MAX_CONCURRENT_SYNCS` | Optional | Maximale Anzahl gleichzeitiger Profil-Syncs (Standard: `4`). |
| `PROFILE_STORE_FILE` | Optional | Pfad zur Profil-Datenbankdatei (Standard: `/app/data/profiles.json`). |
| `DISABLE_PROFILE_SCHEDULER` | Optional | Auf `1` setzen, um den Hintergrund-Scheduler zu deaktivieren. |

Eine vollständige Liste aller Variablen mit Standardwerten steht in `.env.example`.

## Entwicklung

```bash
pip install -r requirements.txt
python web.py          # http://127.0.0.1:8080
python -m unittest discover -v   # Tests ausführen
```

## Lizenz

MIT – Details in der `LICENSE`-Datei.
