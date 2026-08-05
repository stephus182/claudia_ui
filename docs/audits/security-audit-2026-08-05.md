# Security Audit — 2026-08-05 (post-dashboard)

**Scope:** everything that changed since the last full audit (`a8bfdf0`, 2026-07-25) —
33 files, ~13,900 insertions. Five modules had **never** been security-audited:
`dashboard_data.py`, `dashboard_poller.py`, `panel_dashboard.py`, `proposal_tools.py`,
`install_check.py`. Plus a re-verification of every invariant `SECURITY.md` asserts.

**Why now:** the live dashboard (shipped 2026-08-04) is the largest surface added since
the Panel migration and the first one that renders live account data into every session
with no user action. It is also the first significant consumer of `IBKRClient` outside
`ClaudeToolkit`. `SECURITY.md` did not mention it at all.

**Result:** 0 High, 0 Medium, 2 Low — both fixed and regression-tested. 5 documentation
accuracy defects in `SECURITY.md`, all corrected. Gates green: **1,049 tests**, ruff, mypy.

Every finding was verified first-hand against running state, on-disk file modes, or
installed source. Two suspected findings were **withdrawn** under verification (below).

---

## L-1 — Session reports written world-readable (0644)

`claudia/session_reporter.py` wrote each report with `path.write_text(...)` and no
`chmod`. `Path.write_text` honours the process umask, so all **45** reports in
`data/test-sessions/` were `-rw-r--r--`.

The contents are account data, not metadata. A sampled report carried a complete
`[trade_proposed]` decision line: side, symbol, quantity, limit price, order type and TIF,
the live market price the limit was judged against, and the rationale text — everything
needed to reconstruct what was traded and at what level.

*(The verbatim excerpt that stood here was removed before this file was committed. This
repo is public, and no other document in `docs/audits/` carries account figures — quoting
a real proposal to illustrate a finding about account data being too readable would have
published the very thing the finding is about. The shape is the evidence; the values add
nothing to it.)*

This is the app violating a control it already documents: `SECURITY.md` §12 requires
*"any new file written by the app that contains private-document or account content is
`chmod 0600` immediately after the write"*. That checklist item was added in the
2026-07-25 audit for the `docs/versions/` snapshots and never applied to this writer.

**Not a git exposure** — `data/` is wholesale-gitignored and
`test_no_tracked_but_ignored_files` is green. The exposure is local: any other account on
the machine, and any process not running as this user, could read them.

**Fix:** unconditional `path.chmod(0o600)` after the write. Unconditional matters —
`write_text` does not alter an existing file's mode, so a create-only chmod would leave
every pre-existing report at 0644 forever. The 45 existing reports were corrected on disk.

**Regression:** `test_session_report_is_chmod_600`.

## L-2 — Flex validation record written world-readable (0644)

Same defect class, newer code. `claudia/flex_sync.py:_write_record()` writes
`store.db.validation.json` beside the trade store; the live file at
`~/.ibkr_core/store.db.validation.json` was `-rw-r--r--`. It holds the dataset's row
counts, the newest trade date, and the realised-P&L total the integrity checks were
satisfied by — a summary of the account's trading history.

**Fix:** unconditional `path.chmod(0o600)` after the write; existing file corrected.

**Regression:** `test_flex_validation_record_is_chmod_600` and
`test_flex_validation_record_rewrite_stays_600` — the second exists specifically to pin
the *unconditional* property, and fails if the chmod is ever made create-only.

---

## Documentation defects in `SECURITY.md` (all corrected)

A stale security document is a security problem: it is what the next reader trusts instead
of re-deriving. Five claims had drifted.

