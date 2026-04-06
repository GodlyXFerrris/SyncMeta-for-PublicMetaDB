# SyncMeta

SyncMeta is a self-hosted web app that syncs selected content from:

- SIMKL
- AniList
- Trakt
- MDBList

into [PublicMetaDB](https://publicmetadb.com/api-docs).

The project is built around the web UI, persistent profiles, and Docker-first deployment. Each user saves a profile, connects their own source accounts, chooses what should sync, and lets the server keep those lists updated in the background.

## What SyncMeta Does

- Syncs selected lists into PublicMetaDB
- Uses one profile per user with UUID + password
- Stores source credentials securely on the server for background list sync
- Supports private/public visibility per source group
- Can remove items that no longer exist in the source
- Can delete SyncMeta-managed PMDB lists when you disable them
- Supports manual watch history import
- Supports manual resume progress import

## Supported Sources

### SIMKL

- Movies, shows, and anime lists
- Status-based list selection
- PIN auth in the web UI
- Manual watch history import

### AniList

- Anime list sync
- Public username sync
- Optional token for private lists

### Trakt

- Watchlist
- Default catalogs
- Liked lists
- Public discover lists
- Device auth in the web UI
- Manual watch history import
- Manual resume progress import

### MDBList

- My Lists from your account
- Public list search in the web UI
- Per-list selection in the web UI

## Docker Setup

The included `docker-compose.yml` is the main supported deployment path.

Example shape:

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

- Runs the web app
- Exposes port `8080`
- Stores profile data in `./data`
- Keeps the scheduler inside the web process

Stop it with:

```bash
docker compose down
```

## Quick Start

### 1. Start SyncMeta

```bash
docker compose up -d --build web
```

Then open:

- `http://127.0.0.1:8080`

### 2. Create or load a profile

In the web UI:

- Create a profile UUID
- Choose a password
- Save the profile

That profile stores:

- Source settings
- Selected lists
- Activity sync settings
- Encrypted source credentials

### 3. Add your accounts

Depending on what you want to use, enter:

- PublicMetaDB API key
- SIMKL app credentials + token
- AniList username or token
- Trakt app credentials + device auth
- MDBList API key

### 4. Choose what should sync

Examples:

- SIMKL `Watching` series
- SIMKL `Plan to Watch` anime
- AniList `Planning`
- Trakt watchlist
- Selected MDBList lists
- Public MDBList lists from Discover

### 5. Run the dashboard actions

The dashboard has separate actions for:

- `Sync Lists`
- `Dry Run Lists`
- `Sync Watch History`
- `Sync Resume Progress`

## Persistent Data

To keep user data across rebuilds and updates, keep these intact:

- `./data/profiles.json`
- `./data/profiles.key` if you are not using `SYNCMETA_MASTER_KEY`

If you rebuild the container but keep the same data and encryption key, user profiles and saved credentials stay usable.

## Safe Updates

Normal update flow:

```bash
docker compose up -d --build web
```

If you move hosts, keep:

- the `data` folder
- the same `SYNCMETA_MASTER_KEY` if you use one

## Optional `.env`

You do not need a `.env` file for normal dashboard use.

Most people only need it for deployment overrides like:

- `SYNCMETA_MASTER_KEY`
- `SITE_ACCESS_PASSWORD`
- session or rate-limit tuning

If you want one:

```bash
cp .env.example .env
```

## Optional Environment Variables

| Variable | Purpose |
|---|---|
| `PROFILE_STORE_FILE` | Path to the profile JSON store |
| `SYNCMETA_MASTER_KEY` | Encryption key for stored credentials |
| `SYNCMETA_MASTER_KEY_FILE` | Custom path for the generated key file |
| `SITE_ACCESS_PASSWORD` | Optional shared password before the app loads |
| `DISABLE_PROFILE_SCHEDULER` | Disables automatic list syncing |
| `SYNCMETA_SESSION_TTL_SECONDS` | Browser session lifetime |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | Login rate-limit max attempts |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | Login rate-limit window |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | Site-access max attempts |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | Site-access rate-limit window |

## Security

SyncMeta currently:

- hashes profile passwords
- encrypts stored source credentials at rest
- uses server-side sessions
- does not send saved raw secrets back to the browser
- rate-limits login and site-access attempts

Secret fields are overwrite-only. If the UI says a secret is already stored, leaving the input blank keeps the existing value.

## How Syncing Works

### List Sync

List sync can:

- add new items
- remove items that no longer exist in the source
- Delete SyncMeta-managed PMDB lists if you disable them and enable that option

List names stay clean, for example:

- `Watching - Series`
- `Plan to Watch - Movies`
- `Watching - Anime`
- `Planning - Anime`
- `Recommended Movies`

If two sources would create the same visible PMDB list name, SyncMeta separates them so they do not overwrite each other.

### Watch History

Watch history is manual-only.

Current activity sources:

- SIMKL watch history
- Trakt watch history

The selected history source runs when you press `Sync Watch History`.

### Resume Progress

Resume progress is manual-only.

Current resume source:

- Trakt only

The selected resume source runs when you press `Sync Resume Progress`.

## Source Notes

- SIMKL app setup may ask for a redirect URL, but SyncMeta uses PIN auth in the UI.
- AniList only needs a token for private lists.
- Trakt app setup may ask for a redirect URL, but SyncMeta uses device auth in the UI.
- MDBList uses an API key from your MDBList account for both My Lists and public-list search.
- PublicMetaDB needs your API key from [publicmetadb.com/api-docs](https://publicmetadb.com/api-docs).

## Main Routes

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
- `/api/trakt/catalogs` - load or search Trakt lists
- `/api/mdblist/lists` - load or search MDBList lists

## Local Run Without Docker

If you want to run it directly:

```bash
pip install -r requirements.txt
python web.py
```

Then open:

- `http://127.0.0.1:8080`

## Development

Run tests with:

```bash
python -m unittest discover -v
```

## Project Layout

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
- Watch history and resume progress are manual actions
- The scheduler runs inside the web process
- The app is designed around one active web worker

## License

See `LICENSE`.
