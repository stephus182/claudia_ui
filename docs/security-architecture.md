# Security Architecture

The living design document for how `claudia_ui` keeps a language model, a browser page, a
Node sidecar and a future coding agent away from a live brokerage account — and how each of
those properties is *enforced* rather than intended.

**How this differs from the other two security documents.**

| Document | Kind | Answers |
|---|---|---|
| `SECURITY.md` | Policy and control inventory (the GitHub-conventional file, public-facing) | "What controls exist, where, and how do I report a vulnerability?" |
| **`docs/security-architecture.md`** (this file) | Living design | "Where are the boundaries, what must stay true, what enforces it, why was it built this way, and how do I change it without breaking it?" |
| `docs/audits/2026-09-13-security-architecture-audit.md` and earlier audits | Point-in-time evidence, never retroactively edited | "What was found on that day, what was proven, what was done?" |

When a control changes, `SECURITY.md` and this file change together; the audit that motivated
the change stays as it was.

**What this file does not contain.** Anything `ibkr_core_mcp` owns. The gate policy, the order
write endpoints, the capability registry, the backtest sandbox, the SSRF second layer and the
redaction functions are designed and enforced there, and are described in
`ibkr_core_mcp/docs/security-architecture.md`. Restating them here is how the 2026-09-13 audit
found six stale security claims in this repository, including one that was simply false. Where
this file needs such a fact it points at it and, where it can, computes it (§ 5, CLA-SEC-012).

---

## 1. Principals and threat model

`claudia_ui` is not a UI wrapper. It adds a long-running language model, persistent memory,
operator-authored documents that become the system prompt, a browser client, a Node sidecar
with Chrome DevTools access, and the moment where model intent becomes human-authorised
action. Each of those is a principal with its own trust.

| Principal | Trusted for | Not trusted for | Where it enters |
|---|---|---|---|
| **Human operator** | Configuration, the two personal documents, the click on each proposal, Touch ID and the Gate 2 dialog | Nothing is withheld — this is the authority the whole system serves | Browser on loopback, `.env`, the filesystem |
| **The model** (Claude) | Reads, analysis, proposing orders, writing Pine | Executing anything, asserting what it did, being the evidence for its own claims | `tool_use` and `text` blocks in the API response |
| **The current user message** | The operator's intent | Facts about the account | The chat callback |
| **Conversation history** (`claudia.db`) | Continuity | Authority beyond the role it was stored with; tool payloads (dropped, never reconstructed) | `get_history` → `_history_to_messages` |
| **`docs/context.md`, `docs/principles.md`** | Persona and trading rules | These *are* the system prompt: whoever writes the file writes the prompt | `context_loader` → `system=` |
| **Versioned document snapshots** | A historical record | Being current rules | `get_doc_version` → a `tool_result` |
| **IBKR** | Account and market data, order state | The rendering safety of its strings; the shape of its numbers | `ibkr_core_mcp` client and WebSocket |
| **`ibkr_core_mcp`** | The two gates at the innermost write site, the read tools, redaction of its own errors | ClaudIA's invariants: the click, display correspondence, ClaudIA's own error text | Direct Python import (strict editable) |
| **Google Drive** | Persistence of the database, the documents, the Flex archive | A Drive-side edit becomes the next session's system prompt (§ 9) | `GDriveSync` at session start, matched by filename |
| **TradingView Desktop** | Charts and the Pine editor | Anything else; CDP has no authentication by design | The sidecar over port 9222 |
| **The TradingView MCP sidecar** | The 17 curated tools | Anything else — it runs as the operator, unsandboxed (§ 9) | `stdio_client` subprocess |
| **Web and tool output** | Nothing | Anything: this is the prompt-injection vector | `fetch_web_page`, the scraper tools → `tool_result` |
| **The browser client** | Rendering the operator's view, and their clicks | Authority beyond the loopback origin allowlist (§ 9) | Tornado on `127.0.0.1:8001` |
| **SQLite** | Sessions, messages, decisions, document versions, withdrawals | Being a trust upgrade for the content it stores | `conversation_store` |
| **A future coding agent** | Ordinary changes under CI | Silent violation of any invariant in § 5 | `git push` |

