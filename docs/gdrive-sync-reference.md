# GDrive Sync Reference

`claudia/gdrive_sync.py` — `GDriveSync` class, auto-enabled when `GOOGLE_DRIVE_FOLDER_ID` is
set. No new env vars required.

## What syncs

| File | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (first session per process, before DB opens). **Freshness guard:** skipped when the local DB (incl. `-wal` mtime) is newer than the Drive copy's `modifiedTime` — an older Drive copy never overwrites a newer local DB. Stale `-wal`/`-shm` sidecars are removed before the downloaded file lands. |
| `claudia.db` | local → Drive | Session stop (after `close_session`). Uploads a **WAL-consistent snapshot** made with `sqlite3.Connection.backup()` — never the live file, so commits still in `claudia.db-wal` are included and concurrent checkpoints can't tear the upload. |
| `context.md` | Drive → memory | Every session start (overrides local file if present on Drive) |
| `principles.md` | Drive → memory | Every session start (overrides local file if present on Drive) |

**Shared credentials, independent implementations:** `GDriveSync` (this file's module) and
`ibkr_core_mcp`'s `GDriveCache` both read `GDRIVE_TOKEN_FILE`/`GDRIVE_CREDENTIALS_FILE` from
the same `Config`/env vars — one Drive OAuth client, shared because both run in the same
process (claudia_ui imports ibkr_core_mcp directly). They are not in a service/client
relationship: each builds its own `googleapiclient.discovery.build("drive", "v3", ...)`
service object. As of 2026-07-10 both delegate the actual credential load/refresh/persist
logic to the shared `ibkr_core_mcp.gdrive_auth` module (see
`docs/plans/2026-07-10-gdrive-auth-dedup-design.md`) — `GDriveCache` additionally
owns the interactive first-time OAuth bootstrap, which `GDriveSync` deliberately does not
have (it raises if no valid token exists, rather than popping a browser mid-chat-session).

## Drive folder layout

```
<GOOGLE_DRIVE_FOLDER_ID>/              ← root ClaudIA folder
  context.md                           ← ClaudIA persona (optional, upload manually)
  principles.md                        ← trading rules (optional, upload manually)
  db/                                  ← GDRIVE_DB_FOLDER_ID (auto-created by GDriveSync)
    claudia.db                         ← conversation history
  market_data/                         ← GDRIVE_CACHE_FOLDER_ID (auto-created by GDriveCache)
    manifest.json                      ← market data index
    AAPL_1D_1Y_2026-01-01.parquet      ← OHLCV cache
    ...
  account_data/                        ← GDRIVE_ACCOUNT_FOLDER_ID (auto-created; ibkr_core_mcp Flex sync)
    ClaudIA_Full_Activity_*.xml        ← manual Flex archive
    flex_U*.xml                        ← auto-synced Flex archive
    store.db                           ← ibkr_core_mcp trade store backup
  web_docs/                            ← GDRIVE_WEB_DOCS_FOLDER_ID (auto-created; firecrawl_crawl/search)
    ...
```

All four subfolders are auto-created on first use. Set `GDRIVE_DB_FOLDER_ID`,
`GDRIVE_CACHE_FOLDER_ID`, `GDRIVE_ACCOUNT_FOLDER_ID`, or `GDRIVE_WEB_DOCS_FOLDER_ID`
explicitly to point to pre-existing folders instead.

## First-time setup on a new machine

1. Create (or reuse) a Google Drive folder for ClaudIA. Get its ID from the URL:
   `drive.google.com/drive/folders/<FOLDER_ID>`
2. Set `GOOGLE_DRIVE_FOLDER_ID=<FOLDER_ID>` in `.env`
3. Start ClaudIA — it downloads `claudia.db` (from the `db/` subfolder) on session start.
   Both `db/` and `market_data/` subfolders are auto-created on first use.
4. To enable Drive context/principles: upload `docs/context.md` and `docs/principles.md`
   to the **root** folder via the Drive web UI (not inside `db/`)

## Hot-reload behavior

Drive texts are fetched once per session start. The watchdog still watches local files —
editing `docs/context.md` while a session runs clears the Drive override and uses the local
file from the next message.

## Error handling

All Drive operations are non-fatal. On any failure (no token, network error, tampered file):

| Operation | On failure |
|---|---|
| `download_db` at start | Log warning; use existing local `claudia.db` |
| `download_db` sees older Drive copy | Log warning; keep newer local DB (freshness guard); it syncs to Drive at session end |
| `upload_db` at stop | Log warning; local copy preserved; syncs next session (freshness guard protects it across a process restart) |
| `read_text` for context/principles | Log warning; fall back to local `docs/` files |
| `ping()` (connectivity poll) | Returns `False`; status light turns red; no exception raised |

**Threading note:** `upload_db` uses `threading.RLock` (reentrant) because `_find_file`
calls `_get_service()`, which also acquires the same lock. A plain `Lock` would deadlock
when `upload_db` is called while a session is active.
