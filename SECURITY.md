# Security Policy

`claudia_ui` is a single-operator trading assistant that can reach a live brokerage account.
This file is the **control inventory**: what protects what, and how to report a problem.

- **Why each control is shaped the way it is** — `docs/security-architecture.md`
- **What was found on a given day** — `docs/audits/`, never edited after publication
- **The gates, the order endpoints, the tool registry, the sandbox** — owned by
  `ibkr_core_mcp`; see `ibkr_core_mcp/SECURITY.md` and its
  `docs/security-architecture.md`. They are pointed at here, not restated. Restating them is
  how the 2026-09-13 audit found six stale security claims in this file, one of which was
  simply false.

---

## Reporting a vulnerability

Open a GitHub issue for anything non-sensitive. For something that should not be public,
contact the repository owner directly and allow a reasonable window before disclosure. This
is a personal project with one operator and no service behind it: there is no bounty and no
SLA, and a clear reproduction is worth more than a severity rating.

---

## Threat model in one paragraph

The adversary this design is shaped by is **not** a remote attacker — the server binds
loopback and there is no public endpoint. It is a plausible, well-intentioned change, made by
a person or a coding agent, that is correctly typed, correctly linted, fully tested, and
quietly removes a property nobody wrote down. Everything below that says "enforced by" names
the test that fails when that happens. Where a property is real but only partly enforced, it
says so.

Secondary, and real: prompt injection reaching the model through fetched web pages and tool
output; a compromised Node sidecar that runs as the operator; and Google Drive, which is a
remote author of the system prompt.

---

## Controls

| Control | Where | Enforced by |
|---|---|---|
| The model has no order-execution tool | `agent.py` dispatcher; three closed name sets | `tests/security/test_model_cannot_execute_orders.py` |
| Execution begins at a human click, never from code | `panel_order_flow.py`, six `on_click` handlers | `tests/security/test_human_click_execution_boundary.py` |
| Proposing reaches nothing and repairs nothing | `agent._record_proposal`, `_proposal_defect` | `tests/security/test_proposal_tools_are_side_effect_free.py` |
| Order parameters are immutable; every price is a finite non-zero number | `_proposal_defect`, seven terms | `tests/test_security_regressions.py` |
| What the human reads is what the click sends | `_snapshot` deepcopy; one shared price formatter | `tests/test_panel_order_flow.py`, `tests/test_order_flow.py` |
| Untrusted text cannot execute in the UI | `panel_markdown`'s four helpers | `tests/test_security_regressions.py`, `tests/test_panel_system_log.py` |
| History replays at the role it was stored with; withdrawn rows never return | `_history_to_messages`, `search_messages` | `tests/test_conversation_store.py`, `tests/test_agent.py` |
| Claims about actions are ruled on by evidence, not by the model | four detectors, `called_tools` / `_pending_proposal` | `tests/test_agent.py`, `tests/test_corpus_precision.py` |
| Unit tests reach no live system and see no real secret | `tests/conftest.py`, pytest-socket | `tests/security/test_no_live_io.py` |
| Subprocesses are list-form, never a shell; the sidecar gets an env allowlist | `tradingview.py`, `gateway_launch.py` | `tests/test_tradingview.py`, `tests/test_security_regressions.py` |
| The server binds loopback with an exact origin allowlist | `panel_app.main` | `tests/test_security_regressions.py::test_pn_serve_binds_loopback_only` |
| The cross-repo API contract is pinned in both directions | `IMPORTED_API`, `GATED_ENTRY_POINTS` | `tests/security/test_cross_repo_contract.py` |
| No private document or account data is git-tracked | `.gitignore`, wholesale rules | `tests/test_security_regressions.py` (tracked **and** ignored) |
| No known-vulnerable dependency in the resolved tree | `pip-audit` job, own ignore file | CI, blocking; weekly cron |
| No secret or account identifier in a pushed commit | `gitleaks` job, `.gitleaks.toml` | CI, blocking |

`pytest tests/security` runs the structural set in under four seconds (3.7s measured 2026-09-14). It is part of every
unit run, the pre-push hook, and CI.

