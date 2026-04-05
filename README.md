# SyncMeta

SyncMeta is a self-hosted web app that syncs selected lists from:

- SIMKL
- AniList
- Trakt
- MDBList

into [PublicMetaDB](https://publicmetadb.com/api-docs).

It is built for the web UI first. Users save a profile, choose what should sync, and let the server keep those lists updated.

## What It Does

- Syncs selected source lists into PublicMetaDB
- Lets each user keep their own profile with UUID + password
- Stores source credentials securely on the server for background sync
- Supports private/public visibility per source group
- Can remove items that no longer exist in the source
- Can delete SyncMeta-managed PMDB lists when you disable them
- Can sync Trakt watch history manually
- Can sync Trakt resume progress manually

## Supported Sources

### SIMKL

- Movies, shows, and anime
- Status-based selection
- PIN auth in the web UI

### AniList

- Anime lists
- Public username sync
- Optional token for private lists

### Trakt

- Watchlist
- Default catalogs
- Liked lists
- Public discover lists
- Device auth in the web UI
- Manual watch history sync
- Manual resume progress sync

### MDBList

- Account lists
- Per-list selection in the web UI

## Quick Start

### 1. Start the app

```bash
docker compose up -d --build web
```

Then open:

- `http://127.0.0.1:8080`

### 2. Create or load a profile

In the web UI:

- create a profile UUID
- choose a password
- save the profile

That profile stores your sync setup, history, and encrypted source credentials.

### 3. Add your source accounts

Enter the credentials you want to use:

- PublicMetaDB API key
- SIMKL app + token
- AniList username or token
- Trakt app + device auth
- MDBList API key

### 4. Choose what should sync

Examples:

- SIMKL `Watching` shows
- AniList `Planning`
- Trakt watchlist
- selected MDBList lists

### 5. Run sync

Use the dashboard buttons to:

- sync lists
- dry run lists
- sync watch history
- sync resume progress

## Docker Setup

The included `docker-compose.yml` is the main supported setup:

```yaml
services:
  web:
    build: .
    ports:
      - "8080:8080"
    restart: unless-stopped
    environment:
      PROFILE_STORE_FILE: /app/data/profiles.json
      SYNCMETA_MASTER_KEY: ${SYNCMETA_MASTER_KEY:-}
    volumes:
      - ./data:/app/data
```

What this does:

- runs the web app with Gunicorn
- exposes port `8080`
- stores data in `./data`
- keeps one worker, which matches the internal scheduler design

Stop it with:

```bash
docker compose down
```

## Important Data Files

To keep user data across updates, keep these intact:

- `./data/profiles.json`
- `./data/profiles.key` if you are not using `SYNCMETA_MASTER_KEY`

If those stay the same, profiles and saved credentials survive rebuilds.

## Safe Updates

Normal update flow:

```bash
docker compose up -d --build web
```

If you move to a new host, make sure you also keep:

- the `data` folder
- the same `SYNCMETA_MASTER_KEY`, if you use one

## Optional `.env`

You do not need a `.env` file for normal dashboard use.

Most users only need it for deployment settings like:

- `SYNCMETA_MASTER_KEY`
- `SITE_ACCESS_PASSWORD`
- session or throttle tuning

Start from:

```bash
cp .env.example .env
```

## Optional Environment Variables

| Variable | What it does |
|---|---|
| `PROFILE_STORE_FILE` | Path to the profile database JSON |
| `SYNCMETA_MASTER_KEY` | Encryption key for saved credentials |
| `SYNCMETA_MASTER_KEY_FILE` | Custom path for the generated key file |
| `SITE_ACCESS_PASSWORD` | Optional shared password before the app loads |
| `DISABLE_PROFILE_SCHEDULER` | Turns off automatic list syncing |
| `SYNCMETA_SESSION_TTL_SECONDS` | Browser session lifetime |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | Login rate-limit max attempts |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | Login rate-limit window |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | Site-access max attempts |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | Site-access rate-limit window |

## Security

SyncMeta currently does this:

- hashes profile passwords
- encrypts saved source credentials at rest
- uses server-side sessions
- does not send saved raw secrets back to the browser
- rate-limits login and site-access attempts

Secret fields are overwrite-only. If the UI says a secret is already stored, leaving the field blank keeps the existing value.

## Sync Behavior

### Lists

List sync can:

- add new items
- remove missing items
- delete disabled SyncMeta-managed PMDB lists if you enable that option

SyncMeta uses clean list names like:

- `Watching - Series`
- `Plan to Watch - Movies`
- `Planning - Anime`
- `Recommended Movies`

If two different sources would create the same visible PMDB list name, SyncMeta separates them automatically so they do not overwrite each other.

### Watch History

Trakt watch history is manual-only.

It is designed to:

- import new watched entries
- avoid inflating watch counts to `x2`, `x3`, and higher by mistake
- clear PMDB watch history if you use the dashboard clear action

### Resume Progress

Resume sync is also manual-only.

It only updates changed progress entries instead of resending the same progress every run.

## Source Notes

- SIMKL app setup may ask for a redirect URL, but SyncMeta uses PIN auth in the UI.
- Trakt app setup may ask for a redirect URL, but SyncMeta uses device auth in the UI.
- AniList only needs a token for private lists.
- MDBList uses an API key from your MDBList account.
- PublicMetaDB needs your API key from [publicmetadb.com/api-docs](https://publicmetadb.com/api-docs).

## Main API Endpoints

- `/` - dashboard
- `/api/profile/login` - sign in
- `/api/profile/logout` - sign out
- `/api/profile/save` - create or update a profile
- `/api/profile/status` - current dashboard state
- `/api/profile/sync` - sync lists or run a dry run
- `/api/profile/sync/stop` - stop a running sync
- `/api/profile/list/delete` - delete a synced PMDB list and unselect it
- `/api/profile/activity/history/sync` - run watch history sync
- `/api/profile/activity/history/clear` - clear PMDB watch history
- `/api/profile/activity/resume/sync` - run resume sync
- `/api/simkl/pin/start` - start SIMKL PIN auth
- `/api/simkl/pin/check` - poll SIMKL PIN auth
- `/api/trakt/device/start` - start Trakt device auth
- `/api/trakt/device/check` - poll Trakt device auth
- `/api/trakt/catalogs` - load Trakt lists
- `/api/mdblist/lists` - load MDBList lists

## Local Run Without Docker

If you want to run it directly:

```bash
pip install -r requirements.txt
python web.py
```

Then open:

- `http://127.0.0.1:8080`

## Development

Run tests:

```bash
python -m unittest discover -v
```

## Project Structure

```text
web.py
src/
templates/
tests/
docker-compose.yml
Dockerfile
requirements.txt
```

## Notes

- Automatic background sync only applies to list syncing
- Watch history and resume progress are manual-only
- The scheduler runs in the web process
- The app is designed around one active web worker

## License

See `LICENSE`.
