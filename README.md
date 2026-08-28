# Pullbox

Modern comic book management and acquisition for self-hosted libraries.

Pullbox helps discover, download, organize, and catalog digital comic
collections. It follows the familiar self-hosted media-app model: connect
metadata, indexers, download clients, and a library path, then let the app keep
wanted issues moving through search, download, post-processing, and library
organization.


## Quick Start With Docker

Pullbox listens on port `8585` and uses four main container paths:

| Path | Purpose |
| --- | --- |
| `/data` | Pullbox state: database, config, logs, backups, temp files |
| `/comics` | Comic library |
| `/downloads` | Completed downloads shared with download clients |
| `/imports` | Manual import/drop-folder sources, including Mylar3 databases |
| `/imports/remote-drop` | Recommended folder for manual folder-import staging |


### Pre-Run Setup

Pullbox was created with maximum application / image security in mind and
is implemented using a Docker Hardened Image. Based on this, there are a few
requirements that must be met prior to spinning up the container.

First, you must create or choose host folders for Pullbox data, your comics library, completed
downloads, and import sources. The `/data` folder should be durable local
storage because it contains SQLite, `config.xml`, logs, and backups.

If the above folders do not exist create them:

```bash
sudo mkdir -p /path/to/pullbox-appdata /path/to/comics /path/to/shared-downloads /path/to/imports/remote-drop
```

Pullbox-created files and folders are owned by the hardened-image runtime
UID/GID `65532:65532`. On Linux hosts, create a matching host group and add
your user to it before first startup so you can browse Pullbox-created files
from the host:

```bash
if ! getent group 65532 >/dev/null; then
  sudo groupadd --gid 65532 pullbox-runtime
fi
sudo usermod -aG "$(getent group 65532 | cut -d: -f1)" "$USER"
```

Log out and back in after `usermod` so the new group membership is active.

Once created or chosen, make each folder writable by the runtime UID/GID
`65532:65532`. On Linux hosts with ACL support, this avoids changing ownership
of existing media:

```bash
sudo setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/pullbox-appdata
sudo setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/comics
sudo setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/shared-downloads
sudo setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/imports
sudo setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/imports/remote-drop
```

For dedicated Pullbox-only folders, ownership is also acceptable:

```bash
sudo chown -R 65532:65532 /path/to/pullbox-appdata /path/to/comics /path/to/shared-downloads /path/to/imports
```


### Docker Run

```bash
docker run -d \
  --name pullbox \
  --restart unless-stopped \
  -p 8585:8585 \
  -e TZ=America/New_York \
  -e PULLBOX_DB_URL=sqlite+aiosqlite:////data/pullbox.db \
  -e PULLBOX_SQLITE_JOURNAL_MODE=WAL \
  -e PULLBOX_LIBRARY_ROOT=/comics \
  -e PULLBOX_COVERS_DIR=/comics/.covers \
  -v /path/to/pullbox-appdata:/data \
  -v /path/to/comics:/comics \
  -v /path/to/shared-downloads:/downloads \
  -v /path/to/imports:/imports \
  ghcr.io/pullboxapp/pullbox:latest
```

Open `http://localhost:8585` and complete first-run setup.

For manual folder imports, place files under your host import staging folder
such as `/path/to/imports/remote-drop`, then select `/imports/remote-drop`
inside Pullbox.


### Docker Compose

If you do not have a shared Docker Compose `.env` file, copy the production
.env example and edit the paths:

```bash
cp docker/.env.example .env
```

If you already have a shared Docker Compose `.env` file, add these variables to
that existing file instead of creating a new one:

```env
TZ=America/New_York
PULLBOX_HOST_PORT=8585
PULLBOX_DATA_PATH=/path/to/pullbox-appdata
COMICS_PATH=/path/to/comics
DOWNLOADS_PATH=/path/to/shared-downloads
IMPORTS_PATH=/path/to/imports
IMPORTS_DROP_PATH=/path/to/imports/remote-drop
```

