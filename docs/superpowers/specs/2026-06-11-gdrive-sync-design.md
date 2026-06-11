# GDrive Sync Design — claudia.db + context/principles

## Goal

Make ClaudIA portable across machines. Conversation history (`claudia.db`) is downloaded from Google Drive at session start and uploaded at session stop. `context.md` and `principles.md` are read directly from Drive at session start, with local file fallback.

## Architecture

One new file: `claudia/gdrive_sync.py` — a `GDriveSync` class with three operations.

```
claudia/gdrive_sync.py
  GDriveSync(config: Config)
    download_db(local_path: Path) -> bool
      Download claudia.db from Drive to local_path.
      Returns True if found and downloaded; False if not on Drive (first run).
      On error: logs warning, returns False — caller continues with local/empty DB.

    upload_db(local_path: Path) -> None
      Upload local_path to Drive as "claudia.db" (create or update in-place).
      On error: logs warning — local copy is preserved, data not lost.

    read_text(filename: str) -> str | None
      Download a text file (e.g. "context.md") from the Drive folder.
      Returns content string, or None if not found or on error.
```

`GDriveSync` builds its own Drive service using the existing credentials:
- `GDRIVE_TOKEN_FILE` — OAuth2 token
- `GDRIVE_CREDENTIALS_FILE` — OAuth2 client credentials
- `GOOGLE_DRIVE_FOLDER_ID` — Drive folder (same folder used by market data cache)

**Auto-enable:** `GDriveSync` is only instantiated when `GOOGLE_DRIVE_FOLDER_ID` is set. If not set, ClaudIA runs fully local with no behaviour change.

## Data Flow

### Session start (`on_chat_start`)

```
1. Build Config from env
2. If GOOGLE_DRIVE_FOLDER_ID set:
   a. gdrive_sync = GDriveSync(config)
   b. gdrive_sync.download_db(_DB_PATH)   ← pulls claudia.db if it exists on Drive
3. _get_store() opens the local DB as normal
4. If GOOGLE_DRIVE_FOLDER_ID set:
   a. context_text  = gdrive_sync.read_text("context.md")   ← None if not on Drive
   b. principles_text = gdrive_sync.read_text("principles.md")
   c. Pass both to ContextLoader (override local files if Drive versions exist)
```

### Session stop (`on_stop`)

```
1. store.close_session(...)   ← all writes flushed
2. loader.stop_watching()
3. If gdrive_sync configured:
   gdrive_sync.upload_db(_DB_PATH)   ← pushes claudia.db to Drive
```

### context.md / principles.md

Drive is the source of truth at session start. The local `docs/context.md` and `docs/principles.md` remain as fallback (used when Drive is unavailable or files not yet uploaded).

Watchdog hot-reload is unchanged — it still watches the local file path. Since these files are not frequently edited, Drive fetch at session start is sufficient; no polling during a session.

## Files Modified

| File | Change |
|---|---|
| `claudia/gdrive_sync.py` | **New** — `GDriveSync` class |
| `claudia/app.py` | Call `download_db` before `_get_store()`; call `upload_db` in `on_stop`; pass drive text to `ContextLoader` |
| `claudia/context_loader.py` | Accept optional `context_text` / `principles_text` strings to override file reads |
| `CLAUDE.md` | Document Drive sync in architecture section; Drive folder layout |
| `SECURITY.md` | Add Drive sync threat model section: poisoned DB, poisoned context/principles, OAuth token theft, hard guarantees |
| `.env.example` | Add comment to `GOOGLE_DRIVE_FOLDER_ID` noting it enables `claudia.db` sync |

## Error Handling

All Drive operations are non-fatal. ClaudIA degrades gracefully:

| Operation | On failure |
|---|---|
| `download_db` at start | Log warning; continue with existing local DB or empty DB on first run |
| `upload_db` at stop | Log warning; local DB preserved; sync will happen next session |
| `read_text` for context/principles | Log warning; fall back to local `docs/context.md` / `docs/principles.md` |

## Drive Folder Layout

All files share the same folder (`GOOGLE_DRIVE_FOLDER_ID`):

```
<Drive folder>/
  manifest.json           ← market data index (existing, GDriveCache)
  AAPL_1D_1Y_2026-01-01.parquet  ← market data (existing)
  claudia.db              ← conversation history (new)
  context.md              ← ClaudIA persona (new, optional)
  principles.md           ← trading rules (new, optional)
```

No subfolders — keeps it simple. File names are distinct from parquet cache files.

## Security

Drive sync introduces three attack surfaces. All are mitigated without relaxing the existing order execution barriers.

### 1. Poisoned `context.md` / `principles.md` — prompt injection (HIGH)

**Threat:** An attacker with Drive access modifies `principles.md` to weaken trading rules or inject adversarial instructions into the system prompt.

**Mitigations:**
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by anything from Drive. Order execution still requires physical button click + Touch ID + tkinter dialog regardless of what is in `principles.md`.
- On each Drive fetch, compute `SHA-256(context.md + principles.md)` and compare against the hash stored in `claudia.db → sessions.context_hash` from the previous session. If the hash changed, send a visible alert in chat: *"⚠️ Your context.md / principles.md on Drive changed since last session — please verify before continuing."*
- This extends the hash verification already applied to local files (see `context_loader.py`).

### 2. Poisoned `claudia.db` — conversation history injection (MEDIUM)

**Threat:** A malicious actor replaces `claudia.db` on Drive with a crafted file containing fake conversation history designed to influence ClaudIA's responses.

**Mitigations:**
- After downloading, run `PRAGMA integrity_check` on the SQLite file before opening it. A structurally tampered file will fail this check; ClaudIA falls back to an empty local DB and logs a warning.
- Even with poisoned conversation history, no order can be placed without the physical button click + biometric + confirmation dialog. The conversation history injection vector has no path to autonomous order execution.

### 3. Drive OAuth token theft (LOW — scoped blast radius)

**Threat:** Stolen `GDRIVE_TOKEN_FILE` grants Drive access.

**Mitigations:**
- The `drive.file` OAuth scope limits the token to files this app created — it cannot access the rest of the user's Drive.
- `GDRIVE_TOKEN_FILE` is already `chmod 600`.
- The token covers only `claudia.db`, `context.md`, `principles.md`, and market data parquets — no IBKR credentials, no `ANTHROPIC_API_KEY`.

### Hard guarantees unchanged

These two properties hold regardless of what is on Drive:
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by Drive content.
- No order can be placed without physical button click + Touch ID + tkinter dialog.

## What Does NOT Change

- `GDriveCache` in `ibkr_core_mcp` — untouched; market data sync is unchanged
- SQLite schema — `claudia.db` format is identical; Drive is just the transport
- Local file paths — `claudia.db`, `context.md`, `principles.md` still work as before when Drive is not configured
- Hot-reload — watchdog still watches local files; Drive fetch is session-start only
- Security model — `claudia.db` on Drive is scoped to `drive.file` OAuth scope (same as market data); the file is only accessible to the authenticated user

## Configuration

No new env vars. Drive sync is enabled automatically when `GOOGLE_DRIVE_FOLDER_ID` is set.

To upload `context.md` and `principles.md` to Drive for the first time, the user uploads them manually via the Drive web UI into the configured folder. On the next session start, ClaudIA picks them up automatically.