---

## Order execution

ClaudIA cannot place, modify or cancel an order on its own, and the barrier is in two
independent layers owned by two repositories.

**This repository's layer.** The model may call `propose_order`, `propose_cancel` or
`propose_modify`. Those handlers validate, record and return a string; they open no socket and
touch no client. An accepted proposal is deep-copied and rendered as a card with two buttons.
The execution cores are reachable only from a function `on_click` registers, each claims a
one-shot before doing anything else, and nothing in the package writes the parameter Panel
watches. A rejected proposal creates no button and says so in its tool result.

**The core's layer.** After the click, `IBKRClient`'s gated entry points run Touch ID and a
confirmation dialog before the first network call. Policy, fallbacks and the one-authorization-
per-write model are documented in `ibkr_core_mcp/SECURITY.md`; this file deliberately does not
restate them, because the copy here was wrong for three days after the core changed.

**What the model can do that is not read-only.** It reaches the full `ClaudeToolkit`
registry. As of 2026-09-14 that is 44 tools, of which 24 declare `READ_ONLY` and 20 do not —
including four ungated IBKR account writers (`create_price_alert`, `modify_price_alert`,
`delete_alert`, `activate_alert`), tools that write the local SQLite store and Google Drive,
and the backtest sandbox. Those numbers are computed from the core's registry by
`tests/security/test_cross_repo_contract.py`, which also fails if the set of account writers
changes. **"44 read-only tools" was stated here until 2026-09-14 and was false**; the count was
right and the adjective was not.

Full flow, with the reply chain and the read-back: `docs/order-api-reference.md`.

---

## Secrets