Use the below example to create a Docker Compose file:

```yaml
services:
  pullbox:
    image: ghcr.io/pullboxapp/pullbox:latest
    container_name: pullbox
    restart: unless-stopped
    ports:
      - "${PULLBOX_HOST_PORT}:8585"
    environment:
      TZ: ${TZ}
      PULLBOX_DB_URL: sqlite+aiosqlite:////data/pullbox.db
      PULLBOX_SQLITE_JOURNAL_MODE: WAL
      PULLBOX_LIBRARY_ROOT: /comics
      PULLBOX_COVERS_DIR: /comics/.covers
    volumes:
      - ${PULLBOX_DATA_PATH}:/data
      - ${COMICS_PATH}:/comics
      - ${DOWNLOADS_PATH}:/downloads
      - ${IMPORTS_PATH}:/imports
```

To start pullbox:

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
```

Open `http://localhost:8585` and complete first-run setup.


### Docker Environment Variables

The Docker run and compose examples above are intentionally minimal. For most
installs, configure the rest of Pullbox in the web UI. If you want a setting to
be managed by the container environment, add the value to your `.env` file and
pass it through under the service `environment:` block. For `docker run`, add
the same value with `-e`.

Environment-managed values can override UI/database settings and may appear
read-only or runtime-managed inside Pullbox.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | host/system default | Container timezone used for logs and displayed times. |
| `PULLBOX_IMAGE` | `ghcr.io/pullboxapp/pullbox:latest` | Compose helper for selecting the image tag. |
| `PULLBOX_HOST_PORT` | `8585` | Compose helper for the host-side published port. |
| `PULLBOX_DATA_PATH` | none | Compose helper for the host appdata folder mounted at `/data`. |
| `COMICS_PATH` | none | Compose helper for the host comic library mounted at `/comics`. |
| `DOWNLOADS_PATH` | none | Compose helper for completed downloads mounted at `/downloads`. |
| `IMPORTS_PATH` | none | Compose helper for manual import/drop folders mounted at `/imports`. |
| `IMPORTS_DROP_PATH` | none | Documentation helper for the recommended folder-import staging path `/imports/remote-drop`; not read by Pullbox. |
| `PULLBOX_RUNTIME_UID` | `65532` | Documentation helper for host ACL/permission commands; not read by Pullbox. |
| `PULLBOX_RUNTIME_GID` | `65532` | Documentation helper for host ACL/permission commands; not read by Pullbox. |
| `PULLBOX_DB_URL` | `sqlite+aiosqlite:////data/pullbox.db` | Database URL. SQLite under `/data` is the supported Docker default. |
| `PULLBOX_SQLITE_JOURNAL_MODE` | `WAL` | SQLite journal mode. Keep `WAL` for normal Docker installs. |
| `PULLBOX_DATA_DIR` | `/data` | Runtime data directory containing `config.xml` and app state. |
| `PULLBOX_LIBRARY_ROOT` | `/comics` | Default library root used during first-run/runtime resolution. |
| `PULLBOX_COVERS_DIR` | `/comics/.covers` | Cover cache directory. |
| `PULLBOX_LOGS_DIR` | `/data/logs` | Log directory. |
| `PULLBOX_TEMP_DIR` | `/data/tmp` | Temporary working directory. |
| `PULLBOX_BACKUP_DIR` | `/data/backups` | Database backup directory. |
| `PULLBOX_BIND_ADDRESS` | `0.0.0.0` | Interface Pullbox binds inside the container. |
| `PULLBOX_AIRDCPP_ENABLED` | `false` | Enables the experimental AirDC++ settings, search, queue, and import integration. |
| `PULLBOX_PORT` | `8585` | Internal listener port. If changed, update the container-side port mapping too. |
| `PULLBOX_BASE_URL` | `http://localhost:8585` | Public URL used in generated app links and startup output. |
| `PULLBOX_INSTANCE_NAME` | `Pullbox` | Display name for the instance. |
| `PULLBOX_SECRET_KEY` | unset | Optional secret override. Leave unset unless you intentionally manage the secret outside `/data/config.xml`. |
| `PULLBOX_SESSION_LIFETIME_HOURS` | `24` | Login session lifetime. |
| `PULLBOX_TRUSTED_PROXIES` | unset | Comma-separated trusted reverse-proxy IPs. |
| `PULLBOX_LOCAL_ADDRESSES` | unset | Legacy bootstrap value; prefer the Security settings UI. |
| `PULLBOX_HTTPS_ENABLED` | `false` | Enables native HTTPS on the normal Pullbox port. |
| `PULLBOX_HTTPS_CERT_PATH` | unset | Container path to the TLS certificate file. |
| `PULLBOX_HTTPS_KEY_PATH` | unset | Container path to the TLS private key file. |
| `PULLBOX_HTTPS_CERT_ROOT` | `/config/certs` | Directory allowed for HTTPS cert/key browsing and validation. |
| `PULLBOX_LOG_LEVEL` | `INFO` | Runtime log level. |
| `PULLBOX_LOG_SIZE_LIMIT_MB` | `1` | Startup log rotation size in MB. |
| `PULLBOX_LOG_BACKUP_COUNT` | `5` | Number of rotated startup logs to keep. |
| `PULLBOX_DEBUG` | `false` | Enables debug behavior and more detailed error output. |
| `PULLBOX_STARTUP_UPDATE_CHECK_ENABLED` | `true` | Enables the startup update check. |
| `PULLBOX_READER_ENABLED` | `true` | Default-on emergency gate for the embedded comic reader. `false` hides Read and disables its private APIs without deleting comics or resume state. |
| `PULLBOX_READER_CACHE_MAX_MB` | `512` | Maximum generated reader-page cache size. |
| `PULLBOX_READER_OPEN_SOURCE_CACHE_SIZE` | `8` | Maximum in-process archive indexes retained for reuse. |
| `PULLBOX_READER_WORKER_COUNT` | `2` | Maximum concurrent archive/image/PDF worker operations. |
| `PULLBOX_READER_WORKER_WAIT_SECONDS` | `2.0` | Maximum wait for a reader worker before returning a retryable busy response. |
| `PULLBOX_READER_MAX_RENDITION_WIDTH` | `2560` | Maximum delivered image width used to bound browser decode memory. |
| `PULLBOX_READER_MAX_RENDITION_HEIGHT` | `4096` | Maximum delivered image height used to bound browser decode memory. |
| `PULLBOX_RATE_LIMIT_ENABLED` | `true` | Enables request rate limiting. |
| `PULLBOX_RATE_LIMIT_TIER1` | `60` | Expensive-operation requests per minute per IP. |
| `PULLBOX_RATE_LIMIT_TIER2` | `120` | Write-operation requests per minute per IP. |
| `PULLBOX_RATE_LIMIT_TIER3` | `300` | Read-operation requests per minute per IP. |
| `PULLBOX_COMICVINE_API_KEY` | unset | Optional ComicVine API key bootstrap value. The UI is preferred after setup. |
| `PULLBOX_COMICVINE_RATE_LIMIT` | `200` | ComicVine request budget per hour. |
| `PULLBOX_DATA_API_BASE_URL` | `https://api.pullbox.app` | Pullbox Data API base URL. Leave default unless testing a private deployment. |
| `PULLBOX_METADATA_REFRESH_DAYS` | `30` | Metadata refresh age threshold. |
| `PULLBOX_SEARCH_INTERVAL_HOURS` | `6` | Automatic wanted-search scheduler cadence. |
| `PULLBOX_SCAN_INTERVAL_HOURS` | `24` | Library scan scheduler cadence. |
| `PULLBOX_DOWNLOAD_POLL_SECONDS` | `3` | Active download polling cadence. |
| `PULLBOX_PROCESS_COMPLETED_INTERVAL_SECONDS` | `300` | Completed download post-processing cadence. |
| `PULLBOX_BACKUP_INTERVAL_DAYS` | `7` | Automatic database backup cadence. |
| `PULLBOX_BACKUP_RETENTION_DAYS` | `28` | Automatic database backup retention. |
| `PULLBOX_HISTORY_RETENTION_DAYS` | `90` | General history cleanup retention. |
| `PULLBOX_HEALTH_CHECK_INTERVAL_MINUTES` | `5` | Main health check scheduler cadence. |
| `PULLBOX_HEALTH_SCHEDULER_INTERVAL_MINUTES` | `30` | Scheduler health check cadence. |
| `PULLBOX_HEALTH_DATABASE_INTERVAL_MINUTES` | `15` | Database health check cadence. |
| `PULLBOX_HEALTH_FILESYSTEM_INTERVAL_MINUTES` | `15` | Filesystem health check cadence. |
| `PULLBOX_HEALTH_SYSTEM_INTERVAL_MINUTES` | `15` | System resource health check cadence. |
| `PULLBOX_HEALTH_DOWNLOAD_CLIENTS_INTERVAL_HOURS` | `4` | Download-client health check cadence. |
| `PULLBOX_HEALTH_INDEXERS_INTERVAL_HOURS` | `8` | Indexer health check cadence. |
| `PULLBOX_HEALTH_COMICVINE_INTERVAL_HOURS` | `8` | ComicVine health check cadence. |
| `PULLBOX_HEALTH_HISTORY_RETENTION_DAYS` | `1` | Health history retention. |
| `PULLBOX_NAMING_SERIES_FORMAT` | `{series} ({year})` | Bootstrap default for series folder naming. UI settings are preferred after setup. |
| `PULLBOX_NAMING_ISSUE_FORMAT` | `{series} ({year}) #{issue:03d}` | Bootstrap default for issue file naming. UI settings are preferred after setup. |
| `PULLBOX_IMPORT_FILE_WORKER_COUNT` | `2` | Number of files processed concurrently during Step 4 imports. Use `1` for fully serial imports on slow or fragile storage. |
| `PULLBOX_IMPORT_DEBUG_SLOW_MODE` | `false` | Troubleshooting-only import slow mode. |
| `PULLBOX_IMPORT_DEBUG_PHASE_DELAY_SECONDS` | `1.25` | Troubleshooting-only import phase delay. |
| `PULLBOX_IMPORT_DEBUG_ITEM_DELAY_SECONDS` | `0.4` | Troubleshooting-only import item delay. |


