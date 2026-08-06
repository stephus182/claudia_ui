# Documentation audit — 2026-08-05

**Scope:** every tracked Markdown file in this repository — 39 files, plus `CLAUDE.md`,
`README.md` and `SECURITY.md` at the root. Docstrings and code comments were audited
separately earlier the same day and are not re-covered here except where a doc's claim
*about* the code was checkable.

**Excluded by request:** the gateway pre-flight / borrowed-session work of the same
session. It is a feature track, not documentation, and it is recorded in
`docs/connectivity.md` and the Live Test Log.

**Method:** mechanical checks that can fail, run against the live repository and the live
web, not a read-through. Every check below is reproducible. Where a check could not
distinguish a good doc from a bad one, it was rewritten or dropped rather than reported —
this session had already been burned once by a URL probe that graded on HTTP status
against a site that answers **200 with a "Page Not Found" body**, and it certified 74
dead targets as healthy.

---

## Results

| # | Check | Findings | Fixed |
|---|---|---|---|
| 1 | Relative Markdown links resolve | **0 broken** of all links across 39 files | — |
| 2 | Backticked repo paths exist | 18 flagged → **4 real**, 14 correct-by-context | 3 |
| 3 | `docs/README.md` catalog completeness | **2 living references unlisted** | 2 |
| 4 | Counted claims vs reality | **1 stale test count**; tool counts correct | 1 |
| 5 | External URLs resolve to a real page | **9 dead of 147**, 8 in the safety-critical spec | 9 |
| 6 | Superseded claims carry a forward pointer | **2 rows asserting overturned figures** | 2 |
| 7 | Cross-doc contradiction on the realised-P&L rule | **0** | — |

**Total: 25 defects found, 25 fixed.** (17 from the checks above; §8's re-anchoring
accounted for 8 more stale references once it stopped being "accepted".) One follow-up
gate shipped — `tests/test_docs_claims.py`, §9.

---

## 1. Relative links — clean

All `[label](path)` targets that point at repository paths resolve. This is the one check
that came back clean on the first run, and it is worth saying plainly rather than
burying: internal linking in this repo is in good shape.

`docs/plans/**` targets are excluded by design — the directory is git-ignored and those
paths are pointers into the local + Drive archive, not repo files.

## 2. Backticked repo paths — 3 real of 18 flagged

The convention (`CLAUDE.md` → Pointers) is that a backticked path is a *literal pointer*,
not a `@import`. The checker flagged 18 such paths that do not exist. Triaged:

- **14 are correct by context.** `claudia/app.py` appears in dated audits from before the
  Phase 11 Chainlit cutover, where it is an accurate historical statement; `SECURITY.md`
  cites it in a sentence that says it *was removed*; `docs/api-reference.md`'s
  `docs/foo.md` is an illustrative example inside prose about the backtick convention
  itself. None is a defect. A checker that reported these as failures would be the
  cry-wolf failure this project has already fixed once in `order_flow._values_match`.
- **3 are real**, all in `docs/startup-flow.md` — see §8, where the finding is larger than
  a path.
- **1 more is real and is from today.** `docs/audits/security-audit-2026-08-05.md` cites
  "four tests in `tests/test_store.py`". That file exists in **ibkr_core_mcp**, where the
  fix landed — but read from a claudia_ui document the bare path resolves to claudia_ui's
  suite, which has no such file. The repo is named two paragraphs earlier, so a careful
  reader recovers; a reader jumping to the sentence does not. Qualified in place. Worth
  noting that this is a *same-day* record: cross-repo path ambiguity is a live habit, not
  a legacy artifact.

## 3. Catalog completeness — 2 unlisted

`CLAUDE.md` describes `docs/README.md` as "every doc in `docs/`, categorized". Fourteen
files were absent from it. Twelve of those are excluded *deliberately and in writing* by
the catalog itself ("Browse directly rather than looking for an index entry here" for
audits; "Plus two dated research docs" for `docs/panel/`), so they are not defects.

