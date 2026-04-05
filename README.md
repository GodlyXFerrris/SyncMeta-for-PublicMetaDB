# SyncMeta: Multi-Source List Sync for PublicMetaDB

SyncMeta is a self-hostable sync dashboard for people who want their personal lists kept in sync across services without manually rebuilding them in PublicMetaDB.

It connects to SIMKL, AniList, Trakt, and MDBList, lets each user choose exactly which lists or statuses should be mirrored, and keeps the selected catalogs updated in private PublicMetaDB lists. It also supports persistent profiles, background syncing, dry runs, and secure hosted multi-user use.

## Features

- Multi-source sync: Pull from SIMKL, AniList, Trakt, and MDBList, then push into private PublicMetaDB lists.
- Fine-grained list selection: Choose SIMKL statuses per media type, AniList list states, specific Trakt catalogs including public lists, and selected MDBList account catalogs.
- Background automation: Save a profile once and let the server keep it updated on a schedule, with a minimum interval of 300 seconds.
- Multi-user profiles: Each user gets a UUID-backed profile with its own credentials, selections, sync history, and schedule.
- Secure hosted mode: Passwords are hashed, saved credentials are encrypted at rest, browsers use server-side sessions, and login attempts are throttled.
- Optional site-wide access gate: Set a shared site password if you want the whole instance behind a single private entry screen.
- Built-in auth helpers: SIMKL PIN auth and Trakt device auth can be started directly from the Settings page.
- Safe sync controls: Use dry runs, remove items missing from source lists, delete SyncMeta-managed PublicMetaDB lists when they are deselected, and delete user records from the dashboard when you want to wipe a profile.
- Docker-first deployment: Run the web dashboard with Docker Compose, or use the CLI for one-off sync jobs.

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
- Username-based public sync
- Optional access token for private lists
- List selection:
  - All
  - Watching
  - Completed
  - Paused
  - Dropped
  - Planning

### Trakt

- Watchlist catalogs
- Default catalogs
- Liked lists
- Selected public lists from Discover
- Device auth flow in the web UI

### MDBList

- Your MDBList account lists and curated catalog selections
- Per-list selection in the web UI

### Destination

- Private PublicMetaDB lists

## What SyncMeta Creates

SyncMeta creates clean PublicMetaDB list names without source prefixes. Examples:

- `Watching - Series`
- `Plan to Watch - Movies`
- `Planning - Anime`
- `My Watchlist`
- `Popular Movies`
- `Custom Trakt List Name`
- `MDBList List Name`

## How Matching Works

Items are resolved to TMDB IDs in this order:

1. Direct TMDB ID
2. IMDb
3. MyAnimeList
4. AniList
5. AniDB
6. TVDB
7. Root-series MAL or AniList fallback for anime sequels

That fallback helps map later anime seasons back to the main series when needed.

## Installation

### 1. Hosted Instance

If you are using a hosted SyncMeta instance:

1. Open the web dashboard.
2. Create a new profile or sign in with an existing UUID and password.
3. Connect the services you want to use.
4. Pick your SIMKL, AniList, Trakt, and MDBList lists.
5. Save the profile.
6. Let the background scheduler keep it updated.

### 2. Self-Hosting (Docker Compose)

Requirements:

- Docker Engine or Docker Desktop
- Docker Compose support

The repo already includes a working `docker-compose.yml`. The quickest start is:

```bash
docker compose up -d --build web
```

Open the dashboard at:

- `http://127.0.0.1:8080`

The included Docker setup:

- builds from the local `Dockerfile`
- serves the Flask dashboard through Gunicorn
- exposes port `8080`
- runs with one web worker, which matches the built-in background scheduler model
- does not require a `.env` file for normal web use

For production, a persistent data mount is strongly recommended so profiles, encrypted credentials, and the generated encryption key survive container recreation:

```yaml
services:
  web:
    build: .
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      PROFILE_STORE_FILE: /app/data/profiles.json
```

Then run:

```bash
docker compose up -d --build web
```

Stop it with:

```bash
docker compose down
```

### 3. Optional `.env` Overrides

You do not need a `.env` file for the normal web dashboard flow if users enter their credentials in the UI.

Create `.env` only if you want:

- CLI defaults
- Docker overrides
- a fixed encryption key
- deployment-specific settings

```bash
cp .env.example .env
```

Important production note:

- `PROFILE_STORE_FILE` controls where profiles are stored
- `SYNCMETA_MASTER_KEY` lets you provide your own encryption key
- if you do not set `SYNCMETA_MASTER_KEY`, SyncMeta creates a key file beside the profile store, usually `profiles.key`

### 4. Local Python Run

If you want to run the web app without Docker:

```bash
pip install -r requirements.txt
python web.py
```

Default address:

- `http://127.0.0.1:8080`

## Web Dashboard

SyncMeta is designed around persistent per-user profiles.

Each profile has:

- a generated UUID
- a password chosen by the user
- encrypted stored credentials
- selected source lists and statuses
- latest sync results
- sync history
- automatic background scheduling

Security behavior:

- passwords are hashed before storage
- saved source credentials are encrypted at rest
- saved secrets are not returned to the browser after login
- the browser uses a server-side session after login instead of storing the password locally
- login attempts are throttled

Secret fields are overwrite-only. If a field says `Stored securely for this profile`, leaving it blank keeps the current stored secret.

## Docker Notes

The default profile store path is:

- `data/profiles.json`

When credential encryption is enabled, SyncMeta also needs either:

