# Security Audit — 2026-07-25 (post-Panel-migration)

**Scope:** all 18 `claudia/` modules, `SECURITY.md`, and git hygiene. Not a diff — the
working tree was clean at HEAD `c443aea`.

**Why now:** the post-migration cleanup audit
(`2026-07-24-post-migration-cleanup.md`) purged Chainlit residue from code but
**explicitly deferred `SECURITY.md`**. That deferral mattered more than a docs task:
`SECURITY.md` records ClaudIA's network and rendering posture, and the Chainlit→Panel
migration silently changed both. Seven of the modules audited here
(`panel_app`, `panel_sink`, `panel_order_flow`, `panel_pinescript`, `panel_chart`,
`message_sink`, `opening_status`) had **never** been security-audited — the last full
pass, 2026-06-25, predates all of them.

**Result:** 3 High, 3 Medium, 3 Low. All High and Medium fixed and regression-tested.
Gates green: **497 tests**, ruff (now with pydocstyle `D`), mypy.

Every finding below was verified first-hand against running state or installed source.
Nothing here is inference-only, and one finding from the initial sweep was **withdrawn**
under verification (see "Not confirmed").

---

## H-1 — Stored XSS with arbitrary script execution in the chat UI

**Verified end-to-end against the installed `panel 1.9.3` / `bokeh 3.9.1`:**