Two are: **`panel/component-model-reference.md` (745 lines)** and
**`panel/data-surfaces-reference.md` (616 lines)** are living references — the second is
the document the live dashboard was built from and is pointed at directly by `CLAUDE.md` —
and the catalog's Panel table listed only two of the four. `docs/panel/README.md` indexes
all four correctly, so the gap was in the top-level catalog only. **Both added.**

## 4. Counted claims

| Claim | Where | Truth | Verdict |
|---|---|---|---|
| 44 tools (40 core + 4 scraper) | `CLAUDE.md` | `len(TOOL_DEFINITIONS) == 44` | correct |
| 5 local + 3 proposal tools | `CLAUDE.md` | 5 and 3 | correct |
| "1,154 tests as of 2026-08-05" | `CLAUDE.md` | 1,154 passed, 4 skipped, 1,158 collected | correct as *passed* |
| **"757 unit tests"** | **`README.md`** | **1,154** | **stale — fixed** |

The historical counts in `docs/project-status.md` (633, 704, 757, 835, 960, 1,010, 1,046…)
are dated rows recording the count on their date. Correct as written; not touched.

## 5. External URLs — 9 dead, 8 of them in the order-staging spec

147 distinct URLs across the living docs (dated audits and plans excluded — a URL that
worked when a point-in-time record was written is part of the record). Each was fetched
and graded on **body content**, not status code.

| Dead URL | Cited in | Repointed to |
|---|---|---|
| `…/cpapi-v1/#place-order` | `order-api-reference.md` | `…/v1/endpoints/orders/place-order.md` |
| `…/cpapi-v1/#modify-order` | `order-api-reference.md` | `…/v1/endpoints/orders/modify-order.md` |
| `…/cpapi-v1/#cancel-order` | `order-api-reference.md` | `…/v1/endpoints/orders/cancel-order.md` |
| `…/cpapi-v1/#order-status` | `order-api-reference.md` | `…/v1/endpoints/order-monitoring/order-status.md` |
| `…/cpapi-v1/#live-orders` | `order-api-reference.md` | `…/v1/endpoints/order-monitoring/live-orders.md` |
| `…/web-api-v-1-0-documentation/…/order-status-value.md` | `order-api-reference.md` | `…/v1/endpoints/order-monitoring/order-status-value.md` |
| `…/web-api-v-1-0-documentation/…/order-status.md` | `order-api-reference.md` | `…/v1/endpoints/order-monitoring/order-status.md` |
| `…/cpapi-v1/#tickle` | `connectivity.md` | `…/v1/endpoints/session/ping-the-server.md` |
| `…/cpapi-v1/` | `README.md` | `https://ibkrcampus.com/docs/web-api/` |

Two things about this finding are worth stating rather than glossing:

**It is a job left half-done earlier in the same session.** 129 of these retired
`cpapi-v1` anchors were repointed in `ibkr_core_mcp`. Nobody checked whether claudia_ui's
own docs carried the same dead form. They did — nine of them, eight in
`order-api-reference.md`, which `CLAUDE.md` names as the full spec for the safety-critical
order path.

**The old form fails silently in the worst way.** `…/cpapi-v1/#place-order` returns
**HTTP 404** at `interactivebrokers.com`, but the sibling form on `ibkrcampus.com` returns
**200 while dropping the fragment** — so a reader clicking it lands on a real page that is
not the cited one and has no signal anything went wrong.

**Targets were verified for content, not just liveness.** Every replacement was fetched
and checked to carry the claim being cited. The `order-status-value.md` target lists
`Submitted`, `PreSubmitted`, `Filled`, `PendingSubmit`, `Inactive`, `WarnState`,
`Cancelled`, `PendingCancel`, `PreCancelled` — and **does not contain `ApiCancelled`**,
which is exactly what `order-api-reference.md` asserts when it refuses to treat that value
as proof of a cancel. That safety claim is therefore now live-re-verified, not merely
re-linked. `order-status.md` carries the documented 503 behaviour; `ping-the-server.md`
carries the tickle/session semantics.