| § | Claim | Reality |
|---|---|---|
| 2 | "42 read-only `ClaudeToolkit` tools" | `len(TOOL_DEFINITIONS)` == **44** |
| 2 | Gates fire on `place_order_and_confirm()` | Three gated entry points, not one — cancel and modify have their own |
| 4 | "Raw IBKR API responses never appear in conversation context" | False since the read-back work: `order_flow.py` sends `json.dumps(result, indent=2)` into chat and stores it under `decisions.metadata["ibkr_response"]` |
| 5 | Safety block at `agent.py:57-156` | Actually **58–195** — the block grew by ~40 lines and the citation never moved |
| 6 | "16 curated of a 78-tool full sidecar set" | Sidecar at `55534aa` registers **84** |
| 13 | "46 regression tests" | **50** before this audit, **53** after |

The §4 correction is the one that mattered. The other half of that sentence — that tool
errors are sanitised by `_safe_error()` — is **true**, but the function lives in
`ibkr_core_mcp/claude_tools.py`, not in this repo; a grep of `claudia/` finds nothing and
reads like the control was deleted. Both halves are now stated precisely.

`SECURITY.md` also gained a section for the live dashboard, which it did not describe at
all, and §8's "what a reachable session would expose" was rewritten: it still described
the retired chat opening-status payload, which as of `2829789` no longer renders account
figures. The real answer is now a continuously-repainting account dashboard.

---

## Verified good — no finding

Re-checked first-hand rather than assumed:

- **Hard Rule 1 holds.** No tool definition reaches `place_order`/`modify_order`/
  `cancel_order`/`reply_order`. The only call sites are the three UI-layer cores in
  `order_flow.py`, each reached exclusively by a physical button click.
- **All three write paths are gated.** `place_order_and_confirm` → `place_order` →
  `require_touch_id` + `confirm_order_dialog`; `cancel_order` (`client.py:1275`) runs
  `require_touch_id` + `confirm_cancel_dialog` **inside itself** — the absence of an
  `_and_confirm` suffix on the cancel path is a naming artefact, not a missing gate, and
  was checked precisely because it looked like one; `modify_order_and_confirm` likewise.
- **Both dashboard `Tabulator`s are `disabled=True` with no `on_click`/`on_edit` bound
  anywhere in the module.** The order book is the surface where a click could plausibly be
  wired to "cancel this"; it is not. `header_filters`/`pagination` change what is
  displayed and reach no order path.
- **No SQL injection surface.** Zero f-string/`%`/`.format` SQL across `dashboard_data.py`,
  `flex_sync.py`, `conversation_store.py`; every `execute()` is parameterised or a literal.
- **`dashboard_data.connect()` opens `mode=ro`** — a read-only URI handle on a file the
  Flex sync writes concurrently. A display surface cannot corrupt the trade store.
- **The dashboard reaches no write API.** `get_accounts`, `fetch_ledger`, `fetch_positions`,
  `fetch_orders` only.
- **Decision metadata does not reach the LLM.** `search_past_conversations` searches
  `messages` via FTS5; `decisions` has no search path (its FTS table was dropped as dead
  schema in the 2026-07-03 review), so the raw `ibkr_response` blobs stored there are
  never re-injected into context.
- **SSRF guard intact**, including per-hop redirect re-validation and the decimal/hex
  resolve-then-check.
- **TradingView subprocess env allowlist intact**; no secret inherited.
- **File modes:** `.env`, `docs/context.md`, `docs/principles.md` and all eight
  `docs/versions/*/` snapshots are `0600`.
- **Structural guards green:** loopback bind, no unguarded Markdown pane in the package,
  no tracked-but-ignored file, no private path outside `.gitignore`.

## Withdrawn under verification

- **"`_safe_error()` no longer exists."** A grep scoped to `claudia/` found nothing. It
  exists in `ibkr_core_mcp/claude_tools.py:execute()`, which wraps every handler in
  `try/except` and returns the sanitised string. The §4 claim was imprecise about
  *location*, not wrong about the control.
- **"The cancel path skips the gates."** See above — the gates are inside `cancel_order`.

---

## Outside claudia_ui — RESOLVED in ibkr_core_mcp, same day

