# GDrive OAuth Client Migration — ibkr_core_mcp

**Date:** 2026-07-10
**Status:** Approved design, pending implementation plan

## Problem

The Google Drive OAuth client currently active for `claudia_ui` + `ibkr_core_mcp` Drive
access (`~/.ibkr_core/credentials.json` / `token.json`, client_id ending `...lj50qt9r...`,
internally referred to as the "ClaudIA_UI" client — configured 2026-06-11, see
`project-gdrive-credentials.md` memory) was created under/for the old `IBKR-mcp` project,
before the local repo was renamed to `ibkr_core_mcp`. It should be replaced with a fresh
OAuth client created specifically for `ibkr_core_mcp`.

This is a credential/config migration only. No code changes in `ibkr_core_mcp` or
`claudia_ui`.

## Current state (verified 2026-07-10)

`~/.ibkr_core/` holds two OAuth credential files, both under the same GCP project
(`project-441aef5d-b2c8-4485-8c2`):

| File | Client ID (partial) | Status |
|---|---|---|
| `credential.json` (singular) | `d3mdcb27...` | Dormant since May 20 2026 — not referenced by any current env var. Pre-existing straggler, **out of scope** for this migration. |
| `credentials.json` | `lj50qt9r...` | **Active** — referenced by `GDRIVE_CREDENTIALS_FILE` default and matches `token.json`'s embedded client_id. This is the "ClaudIA_UI" client being replaced. |
| `token.json.expired.bak` | — | Dormant backup, **out of scope**. |

Both `ibkr_core_mcp/cache.py` (`GDriveCache`) and `claudia/gdrive_sync.py` (`GDriveSync`)
read `GDRIVE_CREDENTIALS_FILE` / `GDRIVE_TOKEN_FILE` independently from `Config` / env vars
— there is one shared OAuth client serving both consumers, no per-package credential
separation today.

`GDriveCache._get_service()` (`ibkr_core_mcp/cache.py`) already contains the first-run
interactive OAuth flow: `InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)`,
triggered automatically whenever `GDRIVE_TOKEN_FILE` doesn't exist or fails to validate.
`GDriveSync` (`claudia_ui`) deliberately does **not** run this flow itself — its docstring
states it requires an existing valid token file, bootstrapped via `GDriveCache` first. This
means no new bootstrap script is needed for this migration; the existing `GDriveCache` flow
is reused as-is.

## Architecture / scope

No structural changes. `Config`, `GDriveCache`, `GDriveSync` all keep their current
interfaces and behavior. This migration only changes:
1. What OAuth client exists in Google Cloud Console.
2. Which credential/token files `GDRIVE_CREDENTIALS_FILE` / `GDRIVE_TOKEN_FILE` point to.

## Plan

### 1. Google Cloud Console (manual, user-driven)

1. In `project-441aef5d-b2c8-4485-8c2` → **APIs & Services → Credentials**, create a new
   **OAuth 2.0 Client ID**, application type **Desktop app**, named `ibkr_core_mcp`.
   (Desktop app type matches the existing `"installed"` key format used by both prior
   credential files, required by `InstalledAppFlow`.)
2. Download the client secret JSON.
3. If the OAuth consent screen is in **Testing** publish status, confirm the user's Google
   account is listed under **Test users** (expected to already be true — same GCP project
   served the prior client).

### 2. Local file + config swap

1. Move the downloaded JSON to `~/.ibkr_core/credentials_ibkr_core_mcp.json`.
2. Update `.env` (in both `claudia_ui` and `ibkr_core_mcp`, if they maintain separate env
   files):
   ```
   GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials_ibkr_core_mcp.json
   GDRIVE_TOKEN_FILE=~/.ibkr_core/token_ibkr_core_mcp.json
   ```
   (New token path — file does not exist yet, forcing first-run auth rather than silently
   reusing an old token.)
3. Trigger bootstrap: run a short one-off script that constructs `Config` and calls
   `GDriveCache`'s service accessor (e.g. list files in the configured root folder). This
   opens a browser consent screen and writes `token_ibkr_core_mcp.json` on completion.

### 3. Verification

| Check | Confirms |
|---|---|
| `GDriveCache` resolves/lists `market_data/` and `account_data/` subfolders | ibkr_core_mcp Drive cache path works under the new client |
| `GDriveSync.download_db()` (or equivalent manual round-trip) | claudia_ui session-start/stop DB sync works |
| `GDriveSync.ping()` via `ConnectivityChecker` | `GET /api/status` Drive light reflects the new client correctly |
| Full `./start-claudia.sh` session start | Real-world smoke test — Drive status green, no auth warnings in logs |

### 4. Cleanup

- Delete `~/.ibkr_core/credentials.json` and `~/.ibkr_core/token.json` (the retired
  "ClaudIA_UI" client) outright — no local archive, per explicit decision.
- Revoke the old OAuth client (`lj50qt9r...`) in GCP Console → Credentials, once
  verification passes.
- `credential.json` (singular, `d3mdcb27...`) and `token.json.expired.bak` are noted but
  explicitly **out of scope** — pre-existing stragglers to be handled separately, not
  touched by this migration.
- Update `project-gdrive-credentials.md` memory and any relevant `docs/` references with
  the new client name (`ibkr_core_mcp`) and file paths.

## Rollback

If the new client fails verification before cleanup step 4 runs, the old
`credentials.json` / `token.json` are still present and `.env` can be reverted to the
original paths with no data loss — nothing is destructive until cleanup explicitly runs.
Once cleanup runs (old files deleted, old client revoked), rollback requires repeating
steps 1–3 against a newly created client.

## Out of scope

- `credential.json` (singular) and `token.json.expired.bak` cleanup.
- Any change to `GOOGLE_DRIVE_FOLDER_ID` or the Drive folder layout itself.
- Any change to OAuth scopes (`https://www.googleapis.com/auth/drive` stays as-is).
- Moving to a different GCP project (explicitly decided against — staying in
  `project-441aef5d-b2c8-4485-8c2`).