| Material | Lives in | Control |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env`, 0600 | Never formatted into any message; third-party loggers pinned to WARNING; not passed to the sidecar |
| IBKR Flex token | `.env` | Travels in a query string, so every Flex call goes through the toolkit, whose exceptions are replaced by the core's fixed sentences |
| Google OAuth token and credentials | `~/.ibkr_core/`, 0600 | Chmod'd after every refresh; uploads are fixed filenames, never a glob |
| IBKR browser session cookie | The browser's own store | Read for the WebSocket handshake only; never logged |

Scanned 2026-09-13, counts only, and no value printed: no key, token, cookie or OAuth
material appeared in the session log, the conversation database, or any file under
`data/test-sessions/`. That is a dated measurement, not a standing guarantee — re-run it
rather than citing it:

```bash
grep -rcE 'sk-ant-|\?t=|Bearer |ya29\.' claudia-session.log data/test-sessions/ 2>/dev/null
```

**Two limits, stated rather than implied.** `redact_error` exists in the core and has **no
call site in this package**: the guarantee that no secret reaches a log rests on the *type* of
exception each site can see, not on a redaction function. And the keepalive shell agent
sources the whole `.env` to read one URL, so it holds every secret for as long as it runs.
Both are open items in `docs/security-architecture.md` § 9.

---

## Private documents and git

`docs/context.md`, `docs/principles.md` and every `docs/versions/*/` snapshot hold ClaudIA's
persona and the operator's trading rules. **None may ever be committed.** The repository is
public.

`.gitignore` lists all three, but **`.gitignore` does not untrack a file that is already
tracked**. That gap is how `docs/versions/v1/` stayed publicly readable from 2026-06-11 until
2026-07-25: the earlier scrub was path-scoped to the two documents and never covered the
snapshots.

Two lessons from that scrub, the first of which had already been learned once:

1. **Enumerate the content, not the paths you remember.** Scanning every blob in every ref for
   the documents' headers found the same content on a branch still live on the remote and
   inside two tags. A `main`-only scrub would have missed all three.
2. **A force push does not purge GitHub.** Unreachable commits still served their blobs
   afterwards. Removing cached views needs a support request; that request was **deliberately
   not filed** and the residual is accepted (operator decision, 2026-07-25 — persona text, not
   credentials). **Do not re-raise.**

**Standing rule (operator, 2026-07-25): keep private data and all plans out of git; older
leftovers are acceptable.** Prevention, not removal, is the control from here.

| Class | Path |
|---|---|
| Secrets | `.env` |
| Persona and trading rules | `docs/context.md`, `docs/principles.md` |
| Verbatim snapshots of both | `docs/versions/` |
| Personal working documents | `docs/plans/` |
| Live account data in images | `docs/panel/screenshots/` |
| Conversation database, Flex archive, session reports | `data/` |

Each class is guarded twice: once that nothing in it is tracked, and once that an ignore rule
covers it. The second test is the control; the first is the symptom. Writing it exposed a real
gap — `data/` had been ignored by enumerated subpath, so any *new* file under it would have
been unignored, the same shape as the snapshot leak.

**Account identifiers are account data too.** On 2026-09-14 the live account number was found
in a tracked test file and replaced with a placeholder. That is the untrack-at-HEAD half of
the standing rule, not the declined history rewrite. A gitleaks rule now matches that shape on
every push.

```bash
git ls-files -i -c --exclude-standard   # must print nothing
```

---

## Audit checklist

Before any significant change:

- [ ] No new path reaches an order write outside `order_flow.py` — the structural test will say so
- [ ] Anything irreversible behind a button is bound with `on_click`, claims a one-shot first, and snapshots what it closes over
- [ ] The hardcoded safety block in `agent.py` is intact, and still appended **last**
- [ ] A new proposal rule **rejects**; it does not default, round or normalise
- [ ] Any new text reaching the browser goes through a `panel_markdown` helper — and the right one: count the escapes for that path rather than copying a neighbour
- [ ] Any new client-side JS receives untrusted values as named arguments, never interpolated into the code string
- [ ] Any new `Tabulator` is `disabled=True` with no click or edit handler
- [ ] Any new file holding private-document or account content is chmod 0600 immediately after the write, **unconditionally**
- [ ] Any new subprocess uses an explicit env allowlist and list-form arguments
- [ ] Any new outbound HTTP path validates the URL and **every redirect hop**
- [ ] Any new import from `ibkr_core_mcp` is added to the cross-repo contract test in the same commit
- [ ] Any new Drive download has a size guard before the loop
- [ ] Any new shared state touched from a worker thread is lock-protected; cross-thread UI updates go through `call_soon_threadsafe`
- [ ] A new dependency finding with a fixed release bumps the floor; only a no-fix finding goes in the ignore file, with a reason and a re-check date
- [ ] `.env` and both private documents are 0600, and `git ls-files -i -c --exclude-standard` prints nothing

---

## Audit history

| Date | Scope | Findings | Status |
|---|---|---|---|
| 2026-06-12 | Drive sync, TradingView, app, core client | 2 High, 4 Medium, 2 Low | All resolved |
| 2026-06-25 | Full re-audit, 8 modules | 1 High, 3 Low | High fixed; Lows accepted |
| 2026-06-27 | Core v1.0 pre-release, ported back | 1 Medium | Decimal/hex IP bypass fixed |
| 2026-07-25 | Post-Panel migration, all 18 modules, git hygiene | 3 High, 3 Medium, 3 Low | High and Medium fixed |
| 2026-08-05 | Post-dashboard, 33 files | 0 High, 0 Medium, 2 Low, 5 doc defects | All fixed |
| **2026-09-13** | **Security architecture — the authority model, not code quality** | **6 confirmed, 20 architectural weaknesses, cross-repo contract, supply chain** | **All 6 confirmed fixed 2026-09-14, each red-test-first; the structural invariants built; the rest tracked in the architecture document** |

The 2026-09-13 audit is the one that changed how this file is written. It found that the
strongest claims here were the least verified: a sanitiser whose test asserted the vulnerable
signature as success, a tool count with a false adjective, a gate policy copied from the other
repository three days after it changed, and four controls that were true only because no one
had yet written the change that would break them. Two subsequent reviews found defects in the
fixes themselves, including one that was the same class of defect it was fixing.

Report: `docs/audits/2026-09-13-security-architecture-audit.md`.
