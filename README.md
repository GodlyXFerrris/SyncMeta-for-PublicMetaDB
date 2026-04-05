# SyncMeta: Multi-Source Sync for PublicMetaDB

SyncMeta is a self-hostable web dashboard for mirroring selected SIMKL, AniList, Trakt, and MDBList lists into PublicMetaDB.

It is now a web-first, Docker-first project. Users create persistent profiles, choose exactly what should sync, and let the server keep those selections updated in the background.

## Features

- Multi-source sync from SIMKL, AniList, Trakt, and MDBList into PublicMetaDB
- Per-source selection for SIMKL statuses, AniList states, Trakt catalogs, and MDBList lists
- Background sync with saved profiles and a minimum interval of 300 seconds
- Source visibility controls for private/public PublicMetaDB output
- Trakt watched history sync
- Trakt resume / continue-watching sync
- Inline dashboard delete flow that also clears the matching saved source selection
- `Select All` and `Deselect All` for Trakt and MDBList pickers
- Server-side sessions, hashed passwords, encrypted saved credentials, and login throttling
- Optional site-wide access password

## Supported Sources

### SIMKL

- Shows, movies, and anime
- Status selection by media type
- Watching
- Plan to Watch
- Completed
- On Hold
- Dropped

### AniList

- Anime lists
- Public sync by username
- Optional token for private lists
- Watching
- Completed
- Paused
- Dropped
- Planning

### Trakt

- Watchlist
- Default personal catalogs
- Liked lists
- Public Discover lists
- Device auth in the web UI
- Optional watched history import
- Optional resume/progress sync

### MDBList

- Selected MDBList account lists
- Per-list selection in the web UI

### Destination

- PublicMetaDB lists
- Per-source private/public visibility controls

## What SyncMeta Creates

SyncMeta creates clean PublicMetaDB list names without source prefixes. Examples:

- `Watching - Series`
- `Plan to Watch - Movies`
- `Planning - Anime`
- `Watchlist - Movies`
- `Watchlist - Series`
- `Recommended Movies`
- `Popular Netflix Movies`

## Docker Quick Start

Requirements:

- Docker Engine or Docker Desktop
- Docker Compose support

Start the app:

```bash
docker compose up -d --build web
```

Open:

- `http://127.0.0.1:8080`

The included Compose setup:

- builds from the local `Dockerfile`
- serves the Flask app through Gunicorn
- exposes port `8080`
- keeps profile data in `./data`
- uses one web worker, which matches the in-process scheduler design

Current `docker-compose.yml`:

```yaml
services:
  web:
    build: .
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      PROFILE_STORE_FILE: /app/data/profiles.json
      SYNCMETA_MASTER_KEY: ${SYNCMETA_MASTER_KEY:-}
      SITE_ACCESS_PASSWORD: ${SITE_ACCESS_PASSWORD:-}
    volumes:
      - ./data:/app/data
```

Stop it:

```bash
docker compose down
```

## Safe Updates

Normal rebuilds keep user data as long as these stay intact:

- `./data/profiles.json`
- `./data/profiles.key` if you are not using `SYNCMETA_MASTER_KEY`

Safe update flow:

```bash
docker compose pull
docker compose up -d --build web
```

If you want the most reliable setup across fresh hosts or re-deploys, set a fixed `SYNCMETA_MASTER_KEY`.

## Optional `.env`

You do not need a `.env` file for normal use if users enter everything in the dashboard.

Use `.env` only for deployment overrides like:

- `SITE_ACCESS_PASSWORD`
- `SYNCMETA_MASTER_KEY`
- session / throttle tuning
- optional server-side source prefills

Start from:

```bash
cp .env.example .env
```

## Local Python Run

If you want to run the web app without Docker:

```bash
pip install -r requirements.txt
python web.py
```

Default address:

- `http://127.0.0.1:8080`

## Web Workflow

Each profile has:

- a UUID
- a password
- encrypted saved credentials
- source selections
- latest sync results
- sync history
- background scheduling

Security behavior:

- passwords are hashed before storage
- source credentials are encrypted at rest
- saved secrets are not returned to the browser as raw values
- browsers use server-side sessions after login
- login and site-access attempts are throttled

Secret fields are overwrite-only. If a field says `Stored securely for this profile`, leaving it blank keeps the existing saved secret.

## Source Setup Notes