The threat this architecture is shaped by is not a remote attacker. It is **a plausible,
well-intentioned change** — by a person or an agent — that is correctly typed, correctly
linted, fully tested, and quietly removes a property nobody wrote down.

---

## 2. Capabilities

The core owns the tool capability registry and is the only source for what a tool can do.
Measured from it on 2026-09-14: **44 tool definitions, 24 carrying `READ_ONLY` and 20 not**,
of which four (`create_price_alert`, `modify_price_alert`, `delete_alert`, `activate_alert`)
are **ungated IBKR account writes the model can perform unassisted**. They are not order
writes and `ORDER_EXECUTION` has no spelling in the vocabulary at all, which is why "the model
can reach no IBKR write" was a false sentence in this repository until 2026-09-14. Any number
or adjective this project states about that set is computed from the registry by
`tests/security/test_cross_repo_contract.py`, never typed by hand.

On top of the toolkit, ClaudIA declares its own: **5 local utility tools** (document versions,
past-conversation search, a public-web fetch behind an SSRF guard, live P&L), **3 proposal
tools**, and **17 curated TradingView tools** when the sidecar is connected.

ClaudIA's own capabilities, which the core's registry does not describe:

| Capability | Where | Who can trigger it |
|---|---|---|
| MODEL_CONTEXT — writes the system prompt | `context_loader`, `_build_system_prompt` | the operator's files; Drive |
| PERSISTENT_MEMORY | `conversation_store` | the agent loop, the order cores, the session lifecycle |
| UI_RENDER | the `panel_*` modules | the model, tools, IBKR, TradingView, exception text |
| NETWORK, model-directed | `fetch_web_page` | the model, SSRF-guarded on every hop |
| TRADINGVIEW_CDP | `tradingview.py` → sidecar → port 9222 | the model, through the curated set |
| SUBPROCESS | `tradingview.py`, `gateway_launch.py`, the shell scripts, the keepalive agent | startup and operator buttons |
| GOOGLE_DRIVE | `gdrive_sync.py` | session start, session end, shutdown |
| ORDER_PROPOSAL | the three `propose_*` handlers | the model |
| HUMAN_CONFIRMATION | a rendered button and a click | **a person, and only a person** (§ 6.1) |
| ORDER_EXECUTION_REQUEST | the three execution cores | the click closures alone |
| IBKR_ORDER_EXECUTION | inside `ibkr_core_mcp` after both gates | owned by the core |

---

## 3. Trust-boundary map

```
SOURCE                      VALIDATION / CLASSIFICATION            COMPONENT                     PRIVILEGED SINK
──────                      ───────────────────────────            ─────────                     ───────────────
user message ────────────►  (none; it is the operator)         ►  ChatInterface ─────────────►  model context, `user` row
model tool_use ──────────►  name ∈ one of three closed sets    ►  dispatcher (agent.py) ──────►  local handlers        [READ / COMPUTE]
                                                                                          ──►  ClaudeToolkit.execute [the core's registry]
                                                                                          ──►  TradingViewBridge     [TRADINGVIEW_CDP]
model propose_* ─────────►  strict JSON Schema (the API),      ►  _record_proposal ──────────►  _pending_proposal (by reference)
                            then _proposal_defect: 7 terms
                            the schema cannot express
proposal ────────────────►  deepcopy at render (_snapshot)     ►  panel_order_flow ──────────►  a card + two buttons   [UI_RENDER]
a human click ───────────►  _OneShot.claim(), first call       ►  _execute_*_order_core ─────►  IBKRClient gated entry [ORDER_EXECUTION_REQUEST]
                                                                                                  └─► Gate 1 ─► Gate 2 ─► IBKR   [the core]
tool output / web page ──►  escape_markup                      ►  ChatStep ──────────────────►  the browser            [UI_RENDER]
model prose ─────────────►  safe_markdown (feed renderer)      ►  ChatInterface ─────────────►  the browser
IBKR / gateway strings ──►  safe_text, safe_toast              ►  System log, notifications ─►  the browser
history rows ────────────►  role preserved; withdrawn dropped  ►  _history_to_messages ─────►  `messages=`, never `system=`
decisions + tool ledger ─►  allowlisted identity fields        ►  operator channel ──────────►  one `role: system` message
context/principles ──────►  1 MB cap; hash compared            ►  context_loader ────────────►  `system=`, safety block last
Drive ───────────────────►  filename match; integrity_check    ►  GDriveSync ────────────────►  the database, the documents (§ 9)
exceptions ──────────────►  the core's `_safe_error` for its   ►  tool_result / log / UI        (ClaudIA's own are not
                            tools; ClaudIA's own: unwrapped                                      routed through `redact_error` — § 9)
```