1. `MarkdownIt('gfm-like').options['html']` → `True`; raw HTML passes the parser.
2. A chat message becomes `panel.pane.markup.Markdown` → a bokeh `HTML` model with
   **`run_scripts = True`** (Panel's default). Model text keeps the payload, HTML-escaped
   only as transport encoding.
3. Client `process_tex()` calls **`html_decode(this.model.text)`** — reversing it.
4. Client `set_html()` assigns **`this.container.innerHTML = html`**.
5. `run_scripts()` **re-creates every `<script>` via `document.createElement("script")`**,
   which makes it execute. Plain `innerHTML` would not.

The most exposed sink needed **no LLM cooperation**: `panel_sink.py`'s `output` setter
streamed a **raw tool result** unescaped, so any page fetched by
`fetch_web_page`/`firecrawl_*` could land script in the trading UI's origin. Also exposed:
the whole LLM response, tool arguments, order-proposal summaries, chart status, PineScript
errors, and seven `f"…{exc}"` sends in `panel_app`.

**Impact:** read the DOM (positions, P&L, account IDs, order proposals), exfiltrate, and
forge ClaudIA messages and order-proposal UI. Injected JS can `.click()` "Stage this
order" — Gate 1 (Touch ID) and Gate 2 (AppKit) still prevent a silent order, but the
on-screen context around that prompt would be attacker-controlled.

**Fix:** new `claudia/panel_markdown.py` as the single rendering control.
`safe_markdown()` (`renderer_options={"html": False}`) is installed feed-wide via
`ChatInterface(renderers=[...])` and used for every directly-constructed pane;
`escape_markup()` covers `ChatStep.stream()`, which builds its own panes and has no
`renderers` parameter.

Two candidate mitigations were **tested and rejected**:

| Candidate | Result |
|---|---|
| Wrap untrusted text in a ```` ``` ```` fence | **Bypassable** — content carrying its own closing fence escapes and renders as markup |
| `run_scripts=False` | **Insufficient** — stops `<script>` but not `onerror=`/`onload=` |

`quote=False` on the escape is deliberate: with `<` and `>` escaped no tag can be opened,
so no attribute context is reachable and quotes cannot contribute — and JSON tool args stay
readable. Dangerous link schemes (`javascript:`, `data:`, `vbscript:`) are already rejected
by markdown-it's `validateLink`, verified separately.

---

## H-2 — Private persona & trading documents were PUBLIC on GitHub

`docs/versions/v1/context.md` (9,679 B) and `docs/versions/v1/principles.md` (11,470 B)
were **tracked at HEAD on `origin/main`** of `github.com/stephus182/claudia_ui`, verified
**public** (`gh repo view --json isPrivate` → `false`). Real content, not placeholders.

The 2026-07-10 `git-filter-repo` scrub was **path-scoped** to `docs/context.md` /
`docs/principles.md` — those are genuinely clean (`git log --all` returns nothing). The
version snapshots were never in scope. They entered history in `f8dde85` (2026-06-11), so
they were public for roughly six weeks.

`.gitignore` lists `docs/versions/`, but **it does not untrack already-tracked files**. It
did work for everything added later: `v2/` and `v3/` exist locally and were correctly
untracked. Only `v1` leaked. Writer is `panel_app._write_version_snapshot` — the same
function as M-2.

This contradicted the `project-context-principles-exposure` memory, which recorded the
issue as fully resolved. That memory has been corrected.

**Fix, stage 1 — untrack:** `git rm --cached` on both files plus
`data/test-sessions/2026-06-23-2208.md`.

**Fix, stage 2 — full history scrub (same day, on request).** Before rewriting, every path
in history was enumerated rather than assuming the known two — the exact step the 2026-07-10
scrub skipped. Scanning **every `.md` blob in every ref** for the documents' distinctive
headers found the content in exactly two blobs, both under `docs/versions/v1/`. That
enumeration also surfaced two exposures that untracking `main` had done nothing about:

- **`refs/heads/panel-migration` still existed on the remote** at `f0bdec58`, carrying both
  files. Only the *local* branch had been deleted after the migration merged.
- **Both tags** (`v0.9.0`, `pre-claude-md-split-2026-07-10`) carried them too.

So the scrub had to cover all four refs, not just `main`:

```bash
git clone --mirror . ~/Documents/claudia_ui-backup-pre-versions-scrub-2026-07-25.git
git filter-repo --path docs/versions/ --invert-paths --force
git push --force origin main panel-migration refs/tags/v0.9.0 \
                              refs/tags/pre-claude-md-split-2026-07-10
```

423 commits preserved (none became empty). Verified by **mirror-cloning back from GitHub**
and re-running the blob scan across all refs: zero `docs/versions/` paths, zero
private-content blobs. Local files untouched on disk at 0600; 497 tests still green.

**Residual — and it is not zero.** GitHub still serves the old blobs by direct SHA:
`c443aea` and `f0bdec58` return **HTTP 200** on `raw.githubusercontent.com`, even though
both commits are unreachable from every current ref and absent from a fresh clone. This is
GitHub retaining unreachable objects, and it matches their documented behaviour — a force
push alone does not purge cached views. Per GitHub's own guidance, the remaining step is a
Support request to dereference, garbage-collect, and remove cached views:

> "contact us through the GitHub Support portal" to "Remove cached views."
> — [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)

Details Support asks for, gathered:

| Field | Value |
|---|---|
| Repository | `stephus182/claudia_ui` |
| Affected pull requests | **0** (`gh pr list --state all` → empty) |
| Forks | **0** — no independent copies keeping the blobs alive |
| First changed commit | `56631169d0483a901bb0138ac5f490316aefe6c5` → `424910317b3623fa12c1e2669a05473deca27485` |

Until that request completes, **treat the content as disclosed.** GitHub's guidance is
explicit that for genuinely secret material the first step is rotation, not removal — these
are trading rules and persona text rather than credentials, so there is nothing to rotate,
but the disclosure window (2026-06-11 → 2026-07-25) stands regardless.

Backup kept permanently at
`~/Documents/claudia_ui-backup-pre-versions-scrub-2026-07-25.git` (13 MB, verified to
contain the pre-scrub blobs). Any other clone of this repo now has orphaned history and
needs a fresh re-clone, not a pull.

---

## H-3 — Panel server bound to all interfaces, with no authentication

`panel_app.main()` called `pn.serve(...)` with **no `address=`**. Panel's default is
`None` → bokeh `bind_sockets(None, port)` → tornado binds every interface. Verified on the
**running** process (PID 37869):

```
Python  37869 steph  7u  IPv4  TCP *:8001 (LISTEN)
Python  37869 steph  8u  IPv6  TCP *:8001 (LISTEN)
```

Host LAN address `192.168.1.13`. The IBKR gateway binds `127.0.0.1:5055` — ClaudIA was the
outlier.

`websocket_origin` was **not** an equivalent control:

- It gates only the websocket upgrade. `GET /` (bokeh `DocHandler`) is not origin-checked
  and serving it creates a **full session** — Drive download, IBKR calls, a `create_session`
  row, possibly a live Flex sync; on disconnect, a session report and a `claudia.db` upload.
- Tornado **skips `check_origin` entirely when no `Origin` header is sent** ("we assume it
  did not come from a browser"), so any non-browser client omits it.
- `Origin` is client-supplied and trivially forged regardless.

No auth provider, no XSRF cookies, no cookie secret. A reachable session would expose the
opening-status payload — Account Summary, Open Positions, Account P&L, Live Orders — and
could drive every widget. Order placement still required physical Touch ID, so the §2
barriers held.

macOS ALF was enabled but with *auto-allow signed* (built-in and downloaded) on, stealth
off, and `/usr/bin/python3` listed "Allow incoming connections" — unlikely to have been
blocking it.

**Fix:** `address="127.0.0.1"`, with the reasoning recorded at the call site.

---

## M-1 — Order proposals reached display and execution unvalidated

`proposal` was `json.loads()` of an LLM-emitted block with **no schema validation
anywhere**, flowing both into the approval summary and, unchanged, into
`_execute_staged_order_core`. `action` was never checked against `{BUY, SELL}`; `quantity`
never checked as positive or numeric; `order_type`/`tif`/`sec_type` never checked against
the enums the model is told to emit.

Defence in depth, not a bypass — Gate 1/Gate 2 always stood between a malformed proposal
and IBKR. What was missing is that the human could be shown, and click through, a summary
built from values the model was never permitted to produce.

**Fix:** new `claudia/order_proposal_schema.py` — deliberately dependency-free and
execution-free, so `agent.py` keeps its runtime decoupling from the order layer. It
**validates and rejects; it never repairs** (order parameters are immutable —
`feedback-order-parameter-immutability`). A rejected proposal is dropped *and reported in
chat*, never silently: silent dropping would worsen the known failure mode where ClaudIA
claims an order was staged without emitting a usable block
(`finding-llm-proposal-block-emission`).

Verified against 19 shapes, including the live-proven ES order (`716373691`) that cleared
the full gate chain on 2026-07-24 — accepted, as were lowercase enums and fractional FX
quantities.

---

## M-2 — Version snapshots of the private docs written world-readable

Same function as H-2. `_write_version_snapshot` used `Path.write_text()`, which honours the
umask (0644), with **no `os.chmod` after the write** — `grep -n "chmod\|0o600" claudia/*.py`
returned **zero hits in the whole package**. Every new doc version therefore re-created
world-readable copies of the persona and trading rules, and a manual `chmod` could not
durably fix it.

This is precisely the failure mode `SECURITY.md` §7 already reasons about for the OAuth
token ("creation flags only set the mode on files that did not already exist"); the
reasoning had simply never been applied here.

Found on disk: `docs/context.md`, `docs/principles.md`, all six `docs/versions/*/` files,
and `.env` (holding `ANTHROPIC_API_KEY`) all at 0644. `data/claudia.db` was correctly 0600.

**Fix:** `chmod(0o600)` after both writes, plus a one-off `chmod 600` on the existing files.
`docs/context.md`/`docs/principles.md` are hand-maintained — `GDriveSync.read_text()`
returns a string and never writes them — so their mode now persists.

---

## M-3 — `SECURITY.md` described an architecture that no longer existed

Not cosmetic: this is the document consulted to decide whether a change is safe, and it
vouched for controls that were gone. Verified claim by claim; the corrections are in the
rewritten file. The substantive ones:

| Was | Now |
|---|---|
| "ClaudIA runs entirely on `localhost`"; "Chainlit web UI \| `localhost:8000`" | Panel on `127.0.0.1:8001`, with the bind-address rationale and what `websocket_origin` does **not** cover |
| "CORS: `.chainlit/config.toml` restricts `allow_origins`" | That file does not exist; the control it described was gone |
| §9's four `claudia/app.py` endpoints (`/api/status`, `/cl/*`) | None exist. Section replaced with the real Panel surface: no app-authored HTTP routes, the Bokeh websocket as the whole transport, and the H-1 rendering control |
| "`custom.js` … never uses `innerHTML`" | File deleted. The live JS surface is Panel's own renderer — which is exactly where H-1 lived. This checklist item had never been re-run against Panel |
| `verify=False` "scoped to **the single** keepalive call" | **Two** sites: `/tickle` and `/iserver/auth/ssodh/init` (added 2026-07-17) |
| `_SAFETY_BLOCK` at `agent.py:47-201` | Off by one — `48-202` |
| §11 "15 dedicated unit tests" | 12 collected. Every branch the doc enumerates *is* covered; the count was wrong |
| §12 "`cl.make_async()` handlers" | Chainlit API — now `asyncio.to_thread` / `asyncio.Lock` / `call_soon_threadsafe` |
| §13 "All 8 claudia_ui modules" | 18 modules; 7 had never been audited |

Also added: a new **§14 Private Documents and Git** (the `.gitignore`-does-not-untrack
trap from H-2, with the structural check), plus previously undocumented surfaces — the
Bokeh/Tornado websocket, `pn.state` process-global singletons (the single-user assumption
is load-bearing and was unstated), `ChatInterface.callback_exception="summary"` rendering
exceptions into the feed, and a "never enable `--dev`/autoreload" note.

**Verified accurate and kept verbatim:** §2's barrier logic, §4, §5's 8-subsection
inventory, §6 in full, §8's two-layer SSRF architecture (every clause re-checked, including
per-hop redirect re-validation), §10 in full, §11's soft-recovery guarantees.

---

## Low — recorded, not fixed

| # | Finding | Note |
|---|---|---|
| L-1 | `session_reporter.py` writes a 180-char verbatim snippet of any tool result matching `_ERROR_KEYWORDS` — which include `"unauthorized"` and `"traceback"`, exactly the responses that carry auth headers or signed URLs | No scrubbing pass. Mitigated: `data/test-sessions/` is git-ignored and filenames are timestamps |
| L-2 | Reflected injection in the chart pane status line (`CacheError` echoes the rejected symbol) | **Resolved by the H-1 fix**; the pane now uses `safe_markdown` |
| L-3 | `data/test-sessions/2026-06-23-2208.md` was tracked despite being git-ignored | Checked and benign — 527 B, tool names and a session UUID, no account data. Untracked with H-2 |

**Deliberately not re-raised:** the committed UI screenshots with live account data at
`f434312`. The user previously declined that scrub.

---

## Not confirmed — withdrawn under verification

The initial sweep flagged a **display/execution divergence risk** from the three TIF
aliases (`tif` / `time_in_force` / `timeInForce`) being read independently by the summary
formatter and the execution core. Checking the actual source, both use the **identical
expression** (`order_flow.py:52` and `:257`), and the cancel/modify paths both read only
`tif`. There is no divergence, so no validator rule was written for it. Recorded here
because a plausible-sounding finding that does not survive checking is worth remembering.

---

## Verified clean

- **PineScript Copy is genuinely safe** — the pine source is bound as a **named CustomJS
  argument**, never interpolated into the code string. Probed with a breakout payload; it
  survived intact as data. The one place in the new UI where the injection question had
  already been thought through.
- **Subprocess usage is clean** — no `shell=True`, no `os.system`, all list-form; the TV
  sidecar gets an explicit env allowlist rather than `os.environ`. "Start IBKR Gateway"
  passes no arguments.
- **Hardcoded safety block intact** (`agent.py:48-202`, 8 subsections, appended last and
  unconditionally).
- **Hard Rule 1 holds** — 42 toolkit tools + 5 local tools, none of which place, modify,
  cancel, or reply to an order.
- **SSRF guard fully intact** — every clause of §8 re-verified, including per-hop redirect
  re-validation (`allow_redirects=False`, `_MAX_REDIRECTS = 5`).
- **Bokeh chart figure is not a sink** (canvas text, no HoverTool templates).
- **SVG upload is not an XSS vector** — `pn.pane.Image` emits a `data:` PNG.
- **`ANTHROPIC_API_KEY` does not leak** — appears in `claudia/` only inside two comments.

---

## Documentation pass (same sweep)

An AST scan of all 17 then-existing modules found **87 docstring defects: 67 missing, 15
incomplete, 5 stale**. All fixed. The five stale ones were the Chainlit stragglers the
2026-07-24 cleanup missed, including `agent.py:493`'s "called by `on_launch_tradingview`" —
**a symbol that exists nowhere in the repo**. Also removed: 15 dangling `app.py:NNN` line
cites pointing into a deleted file (the past-tense provenance claim was kept; the
unverifiable numbers were dropped). The 7 intentional past-tense Chainlit mentions were
left alone.

Two gates now prevent regression:

- ruff pydocstyle `"D"`, with the formatting-opinion codes ignored (they conflict with the
  house style of multi-paragraph docstrings that explain *why* and cite sources).
- `tests/test_docstring_coverage.py` — an AST sweep, because **ruff's D1xx only covers
  *public* symbols**. Roughly half this package is `_`-prefixed, and that half contains
  nearly every safety-critical function (`_on_stage`, `_fetch_web_page`, `_init_session`),
  so ruff alone would have left the important cases unguarded. The same file also fails on
  any new dangling `app.py:NNN` cite or stale Chainlit-era reference.

---

## Files changed

| File | Change |
|---|---|
| `claudia/panel_markdown.py` | **New** — `safe_markdown` / `escape_markup`, the H-1 control |
| `claudia/order_proposal_schema.py` | **New** — proposal schema validation (M-1) |
| `claudia/panel_app.py` | Loopback bind (H-3); snapshot chmod (M-2); safe renderer installed; module docstring; 11 handler docstrings; stale refs + dangling cites |
| `claudia/panel_sink.py` | Escaped both ChatStep streams (H-1); 13 docstrings |
| `claudia/agent.py` | Proposal validation + user-visible rejection (M-1); `_fetch_web_page` SSRF docstring; 2 stale refs |
| `claudia/panel_order_flow.py` | `safe_markdown` panes; 10 docstrings incl. the three live-order handlers |
| `claudia/order_flow.py` | Docstrings for the three approval-text formatters |
| `claudia/message_sink.py` | Documented the three safety-critical protocol methods + `__aexit__` |
| `claudia/panel_chart.py` | `safe_markdown` status pane; `_on_load` docstring |
| 8 other modules | Docstring coverage to 100% |
| `SECURITY.md` | §1, §2, §3, §7, §8, §9, §11, §12, §13 corrected; §14 added |
| `pyproject.toml` | pydocstyle `"D"` enabled with a documented ignore list |
| `tests/test_security_regressions.py` | +26 tests |
| `tests/test_docstring_coverage.py` | **New** — 20 tests |
| `tests/test_panel_app.py` | +1 (renderer wiring; needs that module's scaffolding) |

---

## Verification

```bash
pytest                                          # 497 passed
ruff check claudia/ tests/ && mypy claudia/     # both clean
git ls-files -i -c --exclude-standard           # prints nothing
stat -f "%Sp %N" .env docs/context.md docs/principles.md   # all -rw-------
```

**Still outstanding — requires a restart to confirm.** The bind fix is unit-tested, but the
running instance (PID 37869) still predates it. After the next restart:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN   # must show 127.0.0.1:8001, NOT *:8001
```

Then load `http://localhost:8001` and confirm chat, status dots, the order-proposal button,
PineScript copy/inject, and the chart pane all render — the escaping change touches every
message, so a visual pass matters. Paste a message containing literal `<b>bold</b>` and
confirm it displays as text while `**bold**` still renders bold.