Found while checking file modes. These belong to `ibkr_core_mcp`, so they were fixed in
that repo rather than papered over with a `chmod` here — a one-off `chmod` on `store.db`
is undone by the next recreate, which is the whole reason the fix had to be in the code.

| Path | Was | Now | Resolution |
|---|---|---|---|
| `~/.ibkr_core/store.db` | `0644` | `0600` | `SQLiteStore._connect()` now holds it, **self-healing on every connection** |
| `~/.ibkr_core/store.db-wal`, `-shm` | `0644` | `0600` | Same call. The WAL holds committed-but-uncheckpointed rows — same content, so securing the main file alone would have left the newest writes readable |
| `~/.ibkr_core/` | `0755` | `0700` | `SQLiteStore.__init__`. Also covers the Flex XML archive, the Drive OAuth token, and `auth_audit.log` |
| `~/.ibkr_core/credential.json` | `0644` | *deleted* | Dormant OAuth client secret, distinct client from the live one, unreferenced. See below |
| `~/.ibkr_core/auth_audit.log` | `0644` | `0644` | Left as-is deliberately — entries are **templated** (`Place order for {account_id}: {order}`) with no interpolated values, so it discloses timing and approval counts only. Now shielded by the `0700` parent regardless |

**`store.db` was verified untouched.** Baselined before the change — SHA-256, byte size,
`PRAGMA integrity_check`, and a row count for each of the 23 tables — then re-verified
after the repair ran through the real code path: **identical hash, identical size,
integrity ok, and every one of the 23 row counts equal.** The 2026-08-04/05 Flex dataset
work is intact. (Counts deliberately not reproduced here — they are a direct measure of
account activity, and this repo is public.)

Baselining *before* touching anything is the part worth repeating. A hash comparison after
the fact is only evidence if the "before" value was captured before the change, and a
permissions fix on a 53 MB live database is exactly the situation where it is tempting to
skip that step because "a chmod cannot alter content".

**The `SQLiteStore` fix closes a documented-but-unimplemented control**, which is the real
finding. `ibkr_core_mcp/SECURITY.md` had said since its first version that the database
*"should be stored in a user-owned directory with `0o600` permissions"* — and nothing
enforced it. The live file had been `0644` the whole time. That section now describes an
enforced control and names the three properties that must not be simplified away (WAL
sidecars in scope; self-healing rather than create-time-only; never raises). Guarded by
four tests in `tests/test_store.py`, two of which assert against a path deliberately reset
to `0644`/`0755` first — the create path is exercised for free by a temp-dir fixture, so a
test covering only creation would have passed against the unfixed code. One did, and was
rewritten.

**On the deleted credential:** `credential.json` held a Google OAuth **client** secret for
a client distinct from the live one (same GCP project, different `client_id`), dormant
since 2026-05-20, superseded by `credentials_ibkr_core_mcp.json` on 2026-07-10, and
matching neither the configured `GDRIVE_CREDENTIALS_FILE` nor the code default
(`credentials.json`, plural). The 2026-07-10 migration design had already listed its
cleanup as outstanding, so this completes a decision rather than making one.

**Not done, and it is a separate action:** deleting the file removes the local copy but
does **not** revoke the OAuth client, which still exists in the Google Cloud console. If
the intent is that this client can never be used again, revoke it there.

**Also left alone:** `token.json.expired.bak` (already `0600`) is the companion straggler
named in the same 2026-07-10 cleanup line. Not deleted — it was not in scope for this
request and its permissions are already correct.

---

## Audit metadata

- **Baseline:** `a8bfdf0` (2026-07-25 audit) → `bccb286` (HEAD at audit start).
- **Suite:** 1,046 → **1,049** tests (3 added), 3 skipped (`live_api`, opt-in).
- **Regression tests added to** `tests/test_security_regressions.py` under the
  "2026-08-05 audit — L-1/L-2" heading. All three were confirmed to **fail** with the
  chmod calls removed and pass with them restored; a regression test never run red is not
  evidence of anything.
</content>
</invoke>