---

## 4. The privileged sinks and who may touch them

| Sink | Only through | Guarded by |
|---|---|---|
| A live IBKR order | `_execute_staged_order_core`, `_execute_modify_order_core`, `_execute_cancel_order_core` | A human click (CLA-SEC-002), then the core's Gate 1 + Gate 2 |
| The three gated client methods | `order_flow.py`, one file | AST: the names appear nowhere else in `claudia/` or `scripts/` |
| The system prompt | `context_loader` | Two operator files, 0600; the safety block appended last; no history, ever |
| The model's context | the dispatcher, `_history_to_messages`, the operator channel | Role preservation; withdrawn rows excluded from replay *and* from search |
| The browser DOM | `panel_markdown`'s four helpers | One sanctioned construction site per pane type; canaries per surface |
| SQLite | `conversation_store` | A CHECK constraint that refuses a `system` role outright |
| Google Drive | `gdrive_sync` | Fixed filenames on upload; size cap and integrity check on download |
| A subprocess | `tradingview.py`, `gateway_launch.py` | List form, no shell; an env allowlist for the sidecar |
| TradingView | the 17 curated tools | The curated set is a constant; no order-shaped tool exists in it |
| The public web | `fetch_web_page` | `_validate_public_url` on the initial URL and every redirect hop |

---

## 5. The twelve invariants and their enforcement

Each row is a property that must stay true regardless of implementation. "Mechanism" is the
code that makes it true; "Enforcement" is the test that fails when it stops being true.

Status is honest: **BUILT** means a test fails today if the property is violated; **PARTIAL**
means the property holds and only part of it is machine-checked; **OPEN** means it is a stated
intention with no enforcement yet, and the audit is the record of why.