The 11 remaining non-resolving URLs are checker noise and were left alone: `localhost`
services, `{port}` / `{_TV_DEBUG_PORT}` templates in prose, Google OAuth **scope
identifiers** (`https://www.googleapis.com/auth/drive` is an identifier, not a page), and
one `https://…/x.png` ellipsis example.

## 6. Superseded claims — the convention has no retraction mechanism

`docs/project-status.md` and `docs/audits/**` follow a stated rule: dated records are
**not retroactively edited**. That rule is right — rewriting history to look correct is
how a project loses the ability to learn from itself. But it has a gap: when a later day
overturns a figure, the earlier row keeps asserting it in the present tense, and a reader
who lands there first has no signal.

Two rows were doing exactly that. Both now carry a **⚠️ marker naming what was overturned
and pointing forward**, with the row body left untouched:

- **2026-08-04, "Ledger `realizedpnl` SETTLED"** — asserted the window is "a calendar day"
  and that the unobserved reset instant "changes no label, so it is a residue, not a gap".
  Both false: the roll is late-ET-evening at a varying hour, and it does change what the
  tile shows. The same row states a **≈−252.60** CRM counter-figure as "measured" when it
  was a projection; the statement arrived at −2,810.47, matching the ledger to the cent.
- **2026-08-05, "D5 closed … 239.10"** — the residue was an artifact of comparing a
  CL-only reconstruction against an account-wide figure, over 4 of 8 fills. Reconciled
  whole it is **0.00**, and the prior-settlement hypothesis it rested on is disproved.

**This is the finding I would keep if I could keep only one.** Both retracted claims were
written confidently, with evidence tables, and both survived because nothing in the
process re-reads yesterday's conclusions. The marker is a patch, not a fix.

## 7. Cross-doc contradiction on the realised-P&L rule — clean

The rule that has bitten this project twice — `SUM(fifo_pnl_realized)` over **all** trades,
no open/close filter, `flex_lot` never summed instead — is stated identically in
`CLAUDE.md`, `docs/flex-query-setup.md`, `docs/trading-data-reference.md` and
`docs/project-status.md`. No drift.

## 8. `docs/startup-flow.md` was anchored to a deleted file — now re-anchored

`CLAUDE.md` points at this doc to "diagnose startup failures". Six of its phase sections
cited `claudia/app.py` with **line numbers** — a file removed in the Phase 11 cutover.

The doc already carried a mapping note for this, which promised that "the equivalent Panel
code carries `# app.py:NNN parity` comments". **The audit checked. It does not.**
`panel_app.py` carries ten parity comments, all of the form "parity with the removed
app.py", none with a line number. So a reader following `app.py:463–480` had nothing to
match against, and the note written to rescue them made a promise the code never kept.

**This section originally read "accepted, not fixed"** — the judgement being that
re-anchoring was a rewrite rather than an audit correction. That judgement was reversed
within the hour, and the reason is worth recording: **the moment the check became a gate
(§9), "accepted" stopped being available.** A red test is not a documented exception, it
is a broken build, and leaving one is how a gate gets weakened later to make it pass.

**Fixed.** All six now point at `claudia/panel_app.py` **by function name** —
`_send_action_buttons()`, `_maybe_background_flex_sync()`, `_send_opening_status()`,
`_init_session` — with the old app.py location kept as parenthetical history. Two further
references (`claudia/app.py:440-448`, and the `on_chat_start` prose) were fixed with them.

**By function, not by line range, deliberately.** A line number is a claim that rots on the
next refactor with nothing to detect it — precisely how this doc broke. A function name
breaks loudly, because §9's gate fails when the path stops existing.

## 9. Follow-up shipped: `tests/test_docs_claims.py`

