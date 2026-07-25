# Post-Migration Cleanup & End State — 2026-07-24

First run after the Chainlit→Panel migration merged to `main` (same day). Audit of the
migration claim — *"no Chainlit workarounds in code, 100% Panel-native"* — followed by
removal of everything the audit surfaced. This file is both the audit record and the
end-state snapshot; every statement below was verified by a fresh command on 2026-07-24.

**Scope exclusion (user-directed):** `SECURITY.md` still describes the Chainlit-era
architecture as current (Chainlit UI on `:8000`, CORS via `.chainlit/config.toml` — a file
that no longer exists, HTTP endpoints added by the removed `claudia/app.py`). It is
deliberately untouched here; a dedicated SECURITY.md audit is scheduled separately.

---

## Audit verdict (pre-cleanup)

**The core claim held.** Evidence:

- Zero Chainlit imports or API usage in `claudia/` and `tests/`; `pip show chainlit` →
  not installed; `pyproject.toml` depends on `panel>=1.9`, never Chainlit.
- Workaround sweep (`workaround|hack|monkey.?patch|kludge|XXX|FIXME|shim` over `claudia/`,
  `tests/`, `scripts/`, `start-claudia.sh`) matched only pytest's standard `monkeypatch`
  fixture in tests and the two lines in `panel_app.py` that state the no-workarounds
  principle itself.
- The only client-side JS in the entire app is `copy_btn.js_on_click(...)`
  (`claudia/panel_pinescript.py:107`) — Panel's first-class Button API for the clipboard,
  the intended Panel-native mechanism, not a patch.
- `panel_ws_fix.py` and its test (the one Panel workaround built and then deleted during
  the migration) are gone; only stale `.mypy_cache` entries remained (removed below).

**Residue found** — dead artifacts and doc rot, not workarounds:

| # | Residue | Where |
| --- | --- | --- |
| 1 | Dead tracked Chainlit config (initial 2026-06-09 scaffold, referenced by nothing) | `chainlit.yaml` |
| 2 | Orphaned Chainlit dependency stack in the venv (`Required-by:` empty): `literalai`, `fastapi`, `python-socketio` (+ transitives) | `.venv` |
| 3 | Docstrings describing **current** behavior in Chainlit/`app.py` terms (see table below) | 8 code files |
| 4 | `# Chainlit` ignore block (`.files/`, `chainlit.db`) + empty `.files/` upload-spool dir | `.gitignore`, repo root |
| 5 | Present-tense "Chainlit" in the startup reference doc body | `docs/startup-flow.md` |
| 6 | Stale cache entries (`chainlit/`, `panel_ws_fix`) | `.mypy_cache` |
| 7 | Chainlit-era architecture described as current | `SECURITY.md` (**deferred**, separate audit) |

---

## Actions taken