| # | Invariant | Mechanism | Enforcement (`tests/security/`) | Status |
|---|---|---|---|---|
| **CLA-SEC-001** | The model's own layer cannot reach order execution | A four-branch dispatcher over three closed name sets; `agent.py` imports no execution module | `test_model_cannot_execute_orders.py`: the model layer names no write method, client, `_post` or authorization; imports neither execution module; the dispatcher reaches exactly seven collaborators; dynamic attribute resolution banned in the two shapes a name probe cannot follow | BUILT |
| **CLA-SEC-002** | Execution begins at a human click and nowhere else | Cores called only from `on_click` closures; `_OneShot` claimed as the first call in every handler | `test_human_click_execution_boundary.py`: cores reached from one module and only from bound handlers; every handler claims first; no module writes `clicks`, passes `clicks=`, calls `trigger` or `setattr`; the sink holds no button | BUILT |
| **CLA-SEC-003** | Proposing is a declaration — it reaches nothing | `_record_proposal`: three checks, one assignment, one string | `test_proposal_tools_are_side_effect_free.py`: collaborator set pinned; no I/O call; no write to the proposal in any of six shapes | BUILT |
| **CLA-SEC-004** | Order parameters are immutable, and every price is a price | Reject whole, never repair; 7 terms in `_proposal_defect`; the dict is stored by identity | `test_security_regressions.py` (the malformed table, incl. non-finite and zero prices), `test_proposal_tools_are_side_effect_free.py` | BUILT |
| **CLA-SEC-005** | What the human reads is what the click sends | `_snapshot` deepcopy at render; one shared price formatter with the dialog; a priced type must carry its price | `test_panel_order_flow.py` (render → mutate → click), `test_order_flow.py` (precision, totality), `test_human_click_execution_boundary.py` (every renderer snapshots) | BUILT |
| **CLA-SEC-006** | Untrusted content cannot execute in the UI | Four helpers in `panel_markdown`, each with the right number of escapes for its path | `test_security_regressions.py` canaries + guard-on-the-guard; `test_panel_system_log.py`; `test_panel_dashboard.py` | PARTIAL — the canaries cover the surfaces that carry untrusted text today; there is no AST allowlist of construction sites yet (audit § B-8) |
| **CLA-SEC-007** | Stored content never gains authority | Role mapping is closed; withdrawn rows excluded from replay and from search; the store's CHECK refuses `system` | `test_conversation_store.py`, `test_agent.py` | PARTIAL — no single test asserts role preservation across every stored kind (audit § 6) |
| **CLA-SEC-008** | A claim about an action is checked against evidence, and the evidence is the API | Four detectors keyed on text, ruling from `called_tools` and `_pending_proposal`; withdraw-and-retry | `test_agent.py`, `test_corpus_precision.py` | PARTIAL — detection is four textual shapes; **nothing outside them is prevented**, and the corpus tests skip in CI (§ 9) |
| **CLA-SEC-009** | Unit tests reach no live system and see no real secret | pytest-socket armed at configure time, markers applied at collection; dotenv neutralised before import | `test_no_live_io.py`, plus a staleness test over both exemption lists | BUILT |
| **CLA-SEC-010** | Processes are spawned from two modules, list-form, never through a shell; the sidecar gets an env allowlist | `tradingview.py`, `gateway_launch.py` | `test_tradingview.py`, `test_security_regressions.py` | PARTIAL — the env is asserted on the dict we build, not on what the child receives after the MCP library merges its defaults (audit § 3.10) |
| **CLA-SEC-011** | The Panel server is reachable only from loopback, with an exact origin allowlist | `pn.serve(address="127.0.0.1", websocket_origin=[…])` | `test_security_regressions.py::test_pn_serve_binds_loopback_only`, `test_panel_app.py` | PARTIAL — the kwargs are pinned; nothing forbids `BOKEH_ALLOW_WS_ORIGIN` widening the allowlist from `.env` (§ 9) |
| **CLA-SEC-012** | The cross-repo contract is pinned in both directions | One import list, three gated entry points, the registry as the only source of tool claims | `test_cross_repo_contract.py` | BUILT — but CI still resolves the core at floating `main` (§ 9) |

`pytest tests/security` runs the set in under four seconds (3.7s measured 2026-09-14); it is part of every unit run, of
the pre-push hook, and of CI.

---

## 6. Subsystem designs

### 6.1 The click boundary — ClaudIA's strongest claim

The core's two gates stop an *unattended* write. By the time they fire, model intent has
already become an order body. ClaudIA's contract is one step earlier and stronger:

> Model intent must become a button a person looked at, before any of it becomes an order.

Three renderers draw a card and two buttons. Each button's handler is registered with
`on_click`, and each handler's first call is `_OneShot.claim()`, so a card acts once whichever
half of it is pressed. The cores are called from those six handlers and nowhere else.

Two things this deliberately does **not** defend against, both recorded rather than implied:
in-process code can call a core directly, and any local process can open its own session and
press a button (§ 9). The gates then prompt the operator for a proposal they did not author —
which is a consent gate doing its job, not a bypass.

### 6.2 Display correspondence

