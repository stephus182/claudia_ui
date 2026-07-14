# Shared Google Drive Auth Helper — De-duplication

**Date:** 2026-07-10
**Status:** Approved design, pending implementation plan
**Follows:** `docs/superpowers/specs/2026-07-10-gdrive-oauth-client-migration-design.md` (OAuth client migration, completed same day)

## Problem

During the OAuth client migration, side-by-side review of the two Drive-auth
implementations surfaced real code duplication:

- `ibkr_core_mcp/cache.py` — `GDriveCache._get_service()` (lines 74-113)
- `claudia_ui/claudia/gdrive_sync.py` — `GDriveSync._get_service()` (lines 52-92)

Both independently: load `Credentials.from_authorized_user_file()` from the token
file, check `creds.valid`, refresh via `creds.refresh(Request())` when expired with a
refresh_token, and rewrite the token file using the identical
`os.open(..., os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` + `os.chmod(..., 0o600)`
pattern. This is ~15 lines of identical logic duplicated across two packages — not
"two consumers sharing one client," but two independent reimplementations of the same
mechanism. Risk: a future bug fix or behavior change (e.g. handling a new refresh
error, changing scopes) applied to one file and forgotten in the other.

The two implementations are **not** fully identical: `GDriveCache` additionally owns
the interactive first-time bootstrap (`InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)`),
which `GDriveSync` deliberately does not have — `GDriveSync` raises if no valid token
exists, by design (it must never pop an interactive browser mid-chat-session).

## Goal

Establish `ibkr_core_mcp` as the sole owner of the Drive credential-loading mechanism.
`claudia_ui`'s `GDriveSync` becomes a thin client of that shared logic rather than
reimplementing it. External behavior of both classes is unchanged — this is a pure
internal refactor, verified by full regression + live Drive API checks before
considering it done.

## Design

New module: **`ibkr_core_mcp/gdrive_auth.py`**

```python
def load_or_refresh_credentials(
    token_file: Path, scopes: list[str]
) -> Credentials | None:
    """Load credentials from token_file if present.

    Returns the credentials unchanged if still valid. If expired but refreshable
    (has a refresh_token), refreshes via google.auth.transport.requests.Request,
    persists the refreshed token (see persist_credentials), and returns it. Returns
    None if the file doesn't exist, or if expired with no refresh_token — never
    raises; callers decide what "no usable credentials" means for them.
    """

def persist_credentials(token_file: Path, creds: Credentials) -> None:
    """Write creds.to_json() to token_file with mode 0o600.

    Two-step chmod: os.open's O_CREAT mode only applies on file creation, not to
    an existing file, so os.chmod is called unconditionally afterward.
    """
```

**`GDriveCache._get_service()`** (ibkr_core_mcp/cache.py) becomes:
```python
creds = load_or_refresh_credentials(self._config.gdrive_token_file, _SCOPES)
if creds is None:
    flow = InstalledAppFlow.from_client_secrets_file(
        str(self._config.gdrive_credentials_file), _SCOPES
    )
    creds = flow.run_local_server(port=0)
    persist_credentials(self._config.gdrive_token_file, creds)
# ... existing gdrive_folder_id validation, unchanged ...
self._service = build("drive", "v3", credentials=creds)
```

**`GDriveSync._get_service()`** (claudia_ui/claudia/gdrive_sync.py) becomes:
```python
with self._lock:
    if self._service:
        return self._service
    creds = load_or_refresh_credentials(self._config.gdrive_token_file, _SCOPES)
    if creds is None:
        raise RuntimeError(
            f"GDrive token file not found or invalid: {self._config.gdrive_token_file}. "
            "Authenticate via GDriveCache (ibkr_core_mcp) first."
        )
    self._service = build("drive", "v3", credentials=creds)
    return self._service
```

Both classes keep their own `_service` caching and locking as-is (`GDriveSync`'s
`RLock` stays — it's a legitimate per-consumer concern, not part of the duplicated
auth mechanism).

## Testing

New unit tests for `gdrive_auth.py`, covering all branches of
`load_or_refresh_credentials`:
1. No token file → `None`
2. Valid, non-expired credentials → returned unchanged, no refresh call, no file write
3. Expired + `refresh_token` present → `creds.refresh()` called, `persist_credentials`
   called, refreshed credentials returned
4. Expired + no `refresh_token` → `None`, no write attempted

Plus a test for `persist_credentials`: writes valid JSON, enforces `0o600` regardless
of the file's pre-existing permissions.

Existing `GDriveCache` and `GDriveSync` test suites are updated to assert delegation
(mock `gdrive_auth.load_or_refresh_credentials`) rather than asserting the old inline
refresh/write logic directly. Full regression run of both packages' test suites after
the refactor. Before considering the work done, a live-verification pass mirroring
today's manual checks (real `GDriveCache` folder resolution, real `GDriveSync.ping()`)
confirms no behavior change against the actual `ibkr_core_mcp` OAuth client and
Drive account.

## Phases

1. Write failing tests for `ibkr_core_mcp/gdrive_auth.py`
2. Implement `gdrive_auth.py` to pass those tests
3. Refactor `GDriveCache._get_service()` to delegate; update its tests
4. Refactor `GDriveSync._get_service()` to delegate; update its tests
5. Full regression (both packages) + live verification
6. Documentation pass (see below)

## Documentation pass (phase 6 — after code changes are made and validated, not before)

Stale references found during this session's OAuth migration, to be corrected once the
refactor lands and its own docstrings are final:

| File | What's stale |
|---|---|
| `claudia_ui/.env.example` (lines 38-39) | Shows old default filenames `~/.ibkr_core/token.json` / `credentials.json` — update to reflect the dedicated `ibkr_core_mcp` naming convention |
| `ibkr_core_mcp/CLAUDE.md` (lines 47-48, example `.env` block) | Same old filenames in the example `.env` |
| `ibkr_core_mcp/CLAUDE.md` (lines 510-511, Claude Desktop config JSON example) | Same old filenames |
| `ibkr_core_mcp/CLAUDE.md` (line 51) | "Never commit `.env`, `token.json`, or `credentials.json`" — generalize or update to actual current filenames |
| `ibkr_core_mcp/docs/windows-setup.md` (lines 76-77) | Same old filenames in a setup walkthrough |
| `claudia_ui/docs/connectivity.md` (line 105) | Troubleshooting step says "regenerate `token.json`" — update to the actual current filename |

Plus: new docstrings for `gdrive_auth.py`'s two functions (drafted above, subject to
refinement once implemented), updated docstrings on both refactored `_get_service()`
methods reflecting delegation, and a short architecture note in `claudia_ui/CLAUDE.md`
clarifying that `GDRIVE_TOKEN_FILE`/`GDRIVE_CREDENTIALS_FILE` are read independently by
both `GDriveCache` and `GDriveSync` from the same env vars (same process, no
service/client relationship between the two — see this session's discussion for why
that distinction matters).

Out of scope for this doc pass: historical/dated records (`docs/security-audit-*.md`,
prior design specs) — these are point-in-time snapshots and are not rewritten to match
later changes, per this project's existing documentation convention.

## Out of scope

- Any change to OAuth client, scopes, or credential *files* themselves (that's the
  already-completed migration in the companion spec).
- Merging `GDriveCache` and `GDriveSync` into one class — their domain separation
  (trading data vs. app state) is correct and stays.
- Adding new config env vars for per-package credential separation (explicitly
  declined earlier this session — one shared client is the deliberate choice).