- SIMKL app setup may ask for a redirect URL, but SyncMeta uses PIN auth in the web UI.
- Trakt app setup may ask for a redirect URL, but SyncMeta uses device auth in the web UI.
- AniList only needs a token for private lists.
- MDBList uses an API key from your MDBList account.
- PublicMetaDB needs your API key from [publicmetadb.com/api-docs](https://publicmetadb.com/api-docs).

## Sync Options

- Automatic background sync
- Update interval in seconds, minimum `300`
- Remove items no longer in source lists
- Delete SyncMeta-managed PublicMetaDB lists when they are disabled
- Sync Trakt watched history
- Sync Trakt resume progress
- Per-source private/public visibility controls
- Dry runs

## API Endpoints

- `/` - dashboard
- `/api/profile/login` - sign in with UUID and password
- `/api/profile/logout` - clear the active session
- `/api/profile/save` - create or update a profile
- `/api/profile/status` - get current profile/dashboard state
- `/api/profile/sync` - trigger a sync or dry run
- `/api/profile/list/delete` - delete a synced PublicMetaDB list and clear the matching selection
- `/api/simkl/pin/start` - start SIMKL PIN auth
- `/api/simkl/pin/check` - poll SIMKL PIN auth
- `/api/trakt/device/start` - start Trakt device auth
- `/api/trakt/device/check` - poll Trakt device auth
- `/api/trakt/catalogs` - load liked or discovered Trakt lists
- `/api/mdblist/lists` - load MDBList lists

## Environment Variables

These are mainly useful for Docker deployment and optional server defaults.

### Core app settings

| Variable | Description |
|---|---|
| `PROFILE_STORE_FILE` | Path to the JSON profile store |
| `SYNCMETA_MASTER_KEY` | Optional Fernet key for encrypted saved credentials |
| `SYNCMETA_MASTER_KEY_FILE` | Optional key-file path if you do not want the default |
| `SITE_ACCESS_PASSWORD` | Shared password that gates the whole site |
| `DISABLE_PROFILE_SCHEDULER` | Set to `1` to disable background scheduling |
| `SYNCMETA_SESSION_TTL_SECONDS` | Session lifetime for signed-in browsers |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | Max login attempts inside the throttle window |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | Login throttle window |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | Max site-access attempts inside the throttle window |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | Site-access throttle window |

### Optional source defaults

| Variable | Description |
|---|---|
| `SIMKL_CLIENT_ID` | Optional SIMKL client ID default |
| `SIMKL_CLIENT_SECRET` | Optional SIMKL client secret default |
| `SIMKL_ACCESS_TOKEN` | Optional SIMKL token default |
| `ANILIST_USERNAME` | Optional AniList username default |
| `ANILIST_ACCESS_TOKEN` | Optional AniList token default |
| `TRAKT_CLIENT_ID` | Optional Trakt client ID default |
| `TRAKT_CLIENT_SECRET` | Optional Trakt client secret default |
| `TRAKT_ACCESS_TOKEN` | Optional Trakt access token default |
| `TRAKT_REFRESH_TOKEN` | Optional Trakt refresh token default |
| `MDBLIST_API_KEY` | Optional MDBList API key default |
| `PMDB_API_KEY` | Optional PublicMetaDB API key default |

## Project Structure

```text
web.py
src/
  anilist_client.py
  config.py
  matcher.py
  mdblist_client.py
  profile_store.py
  publicmetadb_client.py
  simkl_client.py
  sync_service.py
  trakt_client.py
templates/
  index.html
tests/
  test_anilist_client.py
  test_matcher.py
  test_mdblist_client.py
  test_profile_store.py
  test_sync_service.py
  test_trakt_client.py
  test_web.py
```

## Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run tests:

```bash
python -m unittest discover -v
```

Notes:

- PublicMetaDB requests use retry logic and rate limiting
- dry runs are recorded in history but do not advance the automatic schedule
- the scheduler runs in the web process and starts sync jobs in background threads
- deleting a synced list from the dashboard also clears the matching saved source selection when it can be mapped back to the saved profile data

## Current Limits

- Automatic syncing depends on the web process staying alive
- The scheduler is designed around a single active web worker
- A VPS admin can still theoretically extract secrets from a live server, even though SyncMeta hides them from the browser and encrypts them at rest

## License

See `LICENSE`.