Immutability of order parameters is necessary and not sufficient. The 2026-09-13 audit found
the values were faithfully carried and the *rendering* of them was not: both human surfaces
rounded every price to two decimals while the body carried the full value, so a 6E limit of
1.08455 read `1.08` and two prices a full tick apart were indistinguishable.

Three rules now hold the surface to the body:

1. **One formatter.** `order_confirm.price_text_safe` renders every price on the card, in the
   Gate 2 dialog and on the fill line. Two decimals are a floor, not a ceiling. The dashboard
   tables format in the browser and cannot call it, so they are its twin and a test asserts
   their width against what it actually produces.
2. **A snapshot.** `_snapshot` deep-copies the proposal before anything is rendered from it, so
   a later writer cannot change the order without changing the card.
3. **Total rendering.** The card formatter never raises. It is built from
   `get_order_status` on the cancel path, so an IBKR string that parses as no number must
   render as itself rather than take the card down.

### 6.3 The proposal contract

`strict: true` gives types, enums, required keys and closed objects at the API boundary.
Seven things it cannot express are checked in `_proposal_defect`, and it **rejects whole,
never repairs** — a defaulted field is a fabricated order parameter. The seventh, added
2026-09-14, ties the price to the order type, because a schema can say "number or null" but
not "null only when the type is MKT".

### 6.4 Rendering — four paths, three different escape counts

Every HTML-model pane gets one transport escape from Panel and the client undoes it once
before assigning `innerHTML` and re-creating `<script>` nodes. So **one escape is the wire
format, not a control**, and text must arrive already escaped to survive as text. Toasts have
no decode step, so exactly one escape is right there. Getting this wrong in the safe direction
shows entities to the user; getting it wrong in the other direction is an execution sink, and
that is what `safe_text` was from 2026-09-04 to 2026-09-14 — with a green test asserting the
vulnerable signature as success.

| Helper | Path | Escapes |
|---|---|---|
| `safe_markdown` | panes we build, and the ChatInterface `renderers` hook | two (markdown-it emits entities, Panel escapes again) |
| `safe_text` | the System log's monospace lines | two (we escape, Panel escapes) |
| `escape_markup` | text streamed into a `ChatStep` | one, before a default pane adds the second |
| `safe_toast` | `pn.state.notifications` | one — notyf assigns `innerHTML` and never decodes |

### 6.5 History and the operator channel

Only `user` and `assistant` rows are replayed, at the role they were stored with. Tool rows are
dropped rather than reconstructed, because the store keeps no API-assigned `tool_use_id` and
orphaned blocks are a 400; what replaces them is a names-only ledger. A row a claim detector
contradicted is excluded from the replay **and** from full-text search — the second half was
missing until 2026-09-14, so a withdrawn fabrication could return one turn later as a
`tool_result`, which the safety block calls a guaranteed source.

The operator channel is one `role: "system"` message rebuilt each turn from persisted decision
rows. The model cannot produce one: the store's CHECK constraint refuses the role outright.

### 6.6 Test isolation

Sockets are blocked from `pytest_configure`, and pytest-socket's own markers are applied at
collection so the per-test block lands before any fixture. `load_dotenv` is neutralised at
configure time rather than in a fixture, because collection imports `panel_app` — which calls
it at module scope — long before the first fixture runs.

Two exemption lists, kept apart because they are different claims: four tests need real DNS
(a blocked lookup escapes the SSRF guard's narrow `except` and returns "Invalid URL", so an
assertion on "Blocked" would pass for the wrong reason), and one binds a loopback socket
because socket behaviour is what it tests. An opted-in `live_api` run keeps its key and its
network; every other test in that run stays blocked.

---

## 7. CI as a security instrument