## Features

- Comic series and issue management.
- ComicVine metadata integration.
- Newznab, Torznab, Prowlarr, and Jackett indexer support.
- SABnzbd, NZBGet, qBittorrent, Transmission, and Deluge download clients.
- Manual and automated wanted-issue search.
- Search history, rejected results, and blocklist support.
- Post-processing for completed downloads.
- Library scanning, matching, renaming, conversion, and integrity utilities.
- Responsive single-page comic reader for CBZ, CBR, CB7, CBT, and PDF files.
- Intervention queue for ambiguous matches.
- Health checks, diagnostics, logs, backups, and audit trail.
- Server-rendered UI with HTMX, Alpine.js, and Tailwind CSS.


## Development

Contributor setup, validation commands, coding standards, and workflow details
live in the repo docs:

- `CONTRIBUTING.md`
- `docs/development/ARCHITECTURE_OVERVIEW.md`
- `docs/development/CODE_STANDARDS.md`
- `docs/development/DATABASE_STANDARDS.md`
- `docs/development/SECURITY_STANDARDS.md`
- `docs/development/INFRASTRUCTURE.md`
- `docs/development/DESIGN_SYSTEM.md`
- `docs/features/airdcpp.md`
- `docs/features/comic-reader.md`


## Security

Private vulnerability reporting and deployment security guidance are covered in
`SECURITY.md`.


## License

Pullbox is licensed under GPL-3.0-or-later. See `LICENSE` for details.