The audit's own finding is that unenforced doc conventions decay silently, so the one
mechanically checkable shape is now a gate: **a living doc may not backtick a repo path
that does not exist.** Dated records and `docs/plans/**` are exempt by the same convention
that makes them point-in-time; two path exemptions are listed explicitly with reasons, so
a real defect cannot be parked there unremarked. It ships with a guard-the-guard test —
if the corpus discovery ever returns a near-empty list the assertions become vacuous, and
that is the `feedback-mocks-weaker-than-dependencies` failure one layer down.

**It catches one of the three shapes in the assessment below, and no more.** A promise
about comment *formatting*, and a projection labelled "measured", are not machine-
detectable. Reading this gate as coverage of the class would be the exact error it exists
to flag.

---

## Honest assessment

**What this audit actually found.** Sixteen defects, of which one class matters more than
the rest: **three separate cases where a document asserted something checkable about the
code or the world, and nobody had checked.** The `# app.py:NNN parity` promise. The
"calendar day" window. The ≈−252.60 "measured" figure. Each was written in good faith,
each reads authoritatively, and each was false at the moment it was written or shortly
after. Broken links and stale counts are hygiene; these are the ones that mislead.

**The pattern across the whole session, stated plainly.** Four times today a check was
weaker than the thing it was checking, and all four passed green: a URL probe grading on
status against a site that 200s missing pages; test fixtures missing a field the real
payload carries; a FIFO reconstruction that assumed every sell closes; a query netting
across contract expiries. None was caught by the test suite. All four were caught by
running against reality, and two of them only because the user pushed back on a result
that smelled wrong. **The suite proves nothing broke; it does not prove the thing is
right.**

**What I got wrong today and had to retract.** The 239.10 residue and the settlement
hypothesis built on it — both mine, both presented with arithmetic that looked like
evidence. The "calendar day" boundary, which I then *replaced with a second wrong claim*:
a specific "between 21:55 and 22:31 ET" that went into **user-visible dashboard text** on
the strength of a bracket that died one measurement later. That one is the worst of the
day, because a disclosure a trader can check against their own clock is worse than a vague
one when it is wrong. It was live for roughly half an hour.

**What is genuinely in good shape.** Internal linking (0 broken). The realised-P&L rule,
consistent across four documents after having been wrong twice. Tool counts. The
`docs/panel/` reference set. The convention of citing a source URL for every factual claim
— which is precisely *why* this audit could find nine dead ones: a project that cites
nothing has no dead citations to find.

**What I would not claim.** That the docs are now correct. This audit proves nine specific
URL targets resolve and carry their claims, that 39 files have no broken internal links,
that four counted claims match reality, and that one cross-doc rule does not contradict
itself. It does **not** prove that the prose is accurate — most claims in these documents
are not mechanically checkable, and I read for contradiction rather than re-deriving each
one from the code. The honest summary is: **the checkable surface is now clean, and the
unchecked surface is unmeasured.**

**The one structural gap left open.** There is no mechanism that re-reads a conclusion when
later evidence overturns it. Both retractions here were found by accident — I was chasing
an unrelated residue. The ⚠️ markers help the next reader; they do nothing for the next
writer.

**On the gate in §9, and its limits.** Turning the one checkable shape into a test is the
right response to "unenforced conventions decay", and it had an immediate effect: it made
§8's "accepted, not fixed" untenable within the hour, which is what a gate is for. But it
covers **one of three shapes**. The other two — a promise about how code comments are
*formatted*, and a projection wearing the word "measured" — cannot be detected by any test,
because both are well-formed sentences that happen to be false. The only control on those
is the writer running the check before writing the claim, and the discipline of never
using "measured", "verified" or "confirmed" for something derived. That is now recorded as
a standing rule rather than left to memory.

**The sharpest single lesson.** Claim #2 was not merely wrong — it asserted its own
unverifiability ("the reset instant… no single session can produce"). That sentence closed
the question. It was written without attempting the observation, and a 37-read watch
settled it the same evening. **A claim that something cannot be checked is itself a claim,
and it needs the same evidence as any other.**