| Gate | Detects | Blind to | Blocking |
|---|---|---|---|
| ruff check (incl. `S`) | Known-bad calls | Architecture | yes |
| ruff format | — | — | yes |
| mypy strict, over `claudia/` and `tests/` | Type errors | Everything typed correctly and wrong | yes |
| pytest, including `tests/security/` | Behaviour, and the twelve invariants | Anything without a test | yes |
| **pip-audit** | A known-vulnerable version in the **resolved** tree, audited with the scraper extra a real install carries. The whole Panel/Bokeh/Tornado stack — 18 packages measured 2026-09-13, `tornado` among them — is audited by no other repository | Unknown vulnerabilities | yes; no-fix findings go in `security/pip-audit-ignores.txt` with a reason and a re-check date |
| **gitleaks** | A committed secret or account identifier in the pushed range | History before the scan started | yes |
| CodeQL default setup | A fixed set of Python patterns | Taint from this codebase's untrusted source: tool inputs are not "remote flow sources" | no |

Two things to keep straight when reading results. CodeQL's zero is partly blindness, not
cleanliness — `tests/security/` is the coverage for the model boundary. And **a failure to run
the secret scan and a failure of the secret scan are the same red tick**: on 2026-09-14 the
gitleaks action could not download its own binary and the job failed twice, which reads
identically to a leak. Read the log before assuming. The scanner version is pinned for that
reason and for reproducibility.

---

## 8. Decision log

Dated, so a future reader can tell a decision from a default.

| Date | Decision | Why | Revisit if |
|---|---|---|---|
| 2026-07 | Order execution lives behind a physical button, not a tool | A tool is reachable by generation; a click is not | Never |
| 2026-09-11 | One Touch ID per order write, in the core | Matches IBKR Mobile and TWS; every reply still dialogued | Never cache, never make global |
| 2026-09-14 | The card is a **deepcopy**, not the model's dict | The property rested on the absence of a writer; the core closed the same shape one layer down | Never |
| 2026-09-14 | `_OneShot` is claimed server-side, first call in every handler | `disabled` was set and never read, so the one-shot lived in the browser | Never |
| 2026-09-14 | A priced order type must carry its price — **reject, never default** | A defaulted price is a fabricated order parameter | Never |
| 2026-09-14 | The dialog **names a gap and asserts no requirement** | The first fix said "a LMT order needs one", which is untrue of MIDPRICE — the same defect it was fixing, pointed the other way | Never |
| 2026-09-14 | Zero rejected as a price; negatives kept legal | Zero is what an unset field looks like after a coercion; crude has printed below zero and calendar spreads quote negative | If an instrument legitimately quotes zero |
| 2026-09-14 | One price formatter across both repositories | Three implementations disagreed on trailing zeros and decimal count; the browser-side table is its twin, tested against it | — |
| 2026-09-14 | `safe_text` escapes before Panel does | One escape is the wire format; the browser decodes it | Never; the guard test proves the premise |
| 2026-09-14 | Withdrawn rows hidden from search, not marked | The replay path drops them; two paths feeding the model should agree | If the model needs to reason about its own withdrawals |
| 2026-09-14 | pytest-socket, with an opt-in escape for `live_api` | Local validation cannot prove what the tools endpoint accepts; a block that retires those four tests is worse than no block | — |
| 2026-09-14 | ClaudIA keeps **its own** pip-audit ignore file | Each repository accepts its own risk; reading the core's would let a line removed there break this gate with no commit here | — |
| 2026-09-14 | The gitleaks scanner version is pinned | The action's default asset was returning 504; and a gate whose scanner version floats is not a reproducible control | Bump deliberately |
| 2026-09-14 | Structural helpers are **not** shared with the core's | A shared helper would make one repository's CI depend on the other's test layout — the coupling this work is making explicit, not deepening | — |

---

## 9. Known limits and open items

Stated plainly, because a limit that is not written down is indistinguishable from an
oversight.

- **The trust boundary is every process on this machine, not this user.** The Panel server
  binds loopback with an exact origin allowlist, which stops a hostile web page. It does not
  stop a local process: Bokeh session tokens are unsigned by default, an absent `Origin`
  header is not checked at all, and `GET /` is unauthenticated and creates a full session.
  Any local process can therefore open its own ClaudIA session and press a button. Both gates
  still stand between that and IBKR.