- `SYNCMETA_MASTER_KEY` from the environment
- or the generated key file stored beside the profile store

With the default layout, that means persisting both:

- `data/profiles.json`
- `data/profiles.key`

Automatic background syncing works in Docker because the scheduler runs inside the web process.

Because the scheduler is in-process, the current Docker setup intentionally uses a single Gunicorn worker.

## CLI

The CLI is still available for one-off jobs, debugging, or scheduled shell-based syncs.

### Authenticate with SIMKL

```bash
python main.py auth
```

### One-time sync

```bash
python main.py sync
```

### Dry run

```bash
python main.py sync --dry-run
```

### Remove items missing from the source

```bash
python main.py sync --remove-missing
```

### Continuous CLI sync every 30 minutes

```bash
python main.py sync --interval 30
```

### Verbose logging

```bash
python main.py -v sync
```

### Use a JSON config file

```bash
python main.py -c config.json sync
```

### Docker CLI service

The repo also includes a one-shot CLI service:

```bash
docker compose run --rm sync
```

That runs:

```bash
python main.py sync
```

This path usually does need environment variables, because it runs the CLI directly instead of using a saved web profile.

## Configuration

### Source Setup

- SIMKL: Create an app at [simkl.com/settings/developer](https://simkl.com/settings/developer/)
- PublicMetaDB: Create an API key at [publicmetadb.com/api-docs](https://publicmetadb.com/api-docs)
- AniList: Username is enough for public lists; token is only needed for private lists
- Trakt: Create an app in Trakt's API settings, then use the built-in device auth helper
- MDBList: Generate an API key from your MDBList account

### Sync Options

- Automatic background sync
- Update interval in seconds, minimum `300`
- Remove items no longer in source lists
- Dry run before a real sync if you want to preview changes

## API Endpoints

Main dashboard and API routes:

- `/` - web dashboard
- `/api/profile/login` - sign in with profile UUID and password
- `/api/profile/logout` - clear the current session
- `/api/profile/save` - create or update a profile
- `/api/profile/status` - load the current profile and dashboard state
- `/api/profile/sync` - trigger a sync or dry run
- `/api/simkl/pin/start` - start SIMKL PIN auth
- `/api/simkl/pin/check` - poll SIMKL PIN auth
- `/api/trakt/device/start` - start Trakt device auth
- `/api/trakt/device/check` - poll Trakt device auth
- `/api/trakt/catalogs` - load liked or discovered Trakt lists
- `/api/mdblist/lists` - load MDBList account lists

## Environment Variables

These matter mainly for CLI use, Docker overrides, and production hosting.

### Source credentials

| Variable | Required | Description |
|---|---|---|
| `SIMKL_CLIENT_ID` | CLI: Yes | SIMKL app client ID |
| `SIMKL_CLIENT_SECRET` | No | SIMKL app client secret |
| `SIMKL_ACCESS_TOKEN` | CLI: Yes | SIMKL access token from `python main.py auth` |
| `ANILIST_USERNAME` | No | AniList username |
| `ANILIST_ACCESS_TOKEN` | No | Needed only for private AniList lists |
| `TRAKT_CLIENT_ID` | No | Trakt app client ID |
| `TRAKT_CLIENT_SECRET` | No | Used for Trakt device auth |
| `TRAKT_ACCESS_TOKEN` | No | Trakt access token |
| `TRAKT_REFRESH_TOKEN` | No | Trakt refresh token |
| `MDBLIST_API_KEY` | No | MDBList API key |
| `PMDB_API_KEY` | CLI: Yes | PublicMetaDB API key |

### Sync options

| Variable | Required | Description |
|---|---|---|
| `SYNC_REMOVE_MISSING` | No | Remove stale items from PublicMetaDB lists |
| `SYNC_DELETE_DISABLED_LISTS` | No | Delete SyncMeta-managed lists that are no longer selected |
| `SYNC_DRY_RUN` | No | Preview changes without writing |
| `SYNC_INTERVAL_MINUTES` | No | CLI repeat interval in minutes |
| `SYNC_MEDIA_TYPES` | No | Comma-separated media types such as `shows,movies,anime` |

### Web app options

| Variable | Required | Description |
|---|---|---|
| `PROFILE_STORE_FILE` | No | Path to the JSON profile store |
| `DISABLE_PROFILE_SCHEDULER` | No | Set to `1` to disable background scheduling |
| `SYNCMETA_MASTER_KEY` | No | Optional Fernet key for encrypting stored credentials |
| `SYNCMETA_MASTER_KEY_FILE` | No | Optional path to the encryption key file |
| `SYNCMETA_SESSION_TTL_SECONDS` | No | Session lifetime for signed-in browsers |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | No | Max login attempts per client inside the throttle window |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | No | Throttle window for login attempts |
| `SITE_ACCESS_PASSWORD` | No | Shared password that gates the whole site before the app loads |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | No | Max site-access password attempts per client inside the throttle window |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | No | Throttle window for site-access password attempts |

## Project Structure

```text
main.py
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

Run the test suite:

```bash
python -m unittest discover -v
```

Notes:

- PublicMetaDB requests use retry logic and rate limiting
- dry runs are recorded in history but do not advance the automatic schedule
- the scheduler checks for due profiles in the web process and starts sync jobs in background threads

## Current Limits

- Automatic syncing depends on the web process staying alive
- The scheduler is designed around a single active web worker
- A VPS admin can still theoretically extract secrets from a live server, even though SyncMeta now hides them from the browser and encrypts them at rest

## License

See `LICENSE`.