| File | Fix |
| --- | --- |
| `chainlit.yaml` | Deleted (was tracked; deletion lands with this cleanup's commit) |
| `.gitignore` | `# Chainlit` block (`.files/`, `chainlit.db`) removed |
| `.files/` | Empty Chainlit upload-spool directory removed (`rmdir` — would have failed if non-empty) |
| `.mypy_cache/` | Deleted; regenerated clean by the post-rebuild `mypy` gate run |
| `claudia/tradingview.py` | PineScript rendering bullet now points at `panel_pinescript.py`; screenshot fallback "app.py / agent.py" → "panel_app.py / agent.py" |
| `claudia/execution_listener.py` | "concurrent Chainlit sessions" → Panel; `format_pnl_snapshot` shared-by pointer "(app.py)" → "(opening_status.py)" |
| `claudia/status.py` | "/api/status endpoint" (removed with the Chainlit app; nothing serves it in Panel-native Tornado) → panel_app status dots reading `get_status()` in-process, both in the module docstring and `ConnectivityChecker`'s |
| `claudia/session_reporter.py` | "Called from app.py on_stop" → "panel_app.py's `_run_session_cleanup`" (verified actual caller, `panel_app.py:558`) |
| `claudia/panel_order_flow.py` | Header no longer claims order_flow.py has "Chainlit-native render_*_proposal functions" (it holds framework-agnostic cores; rendering lives here) |
| `claudia/opening_status.py` | Port-parity concurrency note now explicit: "the **removed Chainlit** app.py's cl.make_async" |
| `tests/test_panel_sink.py` | Dropped reference to deleted `tests/test_message_sink.py` / `ChainlitMessageSink` |
| `tests/test_opening_status.py` | Parity-source note now past-tense + explicit "removed Chainlit app.py" |
| `docs/startup-flow.md` | 5 present-tense "Chainlit" statements → Panel (banner + historical `app.py:NNN` parity references deliberately kept, see End state) |
| `.venv` | Full rebuild from CLAUDE.md Dev Setup §2–3 exactly (`python3.11 -m venv` → `pip install -e ".[dev]"` → strict-editable ibkr_core_mcp) |
| `docs/project-status.md` | Milestone row added pointing at this record |

---

## End state (all verified fresh, 2026-07-24)

**1. No Chainlit-named file exists.**
`find . -iname '*chainlit*'` (excluding `.git`/`.venv`) → nothing.

**2. Code carries exactly 11 "Chainlit" lines — all intentional historical provenance,
zero describing current behavior:**

- `claudia/panel_app.py:11-12,95` — cutover note (what Phase 11 removed)
- `claudia/panel_sink.py:22` — `pn.chat.ChatStep` described as "Panel's built-in
  equivalent of Chainlit's ChatStep" (design provenance)
- `claudia/panel_pinescript.py:11` — "the concrete Panel-over-Chainlit win" (clipboard)
- `claudia/opening_status.py:3,6,43` — port header + parity note, all "old/removed Chainlit"
- `tests/test_opening_status.py:4`, `tests/test_order_flow.py:271,1027` — port/removal notes

Likewise the remaining `app.py:NNN` references (6 lines, `opening_status.py` + its test)
are port-parity line references into git history, each framed as historical by its module
docstring — the same deliberate convention `docs/startup-flow.md`'s post-cutover banner
declares for that doc.

**3. venv is fully accounted for.** After the from-scratch rebuild:
`chainlit`, `literalai`, `fastapi`, `python-socketio`, `python-engineio`, `traceloop-sdk`
are **absent**. Remaining web-server packages are genuine dependencies, `pip show` verified:
`uvicorn` ← `mcp`; `starlette` ← `mcp`, `sse-starlette`; `sse-starlette` ← `mcp`;
`panel-material-ui` ← `panel`. The rebuild doubles as proof the documented dev setup works
from scratch (fresh resolve moved `anthropic` → 0.120.0 and `mcp` → 1.28.1, both inside
their pins; suite green below).

**4. Gates on the fresh venv:**

```
ruff check claudia/ tests/   → All checks passed!
mypy claudia/                → Success: no issues found in 17 source files
pytest -q                    → 451 passed in 18.98s
```

(mypy resolving `ibkr_core_mcp` also re-proves the strict-editable install requirement
documented in CLAUDE.md Dev Setup §3.)

**5. Deliberately left as-is:**

- `SECURITY.md` — user-directed separate audit (the one remaining place that describes
  Chainlit-era architecture as current).
- Historical records: `docs/plans/` (git-ignored archive incl. the migration plan),
  `docs/audits/`, milestone rows in `docs/project-status.md`, README's one-line
  migration note — records correctly keep their history.
- `docs/startup-flow.md` banner + `app.py:NNN` parity references (declared convention).
- `.claude/settings.json` permission allowlist still contains old
  `docs.chainlit.io`-scrape entries — harness command history, not project code; harmless.

## Re-verification one-liners

```bash
grep -rni chainlit claudia/ tests/ scripts/ start-claudia.sh pyproject.toml .gitignore  # 11 provenance lines
find . -not -path './.git/*' -not -path './.venv/*' -iname '*chainlit*'                # nothing
.venv/bin/pip list | grep -iE 'chainlit|literalai|fastapi|socketio'                    # nothing
.venv/bin/ruff check claudia/ tests/ && .venv/bin/mypy claudia/ && .venv/bin/pytest -q # all green
```