- **`BOKEH_ALLOW_WS_ORIGIN` can widen the allowlist** from `.env`, which `panel_app` loads.
  Nothing forbids it today.
- **Sidecar capability isolation is nil.** The environment allowlist is real and carries no
  secret. The process is still unsandboxed, runs as the operator, and could read `.env` and
  `~/.ibkr_core` if it were compromised. Treated as a supply-chain risk, not a boundary.
- **The sidecar's environment is asserted on the dict we build**, not on what the child
  receives after the MCP library merges its own defaults.
- **`redact_error` has no call site in `claudia/`.** The guarantee that no secret reaches a log
  is held by the *type* of exception each site can see, not by a redaction function. No leak
  has been found in the log, the database or the session reports; the control is absent, not
  the property.
- **Anti-fabrication is detection, not prevention, outside four textual shapes.** Claims
  phrased differently are not detected, and any real tool call in a turn clears two of the
  four detectors. The record is always complete; the blocking is narrow.
- **The corpus precision tests skip in CI**, because the corpus is the git-ignored live
  database.
- **CI resolves `ibkr_core_mcp` at floating `main`.** A green commit here is not reproducible,
  and a core push can turn this repository red with no commit here. The cross-repo contract
  test is the detector until a pinned reference lands.
- **Drive is a remote author of the system prompt.** Files are matched by name and the first
  result is taken; freshness is not trust; a changed document raises a warning in a collapsed
  card and the session continues. The hash baseline for that warning lives in the database,
  which is itself downloaded from Drive.
- **Three refusals before dispatch leave no decision row**, so "every outcome after the button
  is recorded" is true of the exception paths only.
- **A failure to run the secret scan looks like a failure of it.**

---

## 10. Change recipes

**Add a tool.** If it is a core tool, nothing here changes — the registry is the core's. If it
is local, add it to `_LOCAL_TOOLS` and to `_handle_local_tool`'s chain; it must reach no order
write, and `test_model_cannot_execute_orders.py` will say so if it does.

**Add a UI surface that shows text.** Route it through one of `panel_markdown`'s four helpers
and pick the one matching the path — count the escapes, do not reason from a docstring. Add a
canary asserting the model text is double-escaped (or single, for a toast).

**Add a button that does something irreversible.** Bind it with `on_click`, claim a `_OneShot`
as the first call in the handler, and snapshot anything it closes over. The click-boundary
test enforces all three for the order path; extend its constants if you are adding a fourth
kind.

**Change what a proposal may contain.** The schema is the outer layer and `_proposal_defect`
is the inner one. Reject; never repair. Add the case to the malformed table with a reason.

**Import something new from `ibkr_core_mcp`.** Add it to `IMPORTED_API` in
`test_cross_repo_contract.py` in the same commit. If it is an order write, it belongs in
`order_flow.py` or it does not exist.

**Spawn a process.** From `tradingview.py` or `gateway_launch.py`, list form, never a shell,
with an explicit environment. Anywhere else, ask why first.

**Accept a pip-audit finding.** Only when there is no fixed release. One line in
`security/pip-audit-ignores.txt`: the identifier, why the path is unreachable or the risk
accepted, the date, and a re-check date.

---

## 11. Cross-references

- Controls and disclosure: `SECURITY.md`
- The audit that produced this document, with evidence: `docs/audits/2026-09-13-security-architecture-audit.md`
- Earlier audits: `docs/audits/security-audit-2026-08-05.md` and predecessors
- The core's own architecture, which owns the gates and the registry: `ibkr_core_mcp/docs/security-architecture.md`
- Agent behaviour and the anti-fabrication layers: `docs/agent-behavior-reference.md` § 4d
- Order staging end to end: `docs/order-api-reference.md`
- Contributor rules: `CLAUDE.md` § Hard Rules for Developers
