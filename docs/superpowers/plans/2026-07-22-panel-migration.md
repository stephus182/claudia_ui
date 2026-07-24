# ClaudIA Chainlit → Panel Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.
>
> **This is a living document.** Phases 1-2 are fully detailed (bite-sized, TDD, real
> verified code) because their APIs were confirmed against the actually-installed Panel
> 1.9.3 in this worktree's `.venv` before being written down. Phases 3-11 are grounded,
> scoped outlines, not yet bite-sized — each one gets a detailing pass (added directly to
> this file) immediately before it starts, informed by what the prior phase actually
> revealed. Do not treat an outline phase's absence of code as permission to invent it —
> stop and detail it first. See "Living-document protocol" near the end.

**Goal:** Replace Chainlit with Panel (HoloViz) as ClaudIA's UI framework, preserving every
Hard Rule and safety gate in `CLAUDE.md` unchanged, without disrupting the still-unfinished
Chainlit-based work on `main`.

**Architecture:** Build the new Panel surface as new, additively-named files alongside the
existing Chainlit ones inside this same worktree/branch. Decouple `ClaudIAAgent`'s
safety-critical core loop (streaming, tool routing, the hardcoded safety block, order-proposal
parsing) from Chainlit via a small `MessageSink` protocol, so the exact same, already-tested
loop serves both UIs during the transition. Land one phase at a time, each producing working,
independently-testable software; cut over (remove the Chainlit path) only in the final phase.

**Tech Stack:** Panel 1.9.3 (`panel[fastapi]`), Bokeh (Panel's dependency, provides
`Document.add_next_tick_callback`), FastAPI/Starlette (existing), Anthropic SDK (unchanged),
`ibkr_core_mcp` (unchanged, read-only per Hard Rule 5), pytest/pytest-asyncio/pytest-mock
(existing conventions).

---

## Grounding & sources

Everything in this plan traces to one of:

1. **`docs/plans/2026-07-19-ui-framework-research.md`** — the 6-candidate framework
   comparison, requirements bar (8 items derived from a full `claudia/*.py` audit), and the
   Panel recommendation. Cited inline below as **[research]**.
2. **`docs/plans/2026-07-22-panel-shadow-dom-live-test.md`** — live Playwright-verified
   Shadow-DOM styling test (not assumed) against a real running Panel 1.9.3
   `ChatInterface`. Cited as **[shadow-dom-test]**.
3. **`docs/plans/2026-07-22-panel-implementation-kickoff-prompt.md`** — the kickoff prompt
   that commissioned this plan; carries forward the 7 Hard Rules and the confirmed
   technical findings. Cited as **[kickoff]**.
4. **A fresh, full-file audit of the current `claudia/*.py` codebase** (all 10 modules,
   4,636 lines, read in full during this planning pass — not from the `CLAUDE.md` summary
   alone). Cited as **[code-audit]** with exact file:line references.
5. **Direct introspection of Panel 1.9.3, installed in this worktree's `.venv`** — every
   Panel API signature this plan relies on for Phases 1-2 was checked with
   `inspect.signature`/`inspect.getsource` against the real installed package, not taken on
   the research doc's paraphrase alone. Cited as **[verified-live]**.

Anywhere this plan states a Panel behavior without one of these five tags, treat it as
unconfirmed — stop and verify before implementing, per this project's own "API Docs First"
convention (`CLAUDE.md`).

---

## Current-state audit: what actually needs to change

This matters more than it looks — the migration surface is much smaller than "10 files, all
touched." A full read of every module in `claudia/` (not just `CLAUDE.md`'s architecture
diagram) found:

**Zero Chainlit dependency — do not touch, import as-is:**
- `claudia/conversation_store.py` (392 lines) — pure `sqlite3`, no `chainlit` import anywhere.
- `claudia/session_reporter.py` (204 lines) — pure stdlib, no `chainlit` import.
- `claudia/gdrive_sync.py` (365 lines) — pure Google API client + `sqlite3`, no `chainlit` import.
- `claudia/execution_listener.py` (271 lines) — pure `asyncio` + `ibkr_core_mcp.streaming`, no `chainlit` import.
- `claudia/context_loader.py` (189 lines) — pure `hashlib`/`watchdog`, no `chainlit` import.
  **Important correction to a plausible assumption:** the thread-to-event-loop bridge
  (`contextvars.copy_context()` + `loop.call_soon_threadsafe(...create_task(...))`)
  that `CLAUDE.md` and **[research]** describe does **not** live inside this file. It lives in
  `claudia/app.py:266-285`, in the `_on_doc_change` closure passed to
  `ContextLoader.start_watching()`. `ContextLoader` itself only calls a plain
  `Callable[[str, str], None]` — **[code-audit]**. The Panel-native replacement task is
  entirely about that app.py-side closure, not this file.

**One call site each — small, isolated change:**
- `claudia/status.py:236` — `ConnectivityChecker._send_alert()` has the file's only
  `import chainlit as cl`, used once to push a connectivity-change alert into chat
  **[code-audit]**. Everything else in this 246-line file (polling, soft-recovery, TCP/HTTP
  checks) is framework-agnostic.

**Genuinely UI-coupled, need real porting:**
- `claudia/agent.py` (908 lines) — `import chainlit as cl` used at exactly 4 call sites:
  the max-tokens-truncation message, the per-tool-call `cl.Step` indicator, `cl.make_async`
  wrapping a blocking `toolkit.execute` call, and the final response message
  **[code-audit]**. The other ~900 lines — the streaming loop, the hardcoded safety block,
  order-proposal/cancel/modify JSON parsing, prompt-cache breakpoints, local tool dispatch,
  the SSRF-guarded `fetch_web_page` — have zero Chainlit coupling. This is why Phase 1 below
  is a pure decoupling refactor, not a rewrite.
- `claudia/order_flow.py` (681 lines) — heavy `cl.Action`/`cl.Message` usage; this is the
  safety-critical message-with-buttons pattern (Hard Rule 7) and needs a faithful,
  carefully-reviewed port (Phase 3).
- `claudia/app.py` (940 lines) — the Chainlit entry point itself: route registration
  (`_fix_route_priority`), 9 `@cl.action_callback` handlers, lifecycle hooks
  (`on_chat_start`/`on_message`/`on_stop`), `cl.user_session`, ~15 `cl.Message(...).send()`
  call sites. This is the file `claudia/panel_app.py` replaces piece by piece.
- `claudia/tradingview.py` (440 lines) — `TradingViewBridge` (sidecar process management,
  MCP stdio client) has **no** Chainlit dependency; only `render_pinescript()` and its two
  `@cl.action_callback`s (`copy_pinescript`, `inject_pinescript`) at the bottom of the file
  do **[code-audit]**. Lower stakes than order-staging — scheduled later (Phase 9).

**Test coverage baseline (confirmed by running the suite in this worktree, not assumed):**
`pytest -m "not integration"` → **313 passed**, 0 failures, in this venv before any
migration code is written. `tests/test_agent.py` has 63 tests, none of which currently
exercise `handle_message()`'s full streaming loop end-to-end (only its pure-function
helpers: proposal stripping, system-prompt building, history conversion, cache markers).
This is a real, pre-existing gap that Phase 1 closes as a direct consequence of the
refactor it's making (see Task 1.3) — not scope creep.

---

## Target architecture & file structure

New files (created incrementally, phase by phase — none of these exist yet):

| File | Responsibility |
|---|---|
| `claudia/message_sink.py` | `MessageSink`/`ToolStepHandle` protocols + `ChainlitMessageSink` (preserves exact current Chainlit UI behavior) |
| `claudia/panel_sink.py` | `PanelMessageSink` — the Panel-side `MessageSink` implementation, evolves phase by phase |
| `claudia/panel_app.py` | New Panel entry point: FastAPI app, `ChatInterface` factory, session wiring — the eventual replacement for `claudia/app.py` |
| `claudia/panel_order_flow.py` | Phase 3: Panel port of `order_flow.py`'s message-with-buttons rendering (the old file stays untouched and in use by the Chainlit app until cutover) |

Modified files (existing, changes scoped per phase below):
`claudia/agent.py` (Phase 1: sink injection), `claudia/status.py` (Phase 6: alert push),
`claudia/tradingview.py` (Phase 9: action-callback port), `pyproject.toml` (Phase 1: new
dependency).

Untouched until cutover (Phase 11): `claudia/app.py`, `claudia/order_flow.py`'s Chainlit
renderers. Never touched at all: `conversation_store.py`, `session_reporter.py`,
`gdrive_sync.py`, `execution_listener.py`, `context_loader.py` (per the audit above — these
are already framework-agnostic).

**Why a `MessageSink` protocol instead of duplicating `agent.py`:** the streaming loop,
safety block, and proposal parsing in `agent.py` are safety-critical (Hard Rules 1, 3, 6) and
already covered by 63 tests. Forking the file into a near-identical `panel_agent.py` would
mean two copies of the hardcoded safety block to keep in sync by hand — exactly the kind of
drift Hard Rule 3 exists to prevent. A ~15-line seam around the 4 actual UI call sites lets
both frontends run the identical, identically-tested core during the transition. This is a
real design decision, not mechanical porting — flagged here rather than silently applied so
it can be reviewed before Phase 1 starts.

---

## Risks & open issues (consolidated from research + this plan's own findings)

1. **Panel's chat sub-components are the newest, least-proven part of the library**
   **[research]** — issue [holoviz/panel#6291](https://github.com/holoviz/panel/issues/6291)
   (open as of 2026-07-19/22) tracks: a combined file-upload+textarea input still missing, Enter-to-send/Shift+Enter
   keyboard handling needing work, and — directly relevant to Phase 4 below — a "Status"
   component for agent/tool intermediate steps (the `cl.Step` equivalent) still being
   migrated in. **Action:** re-check this issue's current state at the start of Phase 4,
   since it was open 3 days before this plan was written and may have shipped since.
2. **No independent third-party production track record for Panel-as-a-chatbot**
   specifically **[research]** — strong official examples (`panel-chat-examples`), no found
   outside "we run this in production" account either way. Informational; doesn't block
   any phase, but means Phases 2-4 (the chat surface itself) carry more first-mover risk
   than Phases 9-10 (dashboard/charting, where Panel has abundant production history).
3. **Panel 2.0/3.0 API transition** — Panel 2.0 (dual legacy/modern API) targeted Q2 2026,
   3.0 (legacy removal) targeted 2027 **[research]**. This plan targets the modern
   `panel.ui`/`panel-material-ui` namespace throughout, not legacy widgets, per **[kickoff]**.
4. **Shadow-DOM styling constraint is real but scoped and already resolved** — a page-level
   stylesheet (the current `custom.css` pattern) does not reach chat message content
   (confirmed to fail, not assumed) **[shadow-dom-test]**. Two working mechanisms exist
   instead: inline `style="..."` attributes (per-element — P&L color, table cells) and
   Panel's `stylesheets=[...]` component parameter with `:host`/`:host *` selectors
   (broader theming — fonts). Both confirmed working through all 7 nested shadow levels.
   Phase 7 uses both; do not carry the page-level `custom.css` approach forward.
5. **`asyncio.to_thread` replacing `cl.make_async`** — Phase 1 (Task 1.3) replaces
   `cl.make_async(fn)(*args)` with `asyncio.to_thread(fn, *args)`. This is a standard-library
   substitution independent of Panel (works on any running asyncio loop; Tornado's `IOLoop`,
   which Panel's server runs on, has wrapped `asyncio`'s loop directly since Tornado 5) —
   not a Panel-specific claim requiring the same live-verification rigor as the
   Panel-specific APIs below, but flagged here for transparency since it wasn't
   independently re-tested in this session.
6. **IBKR gateway is a live, fragile external system** — per prior project memory, IBKR
   sessions don't tolerate repeated logins and 2FA is unreliable; losing a live gateway
   session is high-cost. **Any plan step that implies exercising the app against a live
   IBKR gateway is marked "manual verification" below and must not be automated /
   run unattended by an agent** — a human runs it, with `caffeinate`, per established
   project practice.
7. **This plan's own outline phases (3, 4, 5, 7-11) are not yet bite-sized** — see
   "Living-document protocol" below. Treat their absence of exact code as an instruction to
   detail them next, not as a gap to fill in from memory during implementation.
8. **Hard Rule 2 (never log/expose `ANTHROPIC_API_KEY`)** — verified not at risk anywhere in
   this plan: `AsyncAnthropic()` continues to read the key from the environment exactly as
   it does today (`agent.py`'s constructor is untouched in this respect across all of
   Phases 1-2), and no new logging of config/env values is introduced by `panel_app.py`.
   No dedicated task exists for this because nothing in the plan changes it — flagged here
   so it was verified, not merely un-mentioned.
9. **⚠️ FINDING (live test 2026-07-23) — LLM intermittently fails to emit the proposal
   block, then confabulates that it did. DEFERRED to its own spec — do NOT hot-fix.**
   During live order-flow testing on the Panel app, BUY (order-proposal) and MODIFY
   (order-modify-proposal) staged correctly through Gate 1/Gate 2 → real IBKR orders
   (orderIds `1986940574` placed, then modified $100→$150 — both independently verified on
   the gateway). But the subsequent CANCEL (order-cancel-proposal) and a later SELL
   (order-proposal) rendered **no staging button**, and ClaudIA then confabulated ("I've
   already staged the cancel proposal in my previous message") — trapping the user with no
   clickable button across repeated retries.
   - **Root cause, proven not guessed:** the `decisions` audit table (written only *after* a
     block is parsed + dispatched at `agent.py:678-689`) has rows for BUY (msg 512,
     `trade_proposed`) and MODIFY (msg 519, `trade_modify_proposed`) but **none** for the
     failed CANCEL (msg 524/528) or SELL (msg 530/532/534). A *parse* failure was ruled out:
     a malformed-JSON block is returned intact in `display_text` (`agent.py:220-222`), yet
     **no** stored message contains a raw ` ```order ` fence. So the block was **never
     emitted** — the LLM wrote prose claiming it staged a proposal. Dispatch was correctly
     skipped; **fails safe/closed — no unwanted order.**
   - **NOT a Panel bug / NOT introduced by this migration:** `agent.py`, the block-stripper,
     and the system prompt are all framework-agnostic and shared with Chainlit; the Panel
     render path is *proven working* by the BUY and MODIFY that succeeded. Same code would
     fail identically in Chainlit — this is a pre-existing agent-reliability issue the live
     Panel session happened to surface.
   - **Candidate mechanism (partial):** replayed history stores the *stripped*
     `display_text` (`agent.py:660-661`), so the model never sees its own successfully-emitted
     blocks — only prose-only "proposals." This may self-reinforce turn-over-turn (1st
     proposal clean → later ones degrade), which fits the observed timeline (BUY ✅, MODIFY ✅,
     then CANCEL ❌, SELL ❌).
   - **SECOND SYMPTOM, SAME ROOT CAUSE — and now PROVEN via A/B, not hypothesized
     (live test 2026-07-23):** later in the *same* long conversation, "What are my current
     positions and today's P&L?" returned a **fabricated all-green portfolio** (TSLA/NVDA/EEM
     positions that don't exist, invented +$1,118 unrealized) with **no tool call at all**
     (DB: zero `role='tool'` rows between the question msg 535 and answer msg 536; real
     account is 5 positions all red, ≈ −$10.4k unrealized, verified on gateway). It even
     reused the *real* NLV from an earlier turn to make the fake look legit — the exact
     documented DATA INTEGRITY fabrication class (`project-status.md`: watchlists, TSLA quote,
     retry-fabrication). **Decisive A/B:** the identical question in a **fresh session**
     (page reload → empty history) called the real `get_positions` + `get_live_pnl` tools
     (DB msgs 538/539) and returned **exact gateway-matching data** for all 5 positions.
     Same code, same prompt, same gateway — only the conversation length/pollution differed.
     This **proves context degradation** (not a broken path) as the root cause, and unifies
     both symptoms: deep in a long/confusing conversation the model stops doing the real
     action (emit block / call tool) and substitutes plausible text; early queries in every
     session work. The confabulation loop from the proposal-block symptom (piling up "I've
     already staged it" messages) plausibly *accelerates* the degradation.
   - **Recommended fix (for a future spec, TDD + review — this touches safety-critical
     `agent.py`):** (a) a deterministic guardrail — when `display_text` shows proposal intent
     but no block parsed, surface an honest "didn't actually stage — retrying" instead of
     letting the model confabulate success; (b) address the history mechanism so the model
     retains that it emitted a block. Needs a proper failing-test harness for the non-block
     case before any change.
10. **⚠️ FINDING (live test 2026-07-23) — first-ever live FUT test surfaced TWO order-path
    bugs in `order_flow.py` (shared code, not Panel-specific). Full spec:
    [`docs/2026-07-23-futures-order-field-8089-bug.md`](../../2026-07-23-futures-order-field-8089-bug.md).**
    Test order `BUY 1 ES SEP2026 LMT 6000 GTC` — staging/gates/`place_order` all worked;
    IBKR rejected the order (`order_id: "0"`, not placed, verified on gateway).
    - **Bug A (FUT-specific) — ROOT CAUSE PROVEN via `whatif` isolation (2026-07-23, user
      approved):** IBKR rejects with `"Can not contain field # 8089"`. Field #8089 is
      **`extOperator`** — the gateway rejects it with *any non-empty value* (including IBKR's
      own docs example `person1234`), while `manualIndicator` alone is **accepted** (full
      margin preview returned, also validating the rest of the FUT body as-is). The initial
      missing-`secType`/`conidex` hypothesis was **disproven** (adding them changes nothing).
      Probable docs reconciliation: `extOperator`'s "Required\*" scopes to
      institutional/multi-operator accounts; individual accounts reject it. **Proven fix:
      keep `manualIndicator: True`, drop `extOperator`** (`order_flow.py:277-284` and modify
      mirror `:641-644`; update `client.py` docstring + field-spec comment). Ten-variant
      isolation table in the bug doc.
    - **Bug B (all instruments, higher priority):** a **rejected order is reported as "Order
      staged successfully"** (`order_flow.py:302-306`) — the success message is built
      unconditionally after `place_order` returns, and IBKR returns rejections as a
      200-with-error-payload (`action: order_submit_issue`, `order_id: "0"`), not an
      exception, so it's never detected. Mislabels any rejection as success. Fix: inspect
      `result` for rejection markers before declaring success (cancel/modify too).
    - **Disposition:** both fail safe on the account (nothing placed), touch the
      safety-critical order path → proper TDD + review, not a live hot-patch.
    - **✅ FIXED 2026-07-23** — commits `a16599f` (both bugs, TDD) + `89a14bb` (review
      hardening: cancel-docstring cross-ref, 10-case classifier contract test, order_id
      spelling in decision metadata), full subagent cycle, 371 unit tests green.
      **Pending: live FUT re-test through the gate chain** on the next gateway session
      (gateway was shut down before it could run).

---

## Phase index

| # | Phase | Status in this document |
|---|---|---|
| 1 | Decouple `ClaudIAAgent` from Chainlit (`MessageSink`) | **Fully detailed** |
| 2 | Panel walking skeleton (FastAPI mount, per-session agent, real chat loop) | **Fully detailed** |
| 3 | Order-staging button pattern (safety-critical) | Outline |
| 4 | Tool-call Status indicator (`cl.Step` equivalent) | Outline |
| 5 | Session lifecycle completeness (GDrive, doc hot-reload, opening status, Flex) | Outline |
| 6 | Background services bridge (`ConnectivityChecker` alert push, `/api/status`) | Outline |
| 7 | Styling (status bar, inline P&L color, `stylesheets=[...]` theme, avatar/logo) | Outline |
| 8 | File upload (TradingView screenshots) | Outline |
| 9 | TradingView action buttons + sidecar tool merge | Outline |
| 10 | Dashboard + candlestick charting (new capability) | Outline |
| 11 | Cutover (parity check, decommission Chainlit, docs) | Outline |

---

## Phase 1: Decouple `ClaudIAAgent` from Chainlit

**Goal:** Introduce a `MessageSink` protocol; `ClaudIAAgent` depends on it instead of
importing `chainlit` directly. Zero behavior change to the existing Chainlit app — every
existing test still passes, `chainlit run claudia/app.py` still works exactly as today.

### Task 1.1: Add the Panel dependency

**Files:**
- Modify: `pyproject.toml`

- [x] **Step 1: Add `panel[fastapi]` to `dependencies`**

In `pyproject.toml`, in the `[project]` `dependencies` list, add (keep `chainlit>=2.0` too —
both coexist until Phase 11):

```toml
dependencies = [
    "chainlit>=2.0",
    "panel[fastapi]>=1.9",
    "anthropic>=0.28",
    # ibkr_core_mcp is installed separately: pip install -e ../ibkr_core_mcp
    "watchdog>=4.0",
    "python-dotenv>=1.0",
    "mcp>=1.27,<2",
    "requests>=2.31",
    "html2text>=2024.2",
]
```

- [x] **Step 2: Reinstall and verify**

Run: `pip install -e ".[dev]"`
Then: `python -c "import panel; print(panel.__version__)"`
Expected: prints `1.9.3` (or newer — if a newer version installs, note the actual version
here in this file before continuing, since Phase 2's verified signatures were checked
against 1.9.3 specifically).

- [x] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: add panel[fastapi] dependency for Panel migration"
```

### Task 1.2: `MessageSink` protocol + `ChainlitMessageSink` adapter

**Files:**
- Create: `claudia/message_sink.py`
- Create: `tests/test_message_sink.py`

- [x] **Step 1: Write the failing tests**

```python
"""Tests for ChainlitMessageSink — preserves exact current Chainlit UI behavior
behind the MessageSink protocol that ClaudIAAgent depends on."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.message_sink import ChainlitMessageSink


@pytest.mark.asyncio
async def test_send_message_calls_cl_message_send():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl") as mock_cl:
        mock_cl.Message.return_value.send = AsyncMock()
        await sink.send_message("hello")
        mock_cl.Message.assert_called_once_with(content="hello")
        mock_cl.Message.return_value.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_max_tokens_warning_uses_system_author():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl") as mock_cl:
        mock_cl.Message.return_value.send = AsyncMock()
        await sink.send_max_tokens_warning()
        _, kwargs = mock_cl.Message.call_args
        assert kwargs["author"] == "System"
        assert "truncated" in kwargs["content"].lower()


def test_tool_step_returns_cl_step_with_name_and_type_tool():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl") as mock_cl:
        sink.tool_step("get_positions")
        mock_cl.Step.assert_called_once_with(name="get_positions", type="tool")


@pytest.mark.asyncio
async def test_send_order_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10}
    with patch("claudia.order_flow.render_order_proposal", new=AsyncMock()) as mock_render:
        await sink.send_order_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")


@pytest.mark.asyncio
async def test_send_cancel_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.order_flow.render_cancel_proposal", new=AsyncMock()) as mock_render:
        await sink.send_cancel_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")


@pytest.mark.asyncio
async def test_send_modify_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.order_flow.render_modify_proposal", new=AsyncMock()) as mock_render:
        await sink.send_modify_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_message_sink.py -v`
Expected: `ModuleNotFoundError: No module named 'claudia.message_sink'`

- [x] **Step 3: Implement `claudia/message_sink.py`**

```python
"""Message-sink abstraction decoupling ClaudIAAgent's core loop from any specific UI
framework.

ClaudIAAgent depends only on the MessageSink protocol below, not on chainlit or panel
directly — migrating the UI framework changes which concrete sink is constructed at
session start, not the safety-critical loop itself (streaming, tool routing, the
hardcoded safety block, order-proposal parsing). ChainlitMessageSink here preserves
today's exact Chainlit behavior; see claudia/panel_sink.py for the Panel counterpart.
"""

from __future__ import annotations

from typing import Protocol

import chainlit as cl


class ToolStepHandle(Protocol):
    """Mutable handle for one in-flight tool call's displayed input/output."""

    input: str
    output: str

    async def __aenter__(self) -> ToolStepHandle: ...
    async def __aexit__(self, exc_type, exc, tb) -> bool | None: ...


class MessageSink(Protocol):
    """Everything ClaudIAAgent needs from a UI to render one turn's output."""

    async def send_message(self, text: str) -> None:
        """Send a plain assistant-authored text message."""
        ...

    def tool_step(self, name: str) -> ToolStepHandle:
        """Return an async-context-manager tool-call indicator for tool `name`."""
        ...

    async def send_max_tokens_warning(self) -> None:
        """Notify the user a response was truncated at the token limit."""
        ...

    async def send_order_proposal(self, proposal: dict) -> None: ...
    async def send_cancel_proposal(self, proposal: dict) -> None: ...
    async def send_modify_proposal(self, proposal: dict) -> None: ...


class ChainlitMessageSink:
    """MessageSink backed by Chainlit — preserves today's exact UI behavior unchanged."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def send_message(self, text: str) -> None:
        await cl.Message(content=text).send()

    def tool_step(self, name: str):
        return cl.Step(name=name, type="tool")

    async def send_max_tokens_warning(self) -> None:
        await cl.Message(
            content="_⚠ Response truncated — token limit reached. "
                    "Ask me to continue if the answer is incomplete._",
            author="System",
        ).send()

    async def send_order_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_order_proposal
        await render_order_proposal(proposal, session_id=self._session_id)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_cancel_proposal
        await render_cancel_proposal(proposal, session_id=self._session_id)

    async def send_modify_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_modify_proposal
        await render_modify_proposal(proposal, session_id=self._session_id)
```

- [x] **Step 4: Run to verify pass**

Run: `pytest tests/test_message_sink.py -v`
Expected: `6 passed` (recount, corrected 2026-07-22 during Task 1.2 execution — the test
code above defines 6 test functions, not 7 as an earlier draft of this plan miscounted;
do not add a 7th test to make the number "match", the implementer who actually ran this
correctly refused to do that).

**Verified finding, 2026-07-22 (Task 1.2 execution):** the `with patch("claudia.message_sink.cl")
as mock_cl:` calls above (no explicit `new=`) crash against this repo's actual installed
`chainlit` — its module-level `__getattr__` (`make_module_getattr` in `chainlit/utils.py`)
raises `KeyError` instead of `AttributeError` for unrecognized names, and `unittest.mock.patch`
internally does a `hasattr()` probe on the patch target that trips over this. This is exactly
why `tests/test_order_flow.py` already avoids `unittest.mock.patch` on `cl` and instead does a
manual `_of.cl = mock_cl` / `finally: restore` swap. **Fix:** add `new=MagicMock()` to each of
the 3 affected `patch("claudia.message_sink.cl", new=MagicMock())` calls (the 3 that patch
`cl` directly: `test_send_message_calls_cl_message_send`,
`test_send_max_tokens_warning_uses_system_author`,
`test_tool_step_returns_cl_step_with_name_and_type_tool`) before running Step 2/Step 4 —
`as mock_cl` still binds identically. **This will recur in any later phase that mocks the
`cl` module directly** (flagged for whoever writes Phase 3/9's Panel-port tests, which won't
hit this since they mock `panel`, not `chainlit` — but any *Chainlit*-side test added after
this point should use this pattern or the existing swap-and-restore one, not a bare `patch`).

- [x] **Step 5: Commit**

```bash
git add claudia/message_sink.py tests/test_message_sink.py
git commit -m "feat: add MessageSink protocol + ChainlitMessageSink adapter"
```

### Task 1.3: Inject the sink into `ClaudIAAgent`, remove its direct `chainlit` import

**Files:**
- Modify: `claudia/agent.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_security_regressions.py` — **verified finding, 2026-07-22 (Task 1.3
  execution):** this file has its own independent `_make_agent()` helper (used only by its
  SSRF-guard tests) that also constructs `ClaudIAAgent(...)` directly — missed when this
  plan was first written, which only accounted for `tests/test_agent.py`'s two helpers.
  Needs the identical `sink=MagicMock()` addition or its 12 tests fail once Step 4 lands.
  Grep for `_make_agent` across `tests/` before assuming a given task's helper-update list
  is complete, in any later phase that touches `ClaudIAAgent`'s constructor again.

- [x] **Step 1: Write the failing tests (new behavior via the sink)**

Add to `tests/test_agent.py` (needs `AsyncMock` added to the existing
`from unittest.mock import MagicMock, patch` import, and `pytest` imported):

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
```

```python
# ── handle_message() → MessageSink (Task 1.3) ───────────────────────────────

class _FakeStream:
    """Fakes AsyncAnthropic().messages.stream()'s async-context-manager + async-iterator
    shape, replaying a canned event sequence. Mirrors the SimpleNamespace-based fake-event
    pattern already used by test_log_cache_usage_* above for the same SDK event shapes."""

    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event


def _text_response_events(text: str, stop_reason: str = "end_turn"):
    return [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason=stop_reason)),
    ]


def _make_agent_with_sink(sink=None):
    """Like _make_agent(), but returns (agent, sink) — sink defaults to a fresh MagicMock
    with async methods pre-wired as AsyncMock so callers can assert on them."""
    sink = sink or MagicMock()
    sink.send_message = AsyncMock()
    sink.send_max_tokens_warning = AsyncMock()
    sink.send_order_proposal = AsyncMock()
    sink.send_cancel_proposal = AsyncMock()
    sink.send_modify_proposal = AsyncMock()
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    store.get_history.return_value = []
    loader = MagicMock()
    loader.reload_count = 0
    loader.load_system_prompt.return_value = "# Role\nStub.\n\n# Principles\nStub."
    with patch("claudia.agent.AsyncAnthropic"):
        agent = ClaudIAAgent(
            toolkit=toolkit, store=store, context_loader=loader,
            session_id="test-session", sink=sink,
        )
    return agent, sink


@pytest.mark.asyncio
async def test_handle_message_sends_final_response_via_sink():
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Hello there."))
    )
    await agent.handle_message("Hi")
    sink.send_message.assert_awaited_once_with("Hello there.")


@pytest.mark.asyncio
async def test_handle_message_max_tokens_calls_sink_warning():
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Truncated...", stop_reason="max_tokens"))
    )
    await agent.handle_message("Hi")
    sink.send_max_tokens_warning.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_message_tool_call_uses_sink_tool_step():
    agent, sink = _make_agent_with_sink()
    tool_use_events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="t1", name="get_positions"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
        ),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use")),
    ]
    agent._client.messages.stream = MagicMock(
        side_effect=[
            _FakeStream(tool_use_events),
            _FakeStream(_text_response_events("You hold 100 AAPL.")),
        ]
    )
    agent._toolkit.execute = MagicMock(return_value=("100 AAPL", None))
    step_cm = MagicMock()
    step_handle = MagicMock(input="", output="")
    step_cm.__aenter__ = AsyncMock(return_value=step_handle)
    step_cm.__aexit__ = AsyncMock(return_value=False)
    sink.tool_step = MagicMock(return_value=step_cm)

    await agent.handle_message("What are my positions?")

    sink.tool_step.assert_called_once_with("get_positions")
    assert step_handle.output == "100 AAPL"
    sink.send_message.assert_awaited_once_with("You hold 100 AAPL.")
```

- [x] **Step 2: Update the existing `_make_agent()` / `_make_agent_with_loader()` helpers**

`ClaudIAAgent(...)` will require `sink` once Step 4 lands. Update both existing helpers
(around line 225 and line 567) to pass a mock sink:

```python
def _make_agent():
    """Build a ClaudIAAgent with all dependencies mocked."""
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    loader = MagicMock()
    with patch("claudia.agent.AsyncAnthropic"):
        return ClaudIAAgent(
            toolkit=toolkit,
            store=store,
            context_loader=loader,
            session_id="test-session",
            sink=MagicMock(),
        )
```

```python
def _make_agent_with_loader(loader):
    toolkit = MagicMock()
    toolkit.tools = []
    with patch("claudia.agent.AsyncAnthropic"):
        return ClaudIAAgent(
            toolkit=toolkit,
            store=MagicMock(),  # unused by these tests — no doc_version passed
            context_loader=loader,
            session_id="test-session",
            sink=MagicMock(),
        )
```

- [x] **Step 3: Run to verify failure**

Run: `pytest tests/test_agent.py -v`
Expected: the 3 new tests fail with `TypeError: ClaudIAAgent.__init__() missing 1 required
positional argument: 'sink'` (or similar) — `ClaudIAAgent` doesn't accept `sink` yet. The
pre-existing 63 tests should still pass at this point (the helper changes above make them
pass `sink=MagicMock()`, which the current constructor will reject with an unexpected-kwarg
`TypeError` — confirming the helpers now correctly anticipate the not-yet-changed signature).

- [x] **Step 4: Implement the refactor in `claudia/agent.py`**

Change the imports (remove `import chainlit as cl`, add `asyncio` and the sink types):

```python
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

if TYPE_CHECKING:
    from ibkr_core_mcp import ClaudeToolkit

    from claudia.context_loader import ContextLoader
    from claudia.conversation_store import ConversationStore
    from claudia.message_sink import MessageSink
    from claudia.tradingview import TradingViewBridge
```

Add `sink: MessageSink` to `__init__` (required, placed right after `session_id` — before
the defaulted params, since Python requires non-default params first; both existing call
sites already use keyword arguments, so this is not a breaking change in practice):

```python
    def __init__(
        self,
        toolkit: ClaudeToolkit,
        store: ConversationStore,
        context_loader: ContextLoader,
        session_id: str,
        sink: MessageSink,
        model: str = "claude-opus-4-8",
        extra_tools: list[dict] | None = None,
        tv_bridge: TradingViewBridge | None = None,
        doc_version: str | None = None,
        trade_context: str | None = None,
    ) -> None:
```

In the body, add alongside the other `self._...` assignments:

```python
        self._sink = sink
```

Replace the tool-execution branch (currently `cl.make_async(self._toolkit.execute)(...)`):

```python
                    else:
                        result_text, _ = await asyncio.to_thread(
                            self._toolkit.execute, tc["name"], tc["input"]
                        )
```

Replace the `cl.Step` context manager:

```python
                async with self._sink.tool_step(tc["name"]) as step:
                    step.input = json.dumps(tc["input"], indent=2)
```
(the rest of that block — `step.output = result_text` and everything else inside — is
unchanged, only the `async with cl.Step(name=tc["name"], type="tool") as step:` line changes)

Replace the max-tokens message:

```python
            if stop_reason == "max_tokens":
                await self._sink.send_max_tokens_warning()
```

Replace the final response send:

```python
        if display_text:
            await self._sink.send_message(display_text)
```

Replace the three proposal-render branches:

```python
        if order_proposal:
            await self._sink.send_order_proposal(order_proposal)
        elif cancel_proposal:
            await self._sink.send_cancel_proposal(cancel_proposal)
        elif modify_proposal:
            await self._sink.send_modify_proposal(modify_proposal)
```

- [x] **Step 5: Run to verify pass**

Run: `pytest tests/test_agent.py -v`
Expected: `66 passed` (63 existing + 3 new)

- [x] **Step 6: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `322 passed` (313 baseline + 6 from Task 1.2 + 3 new), 0 failures — confirms zero
regressions anywhere else in the codebase from this refactor.

- [x] **Step 7: Commit**

```bash
git add claudia/agent.py tests/test_agent.py tests/test_security_regressions.py
git commit -m "refactor: inject MessageSink into ClaudIAAgent, remove direct chainlit import"
```

### Task 1.4: Wire `ChainlitMessageSink` into the existing `claudia/app.py`

**Files:**
- Modify: `claudia/app.py`

- [x] **Step 1: Construct the sink and pass it to `ClaudIAAgent`**

In `on_chat_start`, find the existing `agent = ClaudIAAgent(...)` call and change it to:

```python
    from claudia.message_sink import ChainlitMessageSink
    sink = ChainlitMessageSink(session_id=session_id)
    agent = ClaudIAAgent(
        toolkit=toolkit,
        store=store,
        context_loader=loader,
        session_id=session_id,
        sink=sink,
        model=_MODEL,
        extra_tools=tv_tools,
        tv_bridge=_tv_bridge,
        doc_version=version_label,
    )
```

- [x] **Step 2: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `322 passed`, 0 failures.

- [ ] **Step 3: Manual verification — DO NOT AUTOMATE (live IBKR gateway involved)**

Per this project's IBKR-safety practice (repeated gateway logins and 2FA are unreliable;
losing a live session is high-cost) — a human, not an agent, runs this:

```bash
caffeinate -i ./start-claudia.sh
```

Open http://localhost:8000, send a message that triggers at least one tool call (e.g. "what
are my current positions?"), and confirm: the response renders identically to before this
refactor, the tool-call step indicator still shows in the collapsible UI, and (if a
proposal-worthy question is asked) an order-proposal button still renders correctly. This
step exists to catch anything the unit-test mocks can't — do not mark this task complete
until a human confirms it.

- [x] **Step 4: Commit**

```bash
git add claudia/app.py
git commit -m "feat: wire ChainlitMessageSink into the Chainlit app entry point"
```

---

## Phase 2: Panel walking skeleton

**Goal:** A real, running Panel app — FastAPI-mounted, per-session `ClaudIAAgent`, full
streaming tool loop — that a user can actually chat with. No order-staging, no dashboard, no
GDrive/Flex/TradingView wiring yet (those are later phases). This is the first time the
agent's core loop runs against Panel instead of a mock.

**Panel APIs used below were verified directly against the installed 1.9.3 package in this
worktree's `.venv`** (not taken from documentation paraphrase) — see the grounding section.
Specifically confirmed: `panel.io.fastapi.add_application(path, app, title=...)` is a
decorator whose wrapped function is called once per new browser session inside
`with set_curdoc(doc):` (via Bokeh's `_eval_panel`), and its return value (if not `None` and
not a `BaseTemplate`) is rendered directly into that session's document — so a plain
per-session-scoped Python function that builds and returns a fresh `ChatInterface` is
sufficient for session isolation; no extra session-registry/dict is needed for this phase.

### Task 2.1: `PanelMessageSink`

**Files:**
- Create: `claudia/panel_sink.py`
- Create: `tests/test_panel_sink.py`

- [x] **Step 1: Write the failing tests**

```python
"""Tests for PanelMessageSink — the Panel-side MessageSink implementation.

Phase 2 scope only: send_message and tool_step have real, working (if basic) behavior;
order/cancel/modify proposal rendering is explicitly deferred to Phase 3 and sends a
plain, honest "not yet available" message rather than raising or silently dropping the
proposal — Phase 3 replaces this with the real button-pattern port.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from claudia.panel_sink import PanelMessageSink


def _make_chat():
    chat = MagicMock()
    chat.send = MagicMock()
    return chat


@pytest.mark.asyncio
async def test_send_message_sends_to_chat_interface_as_claudia():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_message("Hello there.")
    chat.send.assert_called_once_with("Hello there.", user="ClaudIA", respond=False)


@pytest.mark.asyncio
async def test_send_max_tokens_warning_sends_as_system():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_max_tokens_warning()
    args, kwargs = chat.send.call_args
    assert "truncated" in args[0].lower()
    assert kwargs["user"] == "System"


@pytest.mark.asyncio
async def test_tool_step_posts_then_updates_message_object():
    chat = _make_chat()
    posted_message = MagicMock()
    posted_message.object = ""
    chat.send.return_value = posted_message

    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions") as step:
        step.input = '{"foo": "bar"}'
        step.output = "100 AAPL"

    assert "get_positions" in posted_message.object
    assert "100 AAPL" in posted_message.object


@pytest.mark.asyncio
async def test_send_order_proposal_sends_placeholder_not_available_message():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_order_proposal({"symbol": "AAPL", "action": "BUY", "quantity": 10})
    args, kwargs = chat.send.call_args
    assert "not available" in args[0].lower() or "not yet available" in args[0].lower()
    assert kwargs["user"] == "System"
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_panel_sink.py -v`
Expected: `ModuleNotFoundError: No module named 'claudia.panel_sink'`

- [x] **Step 3: Implement `claudia/panel_sink.py`**

**Verified findings, 2026-07-22 (Task 2.1 execution) — two real bugs in this section's
original code, both fixed below, not just a style pass:**
1. The placeholder wording originally read "Order staging **isn't** available..." while
   the test below checks for the substring `"not available"`/`"not yet available"` — those
   don't match (`"isn't"` ≠ `"not"`). Wording corrected to "is **not yet** available" across
   all three placeholder methods so it actually satisfies the test (and matches the
   docstring's own stated intent).
2. `self._message = None` with no type annotation makes mypy infer the attribute's type as
   `None`, then flag the later `self._message.object = ...` in `__aexit__` as invalid — a
   real regression against this project's documented 0-error mypy baseline (see
   `docs/audits/2026-07-22-code-quality-pre-migration-audit.md`). Fixed with an explicit
   `self._message: Any = None` annotation (`from typing import Any` added to the imports).

```python
"""Panel-side MessageSink implementation.

Phase 2 scope: send_message and tool_step are real; order/cancel/modify proposal
rendering is a plain, honest placeholder until Phase 3 ports order_flow.py's
message-with-buttons pattern to Panel (claudia/panel_order_flow.py).
"""

from __future__ import annotations

import json
from typing import Any


class _PanelToolStepHandle:
    """Posts a message when a tool call starts, updates it in place when it ends —
    the same message.object-reassignment technique Panel's own docs use for the
    order-staging button pattern (research doc, point 4), applied here to a status
    message instead of a button. Phase 4 replaces this with the dedicated Status
    component once issue #6291's chrome-level gap is resolved or hand-built.
    """

    def __init__(self, chat, name: str) -> None:
        self._chat = chat
        self._name = name
        self.input: str = ""
        self.output: str = ""
        self._message: Any = None

    async def __aenter__(self) -> _PanelToolStepHandle:
        self._message = self._chat.send(
            f"**Running:** `{self._name}`…", user="System", respond=False
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._message.object = (
            f"**Tool:** `{self._name}`\n\n"
            f"Input: `{self.input}`\n\n"
            f"Output: {self.output}"
        )
        return False


class PanelMessageSink:
    """MessageSink backed by a live pn.chat.ChatInterface instance for one session."""

    def __init__(self, chat, session_id: str) -> None:
        self._chat = chat
        self._session_id = session_id

    async def send_message(self, text: str) -> None:
        self._chat.send(text, user="ClaudIA", respond=False)

    def tool_step(self, name: str) -> _PanelToolStepHandle:
        return _PanelToolStepHandle(self._chat, name)

    async def send_max_tokens_warning(self) -> None:
        self._chat.send(
            "⚠ Response truncated — token limit reached. "
            "Ask me to continue if the answer is incomplete.",
            user="System",
            respond=False,
        )

    async def send_order_proposal(self, proposal: dict) -> None:
        self._chat.send(
            f"Order staging is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )

    async def send_cancel_proposal(self, proposal: dict) -> None:
        self._chat.send(
            f"Order cancellation is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )

    async def send_modify_proposal(self, proposal: dict) -> None:
        self._chat.send(
            f"Order modification is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )
```

- [x] **Step 4: Run to verify pass**

Run: `pytest tests/test_panel_sink.py -v`
Expected: `4 passed`

- [x] **Step 5: Commit**

```bash
git add claudia/panel_sink.py tests/test_panel_sink.py
git commit -m "feat: add PanelMessageSink (Phase 2 scope — no proposal rendering yet)"
```

### Task 2.2: `claudia/panel_app.py` — FastAPI mount + per-session agent

**Files:**
- Create: `claudia/panel_app.py`
- Create: `tests/test_panel_app.py`

- [x] **Step 1: Write the failing test**

This tests the pure, framework-independent part — the per-session factory function —
without needing a real running server (verifying an actual HTTP round-trip through
Bokeh's session machinery belongs in the manual-verification step below, not a unit test).

```python
"""Tests for claudia/panel_app.py's per-session app factory."""

from unittest.mock import MagicMock, patch

from claudia.panel_app import _build_chat_app


def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.agent.AsyncAnthropic"),
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
        chat = _build_chat_app()

    assert chat.callback is not None
```

**Verified finding, 2026-07-22 (Task 2.2 execution):** the test above must patch
`claudia.agent.AsyncAnthropic` (added above, wasn't in the original draft) — `ClaudIAAgent.__init__`
constructs a real `AsyncAnthropic()` client otherwise, matching every other
`ClaudIAAgent`-constructing test's convention in `tests/test_agent.py`. **Correction to this
finding's original justification:** initially assumed this would fail in CI/a fresh clone
without a real key — checked directly and that's not accurate against the installed
`anthropic==0.118.0`: its client doesn't raise at construction time even with zero
credentials resolvable anywhere (auth failure is deferred to actual request time, confirmed
via `env -u ANTHROPIC_API_KEY python3 -c "AsyncAnthropic()"` constructing cleanly). The patch
is still correct to keep — a unit test shouldn't depend on real credential-resolution/HTTP-client
construction succeeding regardless of whether it currently happens to, and consistency with
`test_agent.py`'s pattern matters against future SDK upgrades — just don't cite "prevents a
CI crash" as the reason, since it isn't one today.

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_panel_app.py -v`
Expected: `ModuleNotFoundError: No module named 'claudia.panel_app'`

- [x] **Step 3: Implement `claudia/panel_app.py`**

**Verified finding, 2026-07-22 (Task 2.2 execution):** a bare `import panel as pn` does
**not** eagerly import the `fastapi` submodule (`panel/io/__init__.py` keeps it optional
since `fastapi` is only pulled in via the `panel[fastapi]` extra) — `@pn.io.fastapi.add_application`
fails with `AttributeError: module 'panel.io' has no attribute 'fastapi'`. Reproduced in a
clean process, confirmed environment-independent (not a local fluke), fixed below by
importing the function directly.

```python
"""Panel entry point for ClaudIA (Phase 2: walking skeleton).

Standalone FastAPI app, mounted via panel.io.fastapi.add_application — deliberately
its own process (a distinct dev port), not importing claudia/app.py's Chainlit
FastAPI instance, so this can be built and tested fully on the side per the kickoff
prompt's isolation instruction. Phase 11 (cutover) is where this becomes the sole
entry point.

Run with:  uvicorn claudia.panel_app:app --port 8001 --reload
"""

import logging
import os
import uuid

import panel as pn
from dotenv import load_dotenv
from fastapi import FastAPI
from ibkr_core_mcp import (
    BrowserCookieAuth,
    ClaudeToolkit,
    Config,
    GDriveCache,
    IBKRClient,
    SQLiteStore,
)
from panel.io.fastapi import add_application

from claudia.agent import ClaudIAAgent
from claudia.context_loader import ContextLoader
from claudia.conversation_store import ConversationStore
from claudia.panel_sink import PanelMessageSink

log = logging.getLogger(__name__)

load_dotenv(override=False)

_MODEL = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
_DOCS_PATH = os.environ.get("CLAUDIA_DOCS_PATH", "docs")
_DB_PATH = os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db")
_PANEL_PORT = int(os.environ.get("CLAUDIA_PANEL_PORT", "8001"))

_toolkit: ClaudeToolkit | None = None
_conv_store: ConversationStore | None = None


def _get_toolkit() -> ClaudeToolkit:
    """Process-level ClaudeToolkit singleton — identical pattern to claudia/app.py's
    _get_toolkit(), duplicated rather than imported to keep this module fully
    independent of the Chainlit entry point during the transition (see module
    docstring)."""
    global _toolkit
    if _toolkit is None:
        config = Config.from_env()
        ibkr = IBKRClient(
            config=config,
            auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")),
        )
        cache = GDriveCache(config)
        store = SQLiteStore(config)
        _toolkit = ClaudeToolkit(client=ibkr, cache=cache, store=store, config=config)
    return _toolkit


def _get_store() -> ConversationStore:
    global _conv_store
    if _conv_store is None:
        _conv_store = ConversationStore(_DB_PATH)
    return _conv_store


def _build_chat_app() -> pn.chat.ChatInterface:
    """Per-session factory: called fresh for each new browser session by Bokeh's
    _eval_panel (confirmed live against Panel 1.9.3 — see Phase 2 header note),
    so a plain local ClaudIAAgent + PanelMessageSink here already gives correct
    per-session isolation with no extra session registry needed."""
    session_id = str(uuid.uuid4())
    toolkit = _get_toolkit()
    store = _get_store()
    store.create_session(session_id)

    loader = ContextLoader(_DOCS_PATH)
    loader.load_system_prompt()  # validates docs exist before proceeding

    chat = pn.chat.ChatInterface()
    sink = PanelMessageSink(chat=chat, session_id=session_id)
    agent = ClaudIAAgent(
        toolkit=toolkit,
        store=store,
        context_loader=loader,
        session_id=session_id,
        sink=sink,
        model=_MODEL,
    )

    async def _on_user_input(contents: str, user: str, instance: pn.chat.ChatInterface) -> None:
        await agent.handle_message(contents)

    chat.callback = _on_user_input
    chat.send(
        "**ClaudIA (Panel preview) is ready.** Ask me anything about your portfolio, "
        "markets, or strategy.",
        user="ClaudIA",
        respond=False,
    )
    return chat


app = FastAPI()


@add_application("/", app=app, title="ClaudIA (Panel preview)")
def _serve_chat_app() -> pn.chat.ChatInterface:
    return _build_chat_app()
```

- [x] **Step 4: Run to verify pass**

Run: `pytest tests/test_panel_app.py -v`
Expected: `1 passed`

- [x] **Step 5: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `330 passed` (329 baseline after Task 2.1's own fixes + 1 new), 0 failures.

- [x] **Step 6: Manual verification — safe (no live IBKR gateway required to prove the skeleton)**

**Done, 2026-07-22.** Ran `uvicorn claudia.panel_app:app --port 8001`, drove it with Playwright
and a real human message in parallel. Welcome message rendered correctly; a real
conversation round-tripped end to end including a 4-tool-call sequence
(`get_live_pnl`/`get_pnl`/`get_account_summary`/`get_positions`, all correctly erroring since
IBKR gateway was intentionally offline at the time), each rendering as its own tool-step card
via `PanelMessageSink.tool_step()`. Notably, the model's final response correctly invoked the
hardcoded safety block's data-integrity rule ("I have no valid data, and per my
data-integrity rules I won't guess or show remembered figures") when all 4 tool calls
failed — concrete, live proof the safety-critical constraints survive unchanged through the
new Panel frontend. Console: 0 real errors (one benign missing-favicon 404, unrelated).

**Follow-up, same day, against a genuinely live IBKR session:** with the gateway
authenticated (`authStatus.authenticated: true`, confirmed via `/tickle`) and US equity
markets closed but CME ES futures open, re-ran the same skeleton against real account/tool
data (not the error-path case above) — user-confirmed first live test green. This is the
first time any part of the Panel migration has been exercised against genuine live IBKR
data end to end, not mocked or offline-erroring. Specifically exercised: a 4-tool-call
account-check sequence (`get_account_summary`/`get_positions`/`get_live_pnl`/`get_live_orders`)
in one turn, each rendering as its own correct tool-step card; real position/P&L data
flowing through into a well-formatted markdown-table response; and `get_live_orders`'
existing external/read-only origin detection (mobile/TWS/web-placed orders correctly
flagged as non-modifiable via API) surviving unchanged through the new frontend. (Real
account figures deliberately not recorded here — this file is git-tracked.)

Also exercised, same live session: `sync_flex_trades` (a write path — refreshes the local
trade-history store, distinct from the read-only account-check tools above) — green. Then
two order-proposal-adjacent tests, unscripted, both directly relevant to Phase 3:
1. An ambiguous two-order, garbled-symbol ("APPL"), unrealistic-price message
   (BUY 1 @ $100 / SELL 1 @ $1000 against a live $325.69 quote) — the model correctly
   fetched a live quote via `get_market_snapshot`, refused to propose either as fat-finger
   prices, enforced the one-proposal-per-message rule, and asked for clarification instead
   of guessing or silently rounding — the order-parameter-immutability and
   one-proposal-per-message rules firing correctly, unprompted.
2. A deliberate follow-up test proposal (BUY 1 AAPL @ $100 limit, explicitly labeled by the
   model as a non-filling test price) — this one **did** emit a real `order-proposal` block,
   which correctly triggered `PanelMessageSink.send_order_proposal()`'s Phase 2 placeholder:
   the "not yet available" message with the full parsed proposal JSON echoed back, exact
   values preserved. This is the first live confirmation that the placeholder path (not just
   the plain-message path) fires correctly end-to-end — closes out Phase 2's verification
   surface entirely; Phase 3 replaces this exact call site with real buttons.

```bash
uvicorn claudia.panel_app:app --port 8001 --reload
```

Open http://localhost:8001/ and confirm: the welcome message renders, typing a message and
pressing enter triggers a real response from Claude (the streaming tool loop runs — if IBKR
gateway isn't running, tool calls will return an error string, which is expected and fine
for this check; the point is confirming the round-trip, not IBKR connectivity). If a
response involves a tool call, confirm the basic "Running: `tool_name`…" → updated
input/output message appears (Phase 4 will make this nicer). This does not require
`caffeinate` or live gateway credentials — it's safe to run without the risk-flagged
precautions from Phase 1's Task 1.4.

- [x] **Step 7: Commit**

```bash
git add claudia/panel_app.py tests/test_panel_app.py
git commit -m "feat: Panel walking skeleton — FastAPI mount, per-session agent, real chat loop"
```

---

## Phase 3: Order-staging button pattern (safety-critical)

**Goal:** Port `order_flow.py`'s three proposal flows (order/cancel/modify) to Panel,
replacing `PanelMessageSink`'s Phase-2 placeholder methods with real
`pn.Row(pn.widgets.Button(...), pn.widgets.Button(...))` rendering, `on_click` → server
callback → `button.disabled = True`, exactly mirroring the message-with-buttons pattern
**[research]** already confirmed generically for Panel (not yet live-tested for this
specific proposal-button case — do that as part of this phase, the same way the Shadow-DOM
constraint was live-tested rather than assumed).

**Files:** Create `claudia/panel_order_flow.py` (Panel counterpart to `order_flow.py` —
same `_format_order_summary`/`_classify_execution_error`/`_resolve_account_id` helper logic
is pure and framework-agnostic; consider importing those directly from `order_flow.py`
rather than duplicating them — decide during detailing). Modify `claudia/panel_sink.py`
(replace the 3 placeholder methods). New: `tests/test_panel_order_flow.py`.

**Must preserve exactly (Hard Rules 1, 6, 7; `CLAUDE.md` Order Staging spec):**
- Gate 1 (Touch ID) / Gate 2 (AppKit dialog) are untouched — they live entirely in
  `ibkr_core_mcp` (`human_auth.py`, `order_confirm.py`), outside any UI framework. This
  phase only reproduces the trigger pattern, never touches the gates themselves.
- Order parameter immutability — the exact same `order_body` construction logic in
  `execute_staged_order`/`execute_cancel_order`/`execute_modify_order` (`order_flow.py:135-682`)
  must be reused unchanged (import directly, do not re-derive), since this is exactly the
  kind of safety-critical logic Hard Rule 3's "never modify the hardcoded safety block"
  spirit extends to.
- `action.remove()` always fires in a `finally` block today (`order_flow.py:339-340` etc.)
  — the Panel port's equivalent (`button.disabled = True` after click, per **[research]**
  point 4) must have the same never-skipped guarantee.

**Resolved, 2026-07-22 (live-tested, not assumed — before writing this phase's bite-sized
tasks, the same way the Shadow-DOM constraint was resolved before relying on it):** built a
throwaway `pn.chat.ChatInterface` sending a `pn.Column(Markdown, pn.Row(Button, Button))` as
one message's content (mirroring the order-proposal shape: a summary + Stage/Cancel), served
via `panel serve` + Playwright, real click. Confirmed via the browser's accessibility tree
and a screenshot, not inference:
- The button row renders as genuine clickable buttons inside the chat message (not flattened
  to text).
- Clicking correctly fires the `on_click` callback.
- Setting `button.disabled = True` on both buttons *inside* the callback reflects in the
  browser immediately — both buttons show as visually greyed-out/disabled with **zero**
  manual re-render call needed.
- Reassigning the summary pane's `.object` in the same callback (the "update the message
  after click" half of the pattern) also took effect in place, same message bubble.

One dev-server-only wrinkle hit and resolved, irrelevant to the real app: `panel serve`'s
default WebSocket origin check rejected Playwright's connection (403) until served with
`--allow-websocket-origin`; `claudia/panel_app.py`'s actual FastAPI-mounted deployment
doesn't go through `panel serve` at all, so this doesn't apply there — noted only so a future
throwaway test doesn't waste time rediscovering it.

**Net: the core mechanic this entire phase depends on is now confirmed working exactly as
the research doc described, not just plausible.** Nothing left to de-risk before writing
Task 3.x's bite-sized steps.

### Task 3.1: Close the `handle_message()` → proposal-dispatch coverage gap

Flagged by Task 1.3's code-quality review (2026-07-22): no test anywhere currently
exercises `handle_message()`'s `order_proposal`/`cancel_proposal`/`modify_proposal` →
`self._sink.send_*_proposal(...)` wiring end-to-end (`agent.py:675-680`) — only the JSON
strip-parsing and the sink's own delegation are tested in isolation, not the connection
between them. This is the single most safety-critical integration point in the whole
migration and it's currently only indirectly covered. Close it before touching
`order_flow.py` at all.

**Files:**
- Modify: `tests/test_agent.py`

- [x] **Step 1: Write the 3 missing tests**

Add alongside Task 1.3's `handle_message()` tests (same file, same `_FakeStream`/
`_text_response_events`/`_make_agent_with_sink` helpers — no new fixtures needed):

```python
# ── handle_message() → proposal dispatch (Task 3.1) ──────────────────────────

@pytest.mark.asyncio
async def test_handle_message_order_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 10,
        "order_type": "MKT", "limit_price": None, "stop_price": None,
        "tif": "DAY", "sec_type": "STK", "conid": None, "reason": "Test",
    }
    text = f"Here's my proposal.\n```order-proposal\n{json.dumps(proposal)}\n```"
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(text))
    )
    await agent.handle_message("Propose a trade")
    sink.send_order_proposal.assert_awaited_once_with(proposal)


@pytest.mark.asyncio
async def test_handle_message_cancel_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    proposal = {
        "order_id": "242538143", "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 100.0,
        "tif": "GTC", "reason": "Test",
    }
    text = f"Cancelling.\n```order-cancel-proposal\n{json.dumps(proposal)}\n```"
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(text))
    )
    await agent.handle_message("Cancel it")
    sink.send_cancel_proposal.assert_awaited_once_with(proposal)


@pytest.mark.asyncio
async def test_handle_message_modify_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    proposal = {
        "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
        "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "tif": "GTC", "sec_type": "STK",
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    text = f"Modifying.\n```order-modify-proposal\n{json.dumps(proposal)}\n```"
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(text))
    )
    await agent.handle_message("Modify it")
    sink.send_modify_proposal.assert_awaited_once_with(proposal)
```

- [x] **Step 2: Run to verify pass** (no implementation change needed — this only adds
  coverage for existing, already-correct `agent.py` behavior)

Run: `pytest tests/test_agent.py -v`
Expected: `69 passed` (66 existing + 3 new). If any of the 3 new tests fail, that means
`handle_message()`'s proposal-dispatch wiring has a real bug — stop and report, do not
proceed to Task 3.2 with a known-broken dispatch path underneath the button work.

- [x] **Step 3: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `334 passed` (331 baseline + 3 new), 0 failures.

- [x] **Step 4: Commit**

```bash
git add tests/test_agent.py
git commit -m "test: cover handle_message() proposal-dispatch wiring (order/cancel/modify)"
```

### Task 3.2: Extract `order_flow.py`'s execution core from its Chainlit-specific wrapper

**This is the safety-critical task of this phase — read the whole task before starting.**

**Why:** `execute_staged_order`/`execute_cancel_order`/`execute_modify_order`
(`order_flow.py`) each mix two genuinely different things in one function body: (a) the
actual order-placement logic — conid resolution across STK/FUT/FOP, CME 536-B field
injection, the exact IBKR request-body shape, Gate 1/2 invocation via `IBKRClient`, error
classification, decision logging — and (b) Chainlit-specific glue — parsing
`action.payload["order"]`, sending `cl.Message(...)` progress/result updates, calling
`action.remove()`. Only (b) is Chainlit-specific; (a) is the safety-critical part every
future frontend (Panel, and whatever comes after) must reuse byte-for-byte, never
re-derive — exactly the same reasoning `[Target architecture]`'s `MessageSink` decision
was built on. This task separates them; it does **not** change any order-placement
behavior.

**Files:**
- Modify: `claudia/order_flow.py`
- Modify: `tests/test_order_flow.py` (only to add new tests for the extracted core
  functions — every one of the existing tests must keep passing completely unmodified,
  since they test the public `execute_*` functions' observable behavior, which does not
  change)

**Verified finding, 2026-07-22 (Task 3.2 execution):** this section originally said "36
existing tests" / "40 passed" below — wrong, and never actually run before being written;
the real count is **70** existing tests (`grep -cE "^(async )?def test_" tests/test_order_flow.py`
at the pre-task commit), becoming **74** after this task's 4 new ones. Corrected throughout
below. Lesson for future phases: run the actual count, don't estimate one from reading
source — exactly the discipline this plan otherwise held itself to for Panel API claims.

- [x] **Step 1: Write the new tests first (TDD for the new surface; the existing tests are
  the regression guard for the surface that must not change)**

Add to `tests/test_order_flow.py`:

```python
# ── Extracted core functions (Task 3.2) — framework-agnostic, dict + callback in ────

def _make_send_status_recorder():
    """A send_status callback that records every (text, author) call, for assertions —
    the framework-agnostic equivalent of this file's existing _sent_contents(mock_cl)
    helper, which only works against the cl.Message-based wrapper."""
    calls = []

    async def _send_status(text: str, author: str) -> None:
        calls.append((text, author))

    return _send_status, calls


@pytest.mark.asyncio
async def test_execute_staged_order_core_success_calls_send_status():
    """The extracted core, called directly with a plain dict (no cl.Action, no JSON
    parsing) and a plain callback (no chainlit), produces the same success behavior."""
    from claudia.order_flow import _execute_staged_order_core
    ibkr_mod, _client = _make_ibkr_mock()
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 50,
        "order_type": "MKT", "limit_price": None, "stop_price": None, "reason": "Test",
    }
    send_status, calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_staged_order_core(proposal, send_status, session_id="s1", store=None)
    assert any("staged successfully" in text for text, _author in calls)


@pytest.mark.asyncio
async def test_execute_staged_order_core_never_touches_action_or_removes_anything():
    """The core function has no cl.Action parameter at all and does not call .remove() —
    that guarantee now lives entirely in the wrapper (Step 3 below), verified separately."""
    import inspect

    from claudia.order_flow import _execute_staged_order_core
    sig = inspect.signature(_execute_staged_order_core)
    assert "action" not in sig.parameters
    assert "proposal" in sig.parameters
    assert "send_status" in sig.parameters


@pytest.mark.asyncio
async def test_execute_cancel_order_core_calls_client_with_account_and_order_id():
    from claudia.order_flow import _execute_cancel_order_core
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    send_status, _calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_cancel_order_core(proposal, send_status, session_id="s1", store=None)
    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


@pytest.mark.asyncio
async def test_execute_modify_order_core_builds_fresh_body_not_raw_proposal():
    from claudia.order_flow import _execute_modify_order_core
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {
        "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
        "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "tif": "GTC", "sec_type": "STK",
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    send_status, _calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_modify_order_core(proposal, send_status, session_id="s1", store=None)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "_changed_fields" not in order_body
    assert "_previous_values" not in order_body
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_order_flow.py -v -k core`
Expected: `ImportError` / `ModuleNotFoundError`-style failures — `_execute_staged_order_core`
etc. don't exist yet.

- [x] **Step 3: Perform the extraction in `claudia/order_flow.py`**

This is a **structural move, not a rewrite** — the actual order-placement logic must be
relocated verbatim, not retyped from memory (the risk of a transcription slip in CME
536-B field logic or conid resolution is exactly what this step-by-step approach exists
to avoid). Do this mechanically:

1. Add near the top of the file, with the other type-only imports:
   ```python
   from collections.abc import Awaitable, Callable

   SendStatus = Callable[[str, str], Awaitable[None]]
   """(text, author) -> None — the framework-agnostic equivalent of cl.Message(content=text,
   author=author).send(), so the extracted *_core functions below don't import chainlit."""
   ```

2. For **each** of the three functions (`execute_staged_order`, `execute_cancel_order`,
   `execute_modify_order`), in order:

   a. Find the line immediately after the function's opening payload-parsing
      `try/except` block returns (e.g. for `execute_staged_order`, that's the line
      `symbol = proposal.get("symbol", "?")` — the first line that runs only once
      `proposal` is a valid dict). Everything from that line through the end of the
      function's outer `try: ... except Exception as exc: ...` block (i.e. **everything
      except** the top JSON-parsing block and the bottom `finally: await action.remove()`)
      is the part that moves.

   b. Cut that block out and paste it into a **new function** directly above the
      original, named with a `_core` suffix (`_execute_staged_order_core`,
      `_execute_cancel_order_core`, `_execute_modify_order_core`), with this signature
      (adjust per-function per what each one's proposal shape needs, matching the
      original function's existing local variable extraction — do not change what
      fields each function reads from `proposal`):
      ```python
      async def _execute_staged_order_core(
          proposal: dict,
          send_status: SendStatus,
          session_id: str | None = None,
          store: ConversationStore | None = None,
      ) -> None:
      ```
      (same pattern for `_execute_cancel_order_core`/`_execute_modify_order_core` —
      identical signature shape, just the body differs per the original function.)

   c. Within the moved block, replace **every** `await cl.Message(content=X, author=Y).send()`
      call with `await send_status(X, Y)` — same two positional values, same order,
      nothing else about those lines changes. **Verified finding, 2026-07-22:** an earlier
      draft of this step said "there are 2 such calls in each function" — wrong, never
      actually counted; the real count is 6 in `execute_staged_order`, 4 in
      `execute_cancel_order`, 5 in `execute_modify_order` (progress/success/error messages
      plus every early-return branch's message — FOP-guard, futures-not-found,
      contract-not-found, missing-order_id, missing-conid, etc.). The governing instruction
      ("every" call) was always correct; only the illustrative parenthetical was wrong —
      convert all of them, per function, not just two. Do **not** touch anything else in the
      moved block — the conid resolution branches, the `order_body` dict construction,
      the CME 536-B field logic, the `_resolve_account_id`/`_classify_execution_error`
      calls, the `store.add_decision(...)` calls: all byte-identical to before, just
      inside the new function.

   d. The original function (now much shorter) keeps its exact existing signature
      (`action: cl.Action`, `session_id`, `store`) and becomes a thin wrapper:
      ```python
      async def execute_staged_order(
          action: cl.Action,
          session_id: str | None = None,
          store: ConversationStore | None = None,
      ) -> None:
          """
          [keep the existing docstring unchanged]
          """
          try:
              proposal = json.loads(action.payload["order"])
          except (json.JSONDecodeError, TypeError, KeyError):
              await cl.Message(content="Invalid order proposal data.", author="System").send()
              await action.remove()
              return
          try:
              await _execute_staged_order_core(proposal, _cl_send_status, session_id, store)
          finally:
              await action.remove()
      ```
      (same pattern for `execute_cancel_order`/`execute_modify_order`, matching each
      one's own existing top-of-function payload-parse-failure branch exactly as it is
      today. **Correction to this step, 2026-07-22 (twice-corrected — the first correction
      itself undercounted, caught by the spec-compliance review, not just the
      implementer):** the missing-`order_id`/missing-`conid` early-return branches in
      `execute_cancel_order`/`execute_modify_order` DO move into the `_core` functions per
      step (a)'s line-boundary rule — but they originally called `await action.remove()`
      inline, and the `_core` functions have no `action` parameter to call it on. There are
      **three** such branches, not two: `execute_cancel_order`'s missing-`order_id` guard,
      and `execute_modify_order`'s missing-`order_id` guard *and* its separate
      missing-`conid` guard (cancel has no conid guard — cancellation never uses `conid` at
      all). Drop all three inline `action.remove()` calls when moving those branches — the
      wrapper's own `try: ... finally: await action.remove()` already covers those paths
      once control returns from the core function, so keeping the inline calls too would
      double-call `.remove()`. This is a mechanical necessity of the new signature, not a
      behavior change — verify by confirming the existing
      `test_execute_cancel_order_missing_order_id_sends_error`-style tests (all three
      branches have one) still pass with `action.remove.assert_called_once()`, i.e. exactly
      once, not twice.)

3. Add one shared helper, used by all three thin wrappers:
   ```python
   async def _cl_send_status(text: str, author: str) -> None:
       await cl.Message(content=text, author=author).send()
   ```

- [x] **Step 4: Run to verify the new tests pass**

Run: `pytest tests/test_order_flow.py -v -k core`
Expected: `4 passed`

- [x] **Step 5: Run the full existing suite — this is the real verification**

Run: `pytest tests/test_order_flow.py -v`
Expected: **all 70 original tests plus the 4 new ones = 74 passed, 0 failed** (corrected
count, see the Verified finding above this task's Files section). Every single original
test must pass with **zero modification to the test file's existing code** — if any
original test needs to change to pass, the extraction was not behavior-preserving and
something is wrong; stop and report rather than editing a test to match a changed
behavior.

- [x] **Step 6: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `338 passed` (334 baseline + 4 new), 0 failures.

- [x] **Step 7: Commit**

```bash
git add claudia/order_flow.py tests/test_order_flow.py
git commit -m "refactor: extract order_flow.py's execution core from its Chainlit wrapper"
```

### Task 3.3: Real order-staging buttons in Panel, wired to the extracted core

**Design decision, made explicit per Task 3.2's code-quality review request** (rather than
letting it happen implicitly): `SendStatus` (`order_flow.py`) and `MessageSink`
(`message_sink.py`) stay two separate abstractions. They serve genuinely different call
sites — post-button-click order execution vs. per-turn agent streaming — and forcing the
safety-critical `_execute_*_core` functions to depend on `MessageSink`'s full surface
(`tool_step`, `send_order_proposal`, ...) would add irrelevant coupling to the most
sensitive code path in the app. `claudia/panel_order_flow.py` (new, below) uses a small
named factory (`_make_send_status`, mirroring `order_flow.py`'s `_cl_send_status`) rather
than an inline lambda, per the same review's suggestion.

**Why `PanelMessageSink` needs a `store` reference it didn't need before:** Chainlit's
`execute_staged_order` re-fetches `store` fresh from `cl.user_session.get("store")` at
button-click time (`app.py`'s `@cl.action_callback` handlers) — there is no equivalent
"look up session state again at click time" mechanism in Panel; a Panel button's `on_click`
callback is a plain Python closure created once, at render time. So `store` must be
captured in that closure *when the proposal is rendered*, not fetched later — which means
`PanelMessageSink` itself now needs a `store` reference (it didn't in Phase 2, since the
placeholder methods never touched the database), threaded in from `panel_app.py`'s
`_build_chat_app()`, which already has a local `store` variable.

**Files:**
- Create: `claudia/panel_order_flow.py`
- Modify: `claudia/panel_sink.py` (add `store` param to `__init__`; replace the 3
  placeholder methods)
- Modify: `claudia/panel_app.py` (pass `store=store` when constructing `PanelMessageSink`)
- Create: `tests/test_panel_order_flow.py`
- Modify: `tests/test_panel_sink.py` (the 3 placeholder-behavior tests for
  `send_order_proposal`/`send_cancel_proposal`/`send_modify_proposal` now test different,
  real behavior — delegation to `panel_order_flow`'s render functions — not the old "not
  yet available" text)

- [x] **Step 1: Write the failing tests**

Add to `tests/test_panel_order_flow.py` (new file):

```python
"""Tests for panel_order_flow.py — Panel-side order-staging button rendering.

Mirrors tests/test_order_flow.py's mocking conventions (_make_ibkr_mock-style patch.dict
on sys.modules) since render_*_proposal here calls straight through to order_flow.py's
already-tested _execute_*_core functions — these tests verify the Panel-specific wiring
(buttons constructed, on_click bound, message sent, buttons disabled after click), not the
order-placement logic itself (that's test_order_flow.py's job, already covered).
"""

from unittest.mock import MagicMock, patch

import pytest

from claudia.panel_order_flow import (
    render_cancel_proposal,
    render_modify_proposal,
    render_order_proposal,
)


def _make_chat():
    chat = MagicMock()
    chat.send = MagicMock()
    return chat


def _make_ibkr_mock():
    """Same shape as test_order_flow.py's helper of the same name — a successful,
    minimal STK order path, since these tests only need the *call* to succeed, not
    every branch (that's already covered in test_order_flow.py)."""
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    mod.BrowserCookieAuth = MagicMock()
    mod.Config.from_env.return_value = MagicMock()
    client.search_contract.return_value = [{"conid": 265598, "companyName": "APPLE INC"}]
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    client.place_order_and_confirm.return_value = [{"orderId": "999"}]
    client.cancel_order.return_value = {"order_id": "242538143", "msg": "Cancelled"}
    client.modify_order_and_confirm.return_value = {"order_id": "242538143", "order_status": "Submitted"}
    return mod, client


def _get_click_callback(button):
    """Extract the real on_click callback from a live pn.widgets.Button, for direct
    invocation in a unit test (no browser, no running Panel server).

    Verified live, 2026-07-22, against the installed panel==1.9.3: Button.on_click(cb)
    is implemented as `self.param.watch(cb, 'clicks', onlychanged=False)` (confirmed via
    `inspect.getsource(pn.widgets.Button.on_click)`) — there is no `_on_click` attribute
    on the button itself. The registered callback lives in
    `button.param.watchers['clicks']['value']`, a list of param Watcher namedtuples;
    Panel's own internal sync watchers (name/label/value mirroring etc.) are always
    registered with `onlychanged=True`, while on_click's own watcher is always
    `onlychanged=False` — confirmed by direct inspection of that list — so filtering on
    that flag reliably isolates the one watcher this file's own render_* functions
    registered, regardless of how many internal watchers Panel itself adds. Calling
    `.fn` directly and awaiting it (async callbacks are supported natively, confirmed via
    `param.parameterized`'s `iscoroutinefunction(watcher.fn)` branch) exercises the exact
    function a real click would invoke, without needing Panel's async_executor/event-loop
    plumbing that a bare pytest run doesn't have.
    """
    watchers = button.param.watchers["clicks"]["value"]
    matches = [w.fn for w in watchers if not w.onlychanged]
    assert len(matches) == 1, f"expected exactly 1 on_click watcher, found {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_render_order_proposal_sends_message_with_two_buttons():
    chat = _make_chat()
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "MKT"}
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    chat.send.assert_called_once()
    args, kwargs = chat.send.call_args
    assert kwargs["user"] == "ClaudIA — Order Proposal"
    # sent content is a pn.Column containing a pn.Row of 2 buttons — inspect structurally
    column = args[0]
    button_row = column[1]
    assert len(button_row) == 2
    assert button_row[0].name == "Stage this order"
    assert button_row[1].name == "Cancel"


@pytest.mark.asyncio
async def test_render_order_proposal_stage_click_executes_and_disables_buttons():
    chat = _make_chat()
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 10,
        "order_type": "MKT", "limit_price": None, "stop_price": None,
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    stage_btn, cancel_btn = column[1][0], column[1][1]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(stage_btn)(None)  # simulate a real click

    client.place_order_and_confirm.assert_called_once()
    assert stage_btn.disabled is True
    assert cancel_btn.disabled is True


@pytest.mark.asyncio
async def test_render_order_proposal_cancel_click_disables_without_executing():
    chat = _make_chat()
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "MKT"}
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    stage_btn, cancel_btn = column[1][0], column[1][1]

    await _get_click_callback(cancel_btn)(None)

    assert stage_btn.disabled is True
    assert cancel_btn.disabled is True
    # 2 chat.send calls total: the original proposal render + the cancellation notice
    assert chat.send.call_count == 2


@pytest.mark.asyncio
async def test_render_cancel_proposal_sends_message_with_two_buttons():
    chat = _make_chat()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Cancel this order"
    assert button_row[1].name == "Keep order"


@pytest.mark.asyncio
async def test_render_cancel_proposal_confirm_click_calls_cancel_core():
    chat = _make_chat()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    ibkr_mod, client = _make_ibkr_mock()
    await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    cancel_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(cancel_btn)(None)

    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


@pytest.mark.asyncio
async def test_render_modify_proposal_sends_message_with_two_buttons():
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Modify this order"
    assert button_row[1].name == "Discard"


@pytest.mark.asyncio
async def test_render_modify_proposal_confirm_click_calls_modify_core():
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    modify_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(modify_btn)(None)

    client.modify_order_and_confirm.assert_called_once()
```

**Resolved, 2026-07-22, before dispatch (not left for the implementer to discover):** the
click-simulation approach above was verified directly against the installed
`panel==1.9.3` in this worktree's `.venv` — a throwaway script constructed a real
`pn.widgets.Button`, registered an async `on_click` callback, extracted it via
`button.param.watchers['clicks']['value']` filtered by `onlychanged=False` (exactly the
`_get_click_callback` helper above), and confirmed calling `await callback_fn(None)`
directly and correctly invokes the exact function a real click would — verified output:
`"after direct call, clicked count: 1"`. An earlier draft of this task guessed a
`button._on_click.callback` attribute that does not exist; corrected before dispatch.

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_panel_order_flow.py -v`
Expected: `ModuleNotFoundError: No module named 'claudia.panel_order_flow'`

- [x] **Step 3: Implement `claudia/panel_order_flow.py`**

```python
"""Panel counterpart to order_flow.py's Chainlit-native render_*_proposal functions.

Reuses order_flow.py's framework-agnostic pieces directly: _format_*_summary (pure
formatting, already tested) and _execute_*_order_core (the actual safety-critical
order-placement logic, extracted in a prior task specifically so this file never
re-derives it — see that task's rationale). Only the rendering (buttons embedded in a
chat message) and the send_status wiring are Panel-specific.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import panel as pn

from claudia.order_flow import (
    SendStatus,
    _execute_cancel_order_core,
    _execute_modify_order_core,
    _execute_staged_order_core,
    _format_cancel_summary,
    _format_modify_summary,
    _format_order_summary,
)

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore


def _make_send_status(chat) -> SendStatus:
    """Bind a send_status callback to one specific chat session — the Panel
    counterpart to order_flow.py's module-level _cl_send_status, which doesn't need
    binding since Chainlit's cl.Message is already session-scoped via contextvars."""
    async def _send_status(text: str, author: str) -> None:
        chat.send(text, user=author, respond=False)
    return _send_status


async def render_order_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render an order proposal as a Panel chat message with staging/cancel buttons."""
    summary_pane = pn.pane.Markdown(_format_order_summary(proposal))
    stage_btn = pn.widgets.Button(name="Stage this order", button_type="success")
    cancel_btn = pn.widgets.Button(name="Cancel", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_stage(event) -> None:
        try:
            await _execute_staged_order_core(proposal, send_status, session_id, store)
        finally:
            stage_btn.disabled = True
            cancel_btn.disabled = True

    async def _on_cancel(event) -> None:
        chat.send("Order proposal cancelled.", user="ClaudIA", respond=False)
        stage_btn.disabled = True
        cancel_btn.disabled = True

    stage_btn.on_click(_on_stage)
    cancel_btn.on_click(_on_cancel)

    chat.send(
        pn.Column(summary_pane, pn.Row(stage_btn, cancel_btn)),
        user="ClaudIA — Order Proposal",
        respond=False,
    )


async def render_cancel_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a cancel proposal as a Panel chat message with cancel/keep buttons."""
    summary_pane = pn.pane.Markdown(_format_cancel_summary(proposal))
    cancel_btn = pn.widgets.Button(name="Cancel this order", button_type="danger")
    keep_btn = pn.widgets.Button(name="Keep order", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_cancel_click(event) -> None:
        try:
            await _execute_cancel_order_core(proposal, send_status, session_id, store)
        finally:
            cancel_btn.disabled = True
            keep_btn.disabled = True

    async def _on_keep_click(event) -> None:
        chat.send("Cancel proposal dismissed — order left unchanged.", user="ClaudIA", respond=False)
        cancel_btn.disabled = True
        keep_btn.disabled = True

    cancel_btn.on_click(_on_cancel_click)
    keep_btn.on_click(_on_keep_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(cancel_btn, keep_btn)),
        user="ClaudIA — Cancel Proposal",
        respond=False,
    )


async def render_modify_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a modify proposal as a Panel chat message with modify/discard buttons."""
    summary_pane = pn.pane.Markdown(_format_modify_summary(proposal))
    modify_btn = pn.widgets.Button(name="Modify this order", button_type="success")
    discard_btn = pn.widgets.Button(name="Discard", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_modify_click(event) -> None:
        try:
            await _execute_modify_order_core(proposal, send_status, session_id, store)
        finally:
            modify_btn.disabled = True
            discard_btn.disabled = True

    async def _on_discard_click(event) -> None:
        chat.send("Modify proposal discarded — order left unchanged.", user="ClaudIA", respond=False)
        modify_btn.disabled = True
        discard_btn.disabled = True

    modify_btn.on_click(_on_modify_click)
    discard_btn.on_click(_on_discard_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(modify_btn, discard_btn)),
        user="ClaudIA — Modify Proposal",
        respond=False,
    )
```

**Note:** `order_flow.py` must export `SendStatus` (the type alias) for this import to
work — confirm it's not already prefixed as module-private in a way that blocks importing
it (it isn't a `_`-prefixed name per Task 3.2's implementation, but verify against the
actual file rather than assuming).

- [x] **Step 4: Update `claudia/panel_sink.py`**

Add `store` to the constructor, replace the 3 placeholder methods:

```python
    def __init__(self, chat, session_id: str, store: ConversationStore | None = None) -> None:
        self._chat = chat
        self._session_id = session_id
        self._store = store
```

```python
    async def send_order_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_order_proposal
        await render_order_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_cancel_proposal
        await render_cancel_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_modify_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_modify_proposal
        await render_modify_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)
```

Add the matching `ConversationStore` `TYPE_CHECKING` import at the top of `panel_sink.py`
(mirroring how `message_sink.py`/`order_flow.py` already do this — check the existing
pattern rather than inventing a new one).

Update `tests/test_panel_sink.py`'s 3 placeholder tests
(`test_send_order_proposal_sends_placeholder_not_available_message` and its
cancel/modify siblings, added in Task 2.1) — they now test different, real behavior.
Replace them with delegation tests matching this file's existing style (mock
`claudia.panel_order_flow.render_order_proposal` etc. and assert it's awaited with the
right args), the same pattern `tests/test_message_sink.py` already uses for
`ChainlitMessageSink`'s equivalent methods:

```python
@pytest.mark.asyncio
async def test_send_order_proposal_delegates_to_panel_order_flow():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1", store=None)
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10}
    with patch("claudia.panel_order_flow.render_order_proposal", new=AsyncMock()) as mock_render:
        await sink.send_order_proposal(proposal)
        mock_render.assert_awaited_once_with(chat, proposal, session_id="s1", store=None)
```
(same pattern for cancel/modify — 3 tests total replacing the 3 old placeholder tests).

- [x] **Step 5: Update `claudia/panel_app.py`**

In `_build_chat_app()`, change:
```python
    sink = PanelMessageSink(chat=chat, session_id=session_id)
```
to:
```python
    sink = PanelMessageSink(chat=chat, session_id=session_id, store=store)
```
(`store` is already a local variable in this function from `store = _get_store()` a few
lines above — no new lookup needed.)

- [x] **Step 6: Run to verify pass**

Run: `pytest tests/test_panel_order_flow.py -v tests/test_panel_sink.py -v tests/test_panel_app.py -v`
Expected: `tests/test_panel_order_flow.py` → **7 passed** (new file: 3 order-proposal tests,
2 cancel-proposal, 2 modify-proposal). `tests/test_panel_sink.py` → **6 passed**, same
count as before this task (3 placeholder tests replaced in place with 3 delegation tests,
net zero change — not 9; don't add without removing the old ones). `tests/test_panel_app.py`
→ **2 passed**, unchanged (this task doesn't add a test there — see the code-quality
review's likely question about whether `_build_chat_app()`'s `PanelMessageSink(..., store=store)`
wiring needs its own dedicated test; worth raising there rather than deciding it here).

- [x] **Step 7: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: **347 passed** (340 baseline + 7 new in `test_panel_order_flow.py`; `test_panel_sink.py`'s
3-for-3 test replacement is a net-zero change to the total count), 0 failures.

- [ ] **Step 8: Manual verification — HIGH STAKES, human-only, real Touch ID / real IBKR**

**This is not like Phase 1/2's manual-verification steps.** Clicking "Stage this order" in
a live, uvicorn-served `panel_app.py` now calls the real, unmocked
`_execute_staged_order_core` — which calls real `IBKRClient` methods, which trigger real
Gate 1 (Touch ID prompt) and real Gate 2 (AppKit confirmation dialog), exactly as the
existing Chainlit app already does today. This is not new risk introduced by the
migration — it's the same risk the current production Chainlit order-staging flow already
carries — but it means an agent must not attempt this step, and a human doing it should:
- Use a deliberately unrealistic limit price (matching this project's own established
  practice for live write-tests — e.g. a BUY limit far below market, so even a fully
  confirmed order rests unfilled and does nothing), the same pattern already used earlier
  in this same live-testing session.
- Feel free to stop at Gate 1 or Gate 2 without completing them — confirming the prompt
  *appears* is sufficient proof the trigger fires correctly; you do not need to complete
  Touch ID or click "SEND TO IBKR" to validate this task's actual scope (the button →
  callback → gate-invocation wiring), unless you specifically want to test the full path.
- Also verify the "Cancel"/"Keep order"/"Discard" buttons (no IBKR call, no gates) — these
  are safe to click fully in any test.

- [x] **Step 9: Commit**

```bash
git add claudia/panel_order_flow.py claudia/panel_sink.py claudia/panel_app.py tests/test_panel_order_flow.py tests/test_panel_sink.py
git commit -m "feat: real order-staging buttons in Panel, wired to order_flow.py's extracted core"
```

**Code-quality review findings, 2026-07-22 (all closed same day, in a follow-up commit):**
1. Buttons now disable at the *start* of each click handler, not only in `finally` — cheap,
   no downside, applied to all three "confirm" handlers.
2. All six callbacks (`_on_stage`/`_on_cancel`/`_on_cancel_click`/`_on_keep_click`/
   `_on_modify_click`/`_on_discard_click`) now log via `log.exception(...)` before
   re-raising, matching the convention `panel_app.py`'s `_on_user_input` already
   established (`2ac5769`) — previously only the agent-turn callback had this, not the
   order-staging ones, which are the higher-stakes surface.
3. Added a test asserting `_build_chat_app()` constructs `PanelMessageSink(..., store=store)`
   with the real store object — the review noted the one-line wiring wasn't covered, and
   a regression there would silently drop staged-order audit logging with zero test failures.
4. Switched `Button(name=..., button_type=...)` → `Button(label=..., color=...)` across all
   six buttons — the former pair emits `PendingDeprecationWarning` against the installed
   `panel==1.9.3`; confirmed `label=`/`color=` populate the same `.name`/`.button_type`
   attributes the tests already assert on, so no test changes were needed.
5. Typed the `chat` parameter as `pn.chat.ChatInterface` across all functions in this file.

**Deferred, not fixed here (flagged for a future task, not part of Task 3.3's file
scope):** the review's deeper finding — `_execute_*_order_core`'s Gate 1 (Touch ID) and
Gate 2 (AppKit dialog) chain is fully synchronous/blocking (confirmed by reading
`ibkr_core_mcp/human_auth.py`'s `threading.Event.wait` and `order_confirm.py`'s
`subprocess.run`), unlike every other blocking call in this codebase, which is documented
to go through `cl.make_async`/`asyncio.to_thread`. This means the event loop can't flush
a "disabled" button state to the browser until the *entire* gate chain finishes — up to
60s+ — so the button-disable reordering above closes the *server-state* staleness but
likely not the *client-visible* re-click window, by the reviewer's own asyncio-semantics
reasoning (not independently re-verified against Panel/Bokeh's session-dispatch internals).
The actual fix — wrapping the three `_execute_*_order_core` calls in `asyncio.to_thread`
inside `order_flow.py` — belongs to a dedicated future task: it touches the safety-critical
core (not this task's Panel-only file scope), and it would benefit the existing Chainlit
surface too, not just Panel. Two human gates (Touch ID + a physical "SEND TO IBKR" click)
remain a real backstop in the meantime.

## Phase 4: Tool-call Status indicator

**Goal:** Replace `_PanelToolStepHandle`'s Phase-2 placeholder (plain message,
before/after) with something closer to Chainlit's collapsible `cl.Step` UX.

**Resolved, 2026-07-22 — the "first action of this phase" check, done before any code:**
re-checked [holoviz/panel#6291](https://github.com/holoviz/panel/issues/6291) via `gh issue
view 6291 --repo holoviz/panel`: still **open**, but only for *other* roadmap items (a
combined file-upload+textarea input, chat styling). Item 3 on that issue's own list — a
`Status` component for streaming agents' intermediate steps — **has shipped**, as
`pn.chat.ChatStep`, confirmed three ways, not just imported and assumed to work:
1. `python3 -c "import panel as pn; print(pn.chat.ChatStep)"` — exists in the installed
   `panel==1.9.3`.
2. Fetched the official reference docs
   (`https://panel.holoviz.org/reference/chat/ChatStep.html`, via keyless Firecrawl scrape)
   — a fully-documented component with `status` (`pending`/`running`/`success`/`failed`),
   `stream()`/`stream_title()` methods, and sync `with chat_step:` context-manager support
   that auto-transitions `pending→running` on enter and `running→success`/`failed` on exit.
3. **Live-tested the exact mechanics this task depends on** (`inspect.getsource` on
   `__enter__`/`__exit__`, then a real throwaway script exercising both the success and
   exception paths) — this surfaced two real, non-obvious findings neither the docs nor the
   source comments state explicitly:
   - Consecutive `.stream(str)` calls **concatenate into the same Markdown pane with no
     separator** (confirmed: `"Input: {...}Output: 100 AAPL"` with nothing between them on
     first attempt) — the docs' "concatenates to the last available pane" line undersells
     how literal this is. Fix: the `output` setter must add its own `"\n\n"` prefix.
   - **Passing a custom `failed_title` suppresses `ChatStep`'s own automatic
     exception-message streaming** — read directly in `__exit__`'s source: the
     `self.stream(exc_msg)` call for the failure body sits *inside* the
     `if self.failed_title is None:` branch. Confirmed by testing both ways: with a custom
     `failed_title` set, `chat_step.objects` stayed empty after an exception (the error
     silently disappeared from the UI — an even worse version of the exact gap this phase
     exists to close); *without* setting it, `ChatStep` auto-generates
     `f"Error: {exc_type.__name__!r}"` as the title **and** streams the real exception
     message into the body, then `__exit__` returns `False` (exception still propagates
     normally, matching `cl.Step`'s existing re-raise behavior). **Design consequence: do
     not set `failed_title` on the constructed `ChatStep` — let it handle failure display
     natively.** This closes Task 2.1's carried-over gap with less code than the Phase 2
     placeholder had, not more, because the built-in component already does the right
     thing once you don't fight it.

This means Phase 4 is materially smaller than originally scoped — no hand-building, no
need to clone the `panel-source-reference` fork (the kickoff prompt's contingency for "if
not shipped" doesn't apply here).

### Task 4.1: Replace `_PanelToolStepHandle` with a real `ChatStep` wrapper

**Files:**
- Modify: `claudia/panel_sink.py` (`_PanelToolStepHandle`, `PanelMessageSink.tool_step`)
- Modify: `tests/test_panel_sink.py` (replace the Phase-2
  `test_tool_step_posts_then_updates_message_object` test — the whole mechanism it tested,
  "post a message then reassign `.object`", is gone, replaced by `ChatStep`'s own
  `stream()`/status API)

- [ ] **Step 1: Write the failing tests**

Replace the existing `test_tool_step_posts_then_updates_message_object` test in
`tests/test_panel_sink.py` with:

```python
@pytest.mark.asyncio
async def test_tool_step_success_streams_input_then_output_with_separator():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions") as step:
        step.input = '{"foo": "bar"}'
        step.output = "100 AAPL"
    sent_step = chat.send.call_args.args[0]
    assert sent_step.status == "success"
    assert sent_step.serialize() == 'ChatStep(Markdown=\'Input: `{"foo": "bar"}`\n\nOutput: 100 AAPL\')'


@pytest.mark.asyncio
async def test_tool_step_exception_sets_failed_status_and_reraises():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    with pytest.raises(RuntimeError, match="boom"):
        async with sink.tool_step("get_positions") as step:
            step.input = "{}"
            raise RuntimeError("boom")
    sent_step = chat.send.call_args.args[0]
    assert sent_step.status == "failed"
    assert "boom" in sent_step.serialize()


@pytest.mark.asyncio
async def test_tool_step_sends_a_real_chatstep_not_a_plain_message():
    import panel as pn
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions"):
        pass
    sent_step = chat.send.call_args.args[0]
    assert isinstance(sent_step, pn.chat.ChatStep)
    call_kwargs = chat.send.call_args.kwargs
    assert call_kwargs["user"] == "System"
    assert call_kwargs["respond"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_panel_sink.py -v -k tool_step`
Expected: `AttributeError`/assertion failures against the current Phase-2 implementation
(which sends a plain string message, not a `ChatStep`).

- [ ] **Step 3: Implement in `claudia/panel_sink.py`**

Replace the entire `_PanelToolStepHandle` class and `PanelMessageSink.tool_step` method:

```python
class _PanelToolStepHandle:
    """Wraps a real pn.chat.ChatStep — Panel's built-in equivalent of Chainlit's
    cl.Step, shipped in panel==1.9.3 (confirmed live, 2026-07-22 — see Phase 4's
    header note for the verification). Translates the ToolStepHandle protocol's
    plain .input/.output attribute-setting into ChatStep's own .stream() calls, and
    delegates to ChatStep's own (synchronous) __enter__/__exit__ for status
    transitions and exception formatting.

    Deliberately does NOT set a custom failed_title on the underlying ChatStep —
    verified live that doing so suppresses ChatStep's own automatic
    exception-message streaming (the self.stream(exc_msg) call in its __exit__ is
    gated on failed_title being None). Leaving it unset gets a correct
    auto-generated title *and* the real error text in the body, for free.
    """

    def __init__(self, chat_step) -> None:
        self._chat_step = chat_step
        self._input = ""
        self._output = ""
        self._input_set = False

    @property
    def input(self) -> str:
        return self._input

    @input.setter
    def input(self, value: str) -> None:
        self._input = value
        self._chat_step.stream(f"Input: `{value}`")
        self._input_set = True

    @property
    def output(self) -> str:
        return self._output

    @output.setter
    def output(self, value: str) -> None:
        self._output = value
        # Consecutive string .stream() calls concatenate into one Markdown pane with
        # no separator (verified live) — supply our own blank-line break.
        sep = "\n\n" if self._input_set else ""
        self._chat_step.stream(f"{sep}Output: {value}")

    async def __aenter__(self) -> _PanelToolStepHandle:
        self._chat_step.__enter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self._chat_step.__exit__(exc_type, exc, tb)
```

```python
    def tool_step(self, name: str) -> _PanelToolStepHandle:
        chat_step = pn.chat.ChatStep(
            default_title=f"`{name}`",
            running_title=f"Running `{name}`…",
            success_title=f"`{name}`",
            # failed_title deliberately left unset — see _PanelToolStepHandle's docstring.
        )
        self._chat.send(chat_step, user="System", respond=False)
        return _PanelToolStepHandle(chat_step)
```

Add `import panel as pn` at module level if not already present (it should already be
imported for `PanelMessageSink`'s other methods — confirm rather than assume, and don't
add a duplicate import).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_panel_sink.py -v -k tool_step`
Expected: `3 passed`

- [ ] **Step 5: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: `350 passed` (348 baseline + 2 net new — 3 new tests replacing 1 old one), 0
failures.

- [ ] **Step 6: Manual verification — safe (no live IBKR gateway required)**

Same pattern as Phase 2's Task 2.2 Step 6: `uvicorn claudia.panel_app:app --port 8001`,
send a message that triggers at least one tool call, confirm the tool-step card now shows
a collapsible `ChatStep` (title changes from "Running `tool_name`…" to "`tool_name`" on
success, content collapses per `collapsed_on_success` default) rather than the old
plain-message before/after pattern. If IBKR gateway is offline, the tool call will error —
also worth confirming the failure path renders correctly (title becomes
"Error: 'ExceptionType'", body shows the real error text), since this is exactly the path
Task 2.1's original review flagged as silently broken and this phase exists to fix.

- [ ] **Step 7: Commit**

```bash
git add claudia/panel_sink.py tests/test_panel_sink.py
git commit -m "feat: replace hand-built tool-step placeholder with real pn.chat.ChatStep"
```

**First action of this phase, before any code:** re-check
[holoviz/panel#6291](https://github.com/holoviz/panel/issues/6291)'s current state — it was
open as of 2026-07-19/22; may have shipped a real Status component since. If shipped, use
it directly instead of hand-building. If not, `claudia/stephus182-panel` fork
(`git clone https://github.com/stephus182/panel.git ../panel-source-reference` per
**[kickoff]**) exists specifically to study `ChatInterface`/`ChatMessage` internals for
this — clone it before hand-building anything, per the kickoff prompt's own guidance.

**Files:** Modify `claudia/panel_sink.py` (`_PanelToolStepHandle`), possibly new
`claudia/panel_status.py` if the hand-built version grows beyond a trivial wrapper.

## Design principle, confirmed 2026-07-22: no more Chainlit-shape mimicry

From here on, every remaining phase is designed **Panel-native first** — what's the best,
cleanest way to do this *in Panel*, not "how do I port what Chainlit did." This is a
deliberate correction: Phase 5's original "Key design question" below was initially framed
around replicating Chainlit's blocking-until-ready session-start UX, which was never a
requirement (unlike the safety-critical order-execution logic, where preserving exact
behavior *is* mandated by CLAUDE.md's Hard Rules) — it was just habit. The connectivity-status
redesign in Phase 6 below is the first concrete example of this: it doesn't port
`/api/status` + browser-JS-polling at all, because that pattern only existed as a workaround
for Chainlit's opaque frontend, and Panel doesn't have that constraint.

**Long-term goal, not a near-term task:** once Panel reaches full feature parity and is
stable, remove the Chainlit architecture entirely (`claudia/app.py`, `ChainlitMessageSink`,
the Chainlit-specific parts of `order_flow.py`, `custom.css`/`custom.js`, the `chainlit`
dependency). This is still Phase 11's job, not something to start now — Phases 5-10 keep
`claudia/app.py` fully working throughout, exactly as the kickoff prompt's original
isolation instruction required.

## Phase 5: Session lifecycle completeness — outline

**Carry over from Task 2.2 (2026-07-22):** the Phase 2 skeleton's `_build_chat_app()` calls
`loader.load_system_prompt()` with no error handling — `claudia/app.py`'s `on_chat_start`
wraps the equivalent call in `try/except FileNotFoundError` and sends a friendly "Setup
required" message instead of letting a missing `docs/context.md`/`principles.md` surface as
a raw uncaught exception during session creation. Bring this in line as part of this
phase's parity work, not before — Phase 2 was intentionally minimal.

**Goal:** Bring `claudia/panel_app.py`'s per-session factory up to parity with
`claudia/app.py`'s full `on_chat_start` (`app.py:215-604`): GDrive DB download (first
session only) + context/principles Drive read, doc-version registration + hash-change
alert, opening status block (account summary/positions/P&L/live orders via
`toolkit.execute`), Flex/trade-data status line + background sync decision logic, action
buttons (Start IBKR Gateway, End Session — Launch TradingView deferred to Phase 9), and
session-end cleanup (`_run_session_cleanup` equivalent: `session_reporter`, GDrive upload).

**Resolved, 2026-07-22 (live-tested, not assumed — and the conclusion changed once "no
Chainlit-shape mimicry" was applied to it):** `_build_chat_app()` runs synchronously inside
Bokeh's `_eval_panel`, before the page is servable. The original framing of this question
asked whether `pn.state.on_session_created` could prefetch GDrive/IBKR data *before*
`_build_chat_app()` runs, so the first render already has it. Built a real FastAPI +
`add_application` test app (matching `panel_app.py`'s actual architecture, not `panel
serve`'s per-session script re-exec, which is a materially different execution model and
gave misleading results on the first attempt) and confirmed two things empirically:
- `on_session_created` **does** fire before the build function, every time — verified via
  print-ordering across real requests.
- But its callback is **not awaited** by Panel (`for cb in ...: cb(session_context)`, a
  plain sync call in `panel/io/application.py`) — so any async work started from inside it
  is still in-flight when `_build_chat_app()` runs immediately after. A `pn.state.cache`
  round-trip test proved this directly: the build function saw `None` for data an
  `on_session_created`-triggered task was still fetching, which only landed *after* the
  page had already rendered.

So `on_session_created` genuinely can't hand `_build_chat_app()` pre-fetched data — but
chasing that was solving the wrong problem. The real answer, once "just make Panel behave
like Chainlit's blocking start" stopped being the goal: **`_build_chat_app()` doesn't need
to block on anything.** It builds the `ChatInterface` and sends a lightweight "ClaudIA is
ready — gathering your account status…" message immediately (fast first render, user can
start typing right away), then calls `asyncio.create_task(...)` for the GDrive/doc-version/
opening-status work — safe and simple here because `_build_chat_app()` already runs *on*
the session's own live event loop (confirmed: it's invoked synchronously from within
Bokeh's own async session-creation flow, not from a separate thread), so no thread-crossing
bridge is needed, and the task closes directly over the just-created `chat`/`sink` objects
— no cross-session cache key, no registry. The task pushes the real status block into the
same chat once ready. This is a genuine UX improvement over Chainlit's blocking start, not
a compromise.

**Files:** Modify `claudia/panel_app.py`. `claudia/gdrive_sync.py`, `claudia/session_reporter.py`,
`claudia/conversation_store.py` need zero changes (per the audit) — only new call sites in
`panel_app.py`.

### Phase 5 design decisions (grounded 2026-07-23 against `app.py:215-718` + current `panel_app.py`, read in full)

**D1 — everything except the chat surface moves into the background init task.** The
resolved design above said "send a lightweight ready message immediately, then
`asyncio.create_task` the slow work." Grounding sharpens this: since the *agent* is
constructed in the background anyway, **so can the store, loader, and GDrive sync** —
`_build_chat_app()` shrinks to: build `ChatInterface`, send the instant welcome line,
register the gated callback (D2), spawn `_init_session()`. This also dissolves an ordering
constraint that would otherwise bite: the GDrive DB download **must happen before
`ConversationStore` first opens the DB file** (`app.py:246-254` guards this with
`if _conv_store is None` before the store exists — opening a stale local DB then
overwriting it is exactly the G1-G3 stale-Drive-overwrite gap class from the
info-architecture review). With store construction deferred into `_init_session()`, the
download naturally precedes it in straight-line code — no lazy-download-inside-getter
tricks needed.

**D2 — input gating via a closed-over `asyncio.Event`, not a disabled widget.** The user
can type the moment the page renders; the chat callback `await`s an `_init_done` event
before dispatching, so an early message simply shows the chat's normal thinking state
until init lands (~1-2s), then processes. A mutable holder closure (`_session: dict`)
carries `agent`/`store`/`loader` from the init task to the callback — no registry, no
`pn.state.cache` key, same closure pattern the resolved design already chose. If init
*failed*, the holder carries the error and the callback answers honestly ("Session init
failed: … — fix and reload") instead of dispatching into a half-built session. The current
skeleton's `loader.load_system_prompt()` FileNotFoundError carry-over (top of this phase)
lands here as the first failure case: caught in `_init_session()`, surfaced as the same
"**Setup required:** …" chat message `app.py:268-273` sends.

**D3 — `create_session` moves into init and gains its missing metadata.** The current
skeleton calls `store.create_session(session_id)` with no `context_hash`/`doc_version`
(`panel_app.py:86`) — a parity gap vs `app.py:322`. It moves into `_init_session()` after
doc-version registration, carrying both fields. Doc registration + hash-change WARNING
message + `_write_version_snapshot` port with it (`app.py:302-320`, `197-212`).

**D4 — thread→session delivery needs one verified Panel idiom, used twice.** Two Chainlit
context bridges must be replaced: the watchdog hot-reload alert (`app.py:280-294`,
fires from an OS thread) and — later, Phase 6 — connectivity chat alerts (fire from the
checker's poll task, not the session's context). Bokeh documents require updates under the
document lock; the correct Panel-native idiom (candidates: `doc.add_next_tick_callback`,
`pn.state.execute`, or `chat.send` thread-safety if Panel handles locking internally) must
be **verified empirically before writing the task code** — same discipline as the
`on_session_created` verification above, and per CLAUDE.md's API-Docs-First rule. Whatever
is proven becomes the single documented pattern for both call sites.

**D4 RESOLVED 2026-07-23 (empirical probe + official docs, in agreement).** Proven idiom:
capture `loop = asyncio.get_running_loop()` at session build (on the session's event
loop); the OS thread calls `loop.call_soon_threadsafe(partial(chat.send, <text>,
user="System", respond=False))`. All four candidates (loop bridge, `pn.state.execute`,
`doc.add_next_tick_callback`, even a direct `chat.send` from the thread) rendered
correctly in-browser under isolated runs AND two-message pressure tests with clean
server/console logs (probe: scratchpad `d4_probe.py` + run logs; ground truth =
Playwright browser snapshot, not server-side `chat.objects`). The loop bridge is the
standard because it is the only candidate serializing the ENTIRE `chat.send` (including
the Python-side `ChatMessage` construction / `objects` append) onto the session loop —
`pn.state.execute` from a plain thread degenerates to a direct synchronous call
(`state._curdoc` is a ContextVar, None on foreign threads — panel 1.9.3
`state.py:706-743`, probe-confirmed), with thread safety then coming only from Panel's
reactive `_apply_update` rescheduling (`reactive.py:345-364`), which protects the
Bokeh-model sync but runs the Python mutation on the caller's thread. Official docs
agree (panel.holoviz.org `how_to/callbacks/server.html` + 
`how_to/concurrency/manual_threading.html`); the nuance that `pn.state.execute` doesn't
actually cross-thread-schedule from a plain thread is recorded here deliberately.
Caveats: kwargs need `functools.partial` (`call_soon_threadsafe` is positional-only);
closed-session delivery is a harmless no-op under the process-wide uvicorn loop
(`_apply_update` short-circuits when no live views — `reactive.py:354`), but wrap the
thread-side call in try/except as hygiene against a per-session-loop topology ever
appearing.

**D5 — TradingView sidecar stays out of Phase 5** (per the phase goal above: buttons in
Phase 9). The agent runs without `extra_tools`/`tv_bridge` — both already default to
empty/None in `ClaudIAAgent`. The welcome line notes TradingView as "not connected in the
Panel preview" honestly rather than pretending.

**D6 — backend singletons start in Phase 5; their UI stays in Phase 6/7.**
`ConnectivityChecker.start()` matters beyond status dots: its 60s `/tickle` poll is the
IBKR **session keepalive** (`status.py:92-93`), which Panel sessions currently don't get —
a live-session-protection gap, not cosmetics. `ExecutionListener` likewise
(`app.py:366-377`). Both are process singletons, constructed+started in `_init_session()`
under the same `is None` guards as `app.py:348-377`. **No** per-session
`checker.subscribe(...)` yet — chat-alert delivery needs D4's verified idiom and is
exactly Phase 6's remaining work.

**D7 — session-end cleanup via `pn.state.on_session_destroyed`, verified first.** The
`_run_session_cleanup` equivalent (close session w/ metadata, `generate_session_report`,
GDrive `upload_db`, `loader.stop_watching()` — `app.py:670-700`) hooks Panel's session
destroy. The callback's exact contract (sync vs async, what session context is still
alive, whether blocking work is acceptable or needs a thread) must be verified empirically
before the task is written — same rule as D4. The "skip if End Session button already
cleaned up" guard ports as a flag in the D2 holder.

**D8 — action buttons reuse Phase 3's proven pattern.** "End Session" (always) and "Start
IBKR Gateway" (only when IBKR offline) render as `pn.widgets.Button`s in a chat message
(same `on_click` + disable-on-click pattern as `panel_order_flow.py`). "End Session" runs
D7's cleanup then confirms; "Start IBKR Gateway" ports `on_start_gateway`'s core
(`app.py:815-874`) — read it during task detailing, it wasn't part of this grounding pass.

**Task decomposition** (each to be detailed with real code before dispatch, per the
living-document protocol — outline only here):

- **Task 5.1** — `_init_session` scaffolding: shrink `_build_chat_app()` per D1, add the
  D2 event+holder gating, Setup-required failure path, GDrive singleton + DB download +
  store/loader/agent construction in background order. (The largest task; everything else
  hangs off it.)
- **Task 5.2** — Drive context/principles read + doc-version registration + hash-change
  WARNING + version snapshot + `create_session` with metadata (D3).
- **Task 5.3** — opening status block: IBKR reachability pre-check + `asyncio.gather` of
  the 4 status calls (`asyncio.to_thread` replacing `cl.make_async`), trade-data status
  line + market-calendar context → `agent._trade_context` (`app.py:399-514`).
- **Task 5.4** — verify D4's thread→session idiom empirically; then watchdog hot-reload
  wiring with it.
- **Task 5.5** — ConnectivityChecker + ExecutionListener singletons (D6).
- **Task 5.6** — verify D7's `on_session_destroyed` contract empirically; then session-end
  cleanup + End Session / Start Gateway buttons (D7+D8).
- **Task 5.7** — background Flex sync decision logic + sync + store.db Drive backup
  (`app.py:550-617`).

### Task 5.1: `_init_session` scaffolding — immediate render, background init, gated input

**✅ Completed 2026-07-23.** Commits `b3e814a` (implementation) + `1a646fd` (review
hardening). Full cycle: implement → spec review (COMPLIANT) → quality review (Approve, 3
Important + 5 Minor, all applied). Step 0's assumption proven at the strongest evidence
level (real Playwright browser confirmed the background task's `chat.send` renders in the
live page). Key hardening beyond the plan's own code: a module-level `_init_lock`
serializing GDrive-download→first-store-open across concurrent sessions — the review
caught that `asyncio.to_thread` opens an interleaving window `app.py`'s synchronous
download never had (the plan's "parity" framing was factually wrong); plus a D1
call-order regression test, Drive-failure-nonfatal test, and hang-proofed tests
(`wait_for` timeouts). Tests 3 → 7 in `test_panel_app.py`; full suite 371 → 375.

**Files:**

- Modify: `claudia/panel_app.py` (`_build_chat_app` shrinks; new `_init_session`; new
  module global `_gdrive_sync`; new import `GDriveSync`)
- Modify: `tests/test_panel_app.py` (3 existing tests rework to async; new failure-path +
  gating tests)

- [x] **Step 0: Empirically verify background-task `chat.send` reaches the rendered page**

The resolved design asserts "the task pushes the real status block into the same chat once
ready" — never actually verified. Build a throwaway probe (scratch file, not committed):
minimal `FastAPI` + `add_application` app whose build function returns a `ChatInterface`
and spawns `asyncio.create_task` of a coroutine that `await asyncio.sleep(3)` then
`chat.send("late message", user="System", respond=False)`. Serve with uvicorn, open the
page in a browser (or Playwright), confirm "late message" appears ~3s after render with no
console/server errors (Bokeh doc-lock violations raise loudly server-side). **If it does
NOT appear or errors: STOP — report BLOCKED**; the init task must then route its sends
through the D4 idiom, which changes this task's design and must go back to plan detailing.
If it works (expected — the task runs on the session's own event loop and Panel's `send`
triggers param updates that Panel schedules correctly), note the result in this step and
proceed.

- [x] **Step 1: Write the failing tests**

Rework `tests/test_panel_app.py`. The existing 3 tests assume everything happens
synchronously inside `_build_chat_app()`; after this task, only chat construction +
callback wiring + welcome message are synchronous — store/loader/agent land via
`_init_session` on the running loop, so **every test that touches init effects becomes
`@pytest.mark.asyncio`** (a running loop is now *required* even to call
`_build_chat_app()`, since it calls `asyncio.create_task`). The chat callback `await`s
init internally, so `await chat.callback(...)` is the natural synchronization point — no
need to expose the event or the task.

Full new content for the reworked/new tests (keep the module docstring; existing helper
patches stay the same shape):

```python
def _patch_happy_path(mock_toolkit, mock_store, mock_loader_cls):
    """The standard patch set for a successful init — shared by most tests."""
    return (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader", mock_loader_cls),
        patch("claudia.agent.AsyncAnthropic"),
    )


@pytest.mark.asyncio
async def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None
    mock_loader_cls = MagicMock()
    mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
    mock_loader_cls.return_value.reload_count = 0

    with contextlib.ExitStack() as stack:
        for p in _patch_happy_path(mock_toolkit, mock_store, mock_loader_cls):
            stack.enter_context(p)
        chat = _build_chat_app()

    assert chat.callback is not None


@pytest.mark.asyncio
async def test_build_chat_app_callback_waits_for_init_then_dispatches_to_agent():
    """The gating contract (design D2): a message sent immediately after render must
    wait for _init_session to finish, then reach the real agent — not error out and
    not dispatch into a half-built session."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None
    mock_loader_cls = MagicMock()
    mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
    mock_loader_cls.return_value.reload_count = 0

    with (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader", mock_loader_cls),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        # Callback awaits _init_done internally; awaiting it lets the init task run.
        await chat.callback("hello world", "User", chat)

    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello world")


@pytest.mark.asyncio
async def test_build_chat_app_constructs_sink_with_the_real_store():
    """Unchanged intent from Task 3.3's review (silent audit-trail gap if store= is
    forgotten) — now asserted after init completes."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None
    mock_loader_cls = MagicMock()
    mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
    mock_loader_cls.return_value.reload_count = 0

    with (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader", mock_loader_cls),
        patch("claudia.agent.AsyncAnthropic"),
        patch("claudia.panel_app.PanelMessageSink") as mock_sink_cls,
    ):
        chat = _build_chat_app()
        await chat.callback("ping", "User", chat)  # sync point: init has finished after this

    mock_sink_cls.assert_called_once()
    assert mock_sink_cls.call_args.kwargs["store"] is mock_store


@pytest.mark.asyncio
async def test_init_failure_missing_docs_sends_setup_required_and_callback_answers_honestly():
    """Design D2 failure path + the Phase 5 carry-over: a missing context.md must
    surface as the 'Setup required' message (parity with app.py:268-273), and a
    subsequent user message must get an honest failure reply — never a dispatch into
    a half-built session, never a raw exception."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_loader_cls = MagicMock()
    mock_loader_cls.return_value.load_system_prompt.side_effect = FileNotFoundError(
        "docs/context.md not found"
    )

    with (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader", mock_loader_cls),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        chat = _build_chat_app()
        await chat.callback("hello", "User", chat)

        texts = [
            (m.object if hasattr(m, "object") else str(m))
            for m in chat.objects
        ]
        assert any("Setup required" in str(t) for t in texts)
        assert any("Session init failed" in str(t) for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_init_unexpected_failure_reports_error_not_crash():
    """Any non-FileNotFoundError init failure must also land in the holder and produce
    an honest chat message — _init_session must never let an exception vanish into an
    unawaited task (asyncio would only log it at process exit)."""
    mock_loader_cls = MagicMock()
    mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
    mock_loader_cls.return_value.reload_count = 0

    with (
        patch("claudia.panel_app._get_toolkit", side_effect=RuntimeError("boom")),
        patch("claudia.panel_app._get_store"),
        patch("claudia.panel_app.ContextLoader", mock_loader_cls),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        chat = _build_chat_app()
        await chat.callback("hello", "User", chat)

        texts = [
            (m.object if hasattr(m, "object") else str(m))
            for m in chat.objects
        ]
        assert any("Session init failed" in str(t) for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()
```

Note `import contextlib` joins the test module's imports for the ExitStack variant, or
drop `_patch_happy_path` and inline the four patches everywhere — implementer's choice,
match whichever reads cleaner; the shown code compiles either way.

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_panel_app.py -v`
Expected: the reworked tests fail against the current synchronous `_build_chat_app`
(e.g. `create_session` called at build time, no gating, no Setup-required path; the two
new failure-path tests fail outright). The wired-callback test may still pass — fine.

- [x] **Step 3: Implement in `claudia/panel_app.py`**

New module-level pieces (imports join the existing block; global joins
`_toolkit`/`_conv_store`):

```python
import asyncio

from claudia.gdrive_sync import GDriveSync

_gdrive_sync: GDriveSync | None = None
```

Replace `_build_chat_app` in full:

```python
def _build_chat_app() -> pn.chat.ChatInterface:
    """Per-session factory: called fresh for each new browser session by Bokeh's
    _eval_panel (confirmed live against Panel 1.9.3 — see Phase 2 header note).

    Phase 5 design (see 'Phase 5 design decisions' in the migration plan): only the
    chat surface is built synchronously — everything else (GDrive download, store,
    loader, agent) runs in a background _init_session task on the session's own event
    loop, with user input gated on an asyncio.Event so an early message waits for
    init instead of racing it or erroring.
    """
    session_id = str(uuid.uuid4())
    chat = pn.chat.ChatInterface()

    _session: dict = {"agent": None, "error": None, "store": None, "loader": None}
    _init_done = asyncio.Event()

    async def _on_user_input(contents: str, user: str, instance: pn.chat.ChatInterface) -> None:
        await _init_done.wait()
        agent = _session["agent"]
        if agent is None:
            chat.send(
                f"**Session init failed:** {_session['error']} — fix the issue and "
                f"reload the page.",
                user="System",
                respond=False,
            )
            return
        try:
            await agent.handle_message(contents)
        except Exception:
            log.exception("Error handling message (session %s)", session_id)
            raise  # Panel's callback_exception="summary" still renders the friendly message

    chat.callback = _on_user_input
    chat.send(
        "**ClaudIA is ready** — gathering your account status…",
        user="ClaudIA",
        respond=False,
    )

    async def _init_session() -> None:
        global _gdrive_sync
        try:
            # GDrive DB download — MUST run before ConversationStore first opens the
            # DB file (design D1; parity with app.py:246-254's if-store-is-None guard).
            if _gdrive_sync is None and os.environ.get("GOOGLE_DRIVE_FOLDER_ID"):
                cfg = Config.from_env()
                try:
                    _gdrive_sync = GDriveSync(cfg)
                    if _conv_store is None:
                        await asyncio.to_thread(_gdrive_sync.download_db, _DB_PATH)
                except Exception as exc:
                    log.warning("GDriveSync setup failed: %s — continuing without Drive sync", exc)

            toolkit = _get_toolkit()
            store = _get_store()

            loader = ContextLoader(_DOCS_PATH)
            try:
                loader.load_system_prompt()  # validate docs exist before proceeding
            except FileNotFoundError as exc:
                _session["error"] = f"Setup required: {exc}"
                chat.send(
                    f"**Setup required:** {exc}\n\nCreate the missing file and reload.",
                    user="System",
                    respond=False,
                )
                return

            store.create_session(session_id)  # Task 5.2 adds context_hash/doc_version

            sink = PanelMessageSink(chat=chat, session_id=session_id, store=store)
            _session["store"] = store
            _session["loader"] = loader
            _session["agent"] = ClaudIAAgent(
                toolkit=toolkit,
                store=store,
                context_loader=loader,
                session_id=session_id,
                sink=sink,
                model=_MODEL,
            )
        except Exception as exc:
            log.exception("Session init failed (session %s)", session_id)
            _session["error"] = str(exc)
            chat.send(
                f"**Session init failed:** {exc} — check the server logs and reload.",
                user="System",
                respond=False,
            )
        finally:
            _init_done.set()

    # Safe here: _build_chat_app runs synchronously ON the session's live event loop
    # (verified empirically — see the Phase 5 'Resolved' note), so create_task schedules
    # onto the correct loop with no thread-crossing bridge.
    asyncio.create_task(_init_session())
    return chat
```

Delete nothing else — `_get_toolkit`/`_get_store`/`_serve_chat_app` stay as-is.

- [x] **Step 4: Run to verify pass**

Run: `pytest tests/test_panel_app.py -v` — all pass. Count before/after
(`grep -cE "^(async )?def test_" tests/test_panel_app.py`): 3 before, 5 after.

- [x] **Step 5: Full unit suite**

Run: `pytest -m "not integration" -q`
Expected: 371 baseline + 2 net new = 373, 0 failures. `ruff check` + `mypy` on both
touched files: clean.

- [x] **Step 6: Manual smoke (no gateway needed)**

`uvicorn claudia.panel_app:app --port 8001` → page renders the welcome line immediately;
a message typed instantly still gets processed (after a beat) rather than erroring —
confirms the gate. With `docs/context.md` temporarily renamed, reload → "Setup required"
message appears and a typed message gets the honest failure reply (rename back after).

- [x] **Step 7: Commit**

```bash
git add claudia/panel_app.py tests/test_panel_app.py
git commit -m "feat: Panel session init — immediate render, background init task, gated input"
```

### Task 5.2: Drive context/principles read + doc versioning + session metadata

**✅ Completed 2026-07-23.** Commits `c35d5a4` (implementation) + `c52452e` (review
hardening). Full cycle: implement → spec review (COMPLIANT, incl. adversarial pass
confirming `read_text` returns None-not-raise on Drive errors) → quality review (Approve,
2 Important + 2 Minor applied: Drive reads moved under `_init_lock` — googleapiclient
binds one non-thread-safe `httplib2.Http` per service, so concurrent session inits from
two `to_thread` workers were a real hazard; the `get_last_context_hash`-before-
`create_session` ordering invariant now has a load-bearing comment + an order assertion
whose teeth were verified by actually swapping the calls). Tests 7 → 11; suite 375 → 379.
Deferred by review recommendation to Task 5.3's start: extracting `_read_context_docs` /
`_register_doc_version` helpers before the status block lands (`_init_session` is at its
readable size limit).

Grounded 2026-07-23 against verified signatures: `ContextLoader(__init__: docs_path,
context_text=None, principles_text=None)` with `get_effective_texts() -> tuple[str, str]`
and `compute_hash() -> str` (`context_loader.py:59-94`); `GDriveSync.read_text(filename,
local_path: Path | None) -> str | None` (`gdrive_sync.py:310`);
`store.register_doc_version_if_new / get_last_context_hash / get_version_label`
(`conversation_store.py:177-207`); `create_session(session_id, context_hash="",
doc_version=None)` (`conversation_store.py:158`); `ClaudIAAgent(..., doc_version=None)`
(`agent.py:465`). Parity source: `app.py:256-322` + `_write_version_snapshot`
(`app.py:197-212`).

**Files:**

- Modify: `claudia/panel_app.py` (`_DOCS_PATH` → `Path`, new `_VERSIONS_PATH`, duplicated
  `_write_version_snapshot` helper, `_init_session` gains Drive read + versioning block)
- Modify: `tests/test_panel_app.py`

- [x] **Step 1: Write the failing tests**

The happy-path store/loader mocks in ALL existing tests need two additions or the new
init code breaks them (TDD red will show this): `mock_loader_cls.return_value.
get_effective_texts.return_value = ("ctx text", "pri text")` (a real 2-tuple — the code
unpacks it) and `.compute_hash.return_value = "hash123"`; plus
`mock_store.get_last_context_hash.return_value = None` and
`mock_store.register_doc_version_if_new.return_value = "v7"` (prevents spurious
hash-change warnings and MagicMock-as-label leakage). Patch
`claudia.panel_app._write_version_snapshot` in happy-path tests (it writes real files).

New tests:

```python
@pytest.mark.asyncio
async def test_init_registers_doc_version_and_creates_session_with_metadata():
    """Parity with app.py:302-322 (D3): doc version registered from the loader's
    effective texts, snapshot written, session row carries hash + version, agent
    constructed with doc_version."""
    # happy-path patches incl. ClaudIAAgent mocked; loader/store configured per above
    ...
    # after: await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=5)
    mock_store.register_doc_version_if_new.assert_called_once_with(
        "hash123", "ctx text", "pri text"
    )
    mock_snapshot.assert_called_once_with("v7", "ctx text", "pri text")
    _, cs_kwargs = mock_store.create_session.call_args
    assert cs_kwargs["context_hash"] == "hash123"
    assert cs_kwargs["doc_version"] == "v7"
    assert mock_agent_cls.call_args.kwargs["doc_version"] == "v7"


@pytest.mark.asyncio
async def test_init_hash_change_sends_warning():
    """Parity with app.py:310-320: a changed doc hash produces the WARNING message
    with prev → new version labels."""
    # as above but mock_store.get_last_context_hash.return_value = "oldhash"
    # and mock_store.get_version_label.return_value = "v6"
    ...
    texts = _message_texts(chat)
    assert any("WARNING" in t and "v6" in t and "v7" in t for t in texts)


@pytest.mark.asyncio
async def test_init_no_warning_when_hash_unchanged_or_first_run():
    """get_last_context_hash None (first run) → no WARNING."""
    ...
    assert not any("WARNING" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_reads_context_docs_from_drive_when_sync_available():
    """The user-confirmed requirement: context.md/principles.md live in Google Drive —
    every session reads them via GDriveSync.read_text (local fallback inside read_text),
    and the loader is constructed with the Drive texts (app.py:256-265 parity)."""
    # monkeypatch claudia.panel_app._gdrive_sync to a MagicMock whose read_text
    # side-effects: "context.md" -> "drive ctx", "principles.md" -> "drive pri"
    ...
    assert mock_loader_cls.call_args.kwargs["context_text"] == "drive ctx"
    assert mock_loader_cls.call_args.kwargs["principles_text"] == "drive pri"
```

(The `...` bodies above are the standard build+patch+callback-sync scaffold every existing
test already uses — implementer fills them mechanically from the surrounding file; the
assertions shown are the complete contract. This is scaffold-reuse, not a placeholder.)

- [x] **Step 2: Run to verify failure** — `pytest tests/test_panel_app.py -v`: new tests
  fail; pre-existing happy tests may also fail red until Step 3 (unpacking
  `get_effective_texts`).

- [x] **Step 3: Implement in `claudia/panel_app.py`**

Module level: `_DOCS_PATH = Path(os.environ.get("CLAUDIA_DOCS_PATH", "docs"))` (matches
`app.py:61` and the `_DB_PATH` precedent from Task 5.1); `_VERSIONS_PATH = _DOCS_PATH /
"versions"`. Duplicate `_write_version_snapshot` verbatim from `app.py:197-212` with a
comment noting the deliberate duplication-for-independence (same rationale as
`_get_toolkit`'s existing docstring — panel_app must not import claudia.app, which pulls
in chainlit).

In `_init_session`, AFTER the `_init_lock` block (race-free: any session reaching here has
passed through the lock, so `_gdrive_sync` setup is complete) and BEFORE the existing
`ContextLoader` construction — replace the plain `loader = ContextLoader(_DOCS_PATH)` with:

```python
            # Read context/principles from Drive every session so each session picks up
            # the latest version (app.py:256-262 parity; read_text falls back to the
            # local file itself when Drive is unreachable or the file is absent).
            drive_context: str | None = None
            drive_principles: str | None = None
            if _gdrive_sync is not None:
                drive_context = await asyncio.to_thread(
                    _gdrive_sync.read_text, "context.md", _DOCS_PATH / "context.md"
                )
                drive_principles = await asyncio.to_thread(
                    _gdrive_sync.read_text, "principles.md", _DOCS_PATH / "principles.md"
                )

            loader = ContextLoader(
                _DOCS_PATH, context_text=drive_context, principles_text=drive_principles
            )
```

Then AFTER the FileNotFoundError guard, REPLACE the bare
`store.create_session(session_id)  # Task 5.2 ...` line with (order matters —
`get_last_context_hash` must run BEFORE this session's row is inserted, else it sees its
own hash; same order as `app.py:302-322`):

```python
            # Register document version (idempotent) + snapshot + hash-change alert
            context_text, principles_text = loader.get_effective_texts()
            current_hash = loader.compute_hash()
            version_label = store.register_doc_version_if_new(
                current_hash, context_text, principles_text
            )
            log.info("Active document version: %s", version_label)
            _write_version_snapshot(version_label, context_text, principles_text)

            prev_hash = store.get_last_context_hash()
            if prev_hash is not None and prev_hash != current_hash:
                prev_version = store.get_version_label(prev_hash) or f"unknown ({prev_hash[:8]})"
                chat.send(
                    f"**WARNING: context.md / principles.md changed: "
                    f"{prev_version} → {version_label}.**\n"
                    "Please verify the content before continuing.",
                    user="System",
                    respond=False,
                )

            store.create_session(
                session_id, context_hash=current_hash, doc_version=version_label
            )
```

And add `doc_version=version_label` to the `ClaudIAAgent(...)` construction.

- [x] **Step 4: Run to verify pass** — `pytest tests/test_panel_app.py -v`; count 7 → 11.
- [x] **Step 5: Full suite** — `pytest -m "not integration" -q`: 375 + 4 = 379, 0
  failures; `ruff` + `mypy` clean.
- [x] **Step 6: Commit** — exactly the two files:

```bash
git commit -m "feat: Panel sessions read context docs from Drive + register doc versions"
```

### Task 5.3: Opening status block — helper extraction, then the status port

**✅ Completed 2026-07-23.** Commits `b5e4097` (pure refactor: `_read_context_docs` /
`_register_doc_version` extracted from `_init_session`), `d6cf92b` (opening status block:
new UI-free `claudia/opening_status.py` + `_send_opening_status` wiring), `4bf5a93`
(review hardening). Full cycle: implement → spec review (COMPLIANT — reviewer mechanically
tokenized every string literal against app.py:399-514 and confirmed character-level
parity; refactor commit verified test-free and behavior-preserving at its own SHA) →
quality review (Approve-with-fixes: 1 Important + 5 Minor, all applied). Key hardening:
the `ibkr_offline` plumbing from `gather_status_block` into `build_trade_lines` was
unpinned — a hardcoded `False` would have passed the whole suite while silently dropping
the "connect IBKR to refresh" note; now pinned in both directions
(`assert_called_once_with(toolkit, False)` + a dedicated `(…, True)` flow test). Also:
no-data fixture re-anchored to the REAL empty-store dict (store.py:355 — it has `gaps`,
not `days_since_newest`); `_send_opening_status` restructured to return `trade_context`
instead of mutating the agent (single responsibility — the stamp now sits visibly next to
the `_session["agent"]` publish); degrade paths gained `log.warning` diagnosability with
zero user-visible string changes. Tests 11 → 13 in test_panel_app.py + 9 new in
test_opening_status.py; suite 379 → 390. Spec-review observation on file (non-blocking):
the publish-after-stamp ordering is defense-in-depth — the `_init_done` gate already
prevents the hazard; it becomes load-bearing only if the gate ever changes to
"agent is not None".

Grounded 2026-07-23 against verified signatures: `IBKRClient.ping() -> bool`
(`ibkr_core_mcp/client.py:178` — verifies authentication, not just reachability; retries
once internally for the IBKR fresh-session `authenticated=false` quirk);
`ClaudeToolkit.execute(name, inputs) -> tuple[str, None]` (`claude_tools.py:1048`);
`get_live_pnl_text(toolkit) -> str` (`claudia/execution_listener.py:88` — module verified
chainlit-free, safe for panel_app to import); `SQLiteStore.get_trade_date_coverage(
gap_threshold_days=45) -> dict` (`ibkr_core_mcp/store.py:335`) and
`get_market_calendar_context` (`store.py:407`). `ClaudeToolkit` exposes `client` and
`tools` properties but NO public `config`/`store` — use `toolkit._config` /
`toolkit._store`, the same sanctioned reach-in `app.py` itself uses (`app.py:433,466`).
Parity source: `app.py:399-514`.

**Design decisions (locked at detailing):**

- The ~110 lines of status/trade/calendar logic land in a NEW UI-free module
  `claudia/opening_status.py` (three functions), NOT inline in `_init_session` — the Task
  5.2 quality review flagged `_init_session` at its readable size limit, and pure
  functions let tests feed dict fixtures directly instead of driving everything through
  the init task. `app.py` is deliberately left untouched (deleted wholesale at Phase 11).
- Task starts with the review-mandated **pure refactor**: extract `_read_context_docs()`
  and `_register_doc_version()` from `_init_session` (behavior-preserving, own commit,
  all 11 existing tests pass unchanged) before any new feature code.
- Status gathering runs INSIDE `_init_session` before `_init_done.set()` — parity with
  app.py, where `agent._trade_context` is always stamped before the first user message
  can be processed. An agent published to `_session` without its trade context would
  silently answer without trade-history grounding — the exact class of silent gap this
  project treats as non-negotiable — so `_session["agent"]` is assigned only AFTER
  `_send_opening_status` completes.
- The Panel welcome already says "gathering your account status…"; the status block
  arrives as a second `chat.send`. Its tail carries the honest D5 TradingView note
  ("not connected in the Panel preview" — Phase 9 replaces it).
- Port subtlety that MUST be preserved: the market-calendar block appends to
  `trade_context` even when Flex is unconfigured (`app.py:511` does
  `(trade_context or "") + _cal_block`).
- The 4-way `asyncio.gather` over `asyncio.to_thread` replaces `cl.make_async` — same
  thread-pool parallelism against `IBKRClient` as app.py, no new concurrency hazard.

**Files:**

- Create: `claudia/opening_status.py`
- Create: `tests/test_opening_status.py`
- Modify: `claudia/panel_app.py` (refactor extraction + `_send_opening_status` wiring)
- Modify: `tests/test_panel_app.py` (1 new integration test; 9 existing tests gain a
  `_send_opening_status` patch line)

- [x] **Step 1: Pure refactor — extract `_read_context_docs` / `_register_doc_version`**

In `claudia/panel_app.py`, add the two module-level helpers (between
`_write_version_snapshot` and `_build_chat_app`):

```python
async def _read_context_docs() -> tuple[str | None, str | None]:
    """Read context.md/principles.md via Drive (read_text falls back to the local
    file when Drive is unreachable or the file is absent) — app.py:256-262 parity.
    MUST be called while holding _init_lock: googleapiclient binds a single
    AuthorizedHttp/httplib2.Http to the built Drive service, shared by every
    .execute(), and httplib2.Http is not thread-safe — concurrent session inits
    would run read_text on that one connection from two worker threads (worst
    case: interleaved socket reads that still parse, handing a session the wrong
    document content silently). Serializing the per-session reads costs ~nothing
    for a single-user app."""
    if _gdrive_sync is None:
        return None, None
    drive_context = await asyncio.to_thread(
        _gdrive_sync.read_text,
        "context.md",
        local_path=_DOCS_PATH / "context.md",
    )
    drive_principles = await asyncio.to_thread(
        _gdrive_sync.read_text,
        "principles.md",
        local_path=_DOCS_PATH / "principles.md",
    )
    return drive_context, drive_principles


def _register_doc_version(
    store: ConversationStore, loader: ContextLoader
) -> tuple[str, str, str | None]:
    """Register the current doc version (idempotent), write the human-readable
    snapshot, and detect a hash change vs the previous session. Returns
    (current_hash, version_label, hash_change_warning_or_None) — UI-free by
    design: the caller decides how to surface the warning.

    ORDERING INVARIANT: get_last_context_hash reads the newest session row, so
    this helper must run BEFORE the session's own create_session — inserting the
    new row first would make it see its own hash and the security warning would
    never fire again."""
    context_text, principles_text = loader.get_effective_texts()
    current_hash = loader.compute_hash()
    version_label = store.register_doc_version_if_new(
        current_hash, context_text, principles_text
    )
    log.info("Active document version: %s", version_label)
    _write_version_snapshot(version_label, context_text, principles_text)

    warning: str | None = None
    prev_hash = store.get_last_context_hash()
    if prev_hash is not None and prev_hash != current_hash:
        prev_version = store.get_version_label(prev_hash) or f"unknown ({prev_hash[:8]})"
        warning = (
            f"**WARNING: context.md / principles.md changed: "
            f"{prev_version} → {version_label}.**\n"
            "Please verify the content before continuing."
        )
    return current_hash, version_label, warning
```

Then in `_init_session`, replace the inline Drive-read block (the
`drive_context/drive_principles` assignments inside the lock) with:

```python
                drive_context, drive_principles = await _read_context_docs()
```

and replace the inline versioning block (from `context_text, principles_text =
loader.get_effective_texts()` through the WARNING `chat.send`) with:

```python
            # Must run BEFORE this session's create_session below (see the
            # ordering invariant in _register_doc_version's docstring).
            current_hash, version_label, warning = _register_doc_version(store, loader)
            if warning is not None:
                chat.send(warning, user="System", respond=False)
```

`store.create_session(...)` and the agent construction keep using `current_hash` /
`version_label` exactly as before. The big httplib2 comment moves INTO
`_read_context_docs`'s docstring (shown above) — leave a one-line pointer at the lock's
call site ("Drive reads must stay under the lock — see _read_context_docs").

- [x] **Step 2: Verify the refactor is behavior-preserving, then commit**

Run: `pytest tests/test_panel_app.py -v` → all 11 pass UNCHANGED (they patch
`ContextLoader`/`_write_version_snapshot` at module level and assert on
`mock_store.mock_calls` order — the helper calls the same names in the same order).
Run: `pytest -m "not integration" -q` → 379 pass. `ruff check claudia/panel_app.py` +
`mypy claudia/panel_app.py` → clean.

```bash
git add claudia/panel_app.py
git commit -m "refactor: extract _read_context_docs/_register_doc_version from _init_session"
```

- [x] **Step 3: Write the failing tests for `claudia/opening_status.py`**

Create `tests/test_opening_status.py`:

```python
"""Tests for claudia/opening_status.py — UI-free builders for the Panel opening
status message (Task 5.3). Fixtures mirror the real shapes: toolkit.execute
returns (text, None) 2-tuples (claude_tools.py:1048); get_trade_date_coverage /
get_market_calendar_context return the dict shapes app.py:426-513 consumes
(the port's parity source)."""

from unittest.mock import MagicMock, patch

import pytest

from claudia.opening_status import (
    OFFLINE_STATUS,
    build_trade_lines,
    gather_status_block,
)


def _make_toolkit(flex: bool = True) -> MagicMock:
    toolkit = MagicMock()
    toolkit._config.flex_token = "tok" if flex else ""
    toolkit._config.flex_query_id = "qid" if flex else ""
    toolkit._store.get_market_calendar_context.return_value = None
    return toolkit


_MKT = {
    "today": "2026-07-23",
    "is_trading_day": True,
    "last_trading_day": "2026-07-22",
    "next_trading_day": "2026-07-24",
    "holidays_by_exchange": {"XNYS": ["2026-12-25"], "CME": []},
    "futures": {
        "note": "CME futures trade nearly 23h/day.",
        "maintenance_break_ct": "16:00-17:00 CT",
        "cme_open_nyse_closed": ["2026-11-27"],
        "product_groups": {
            "equity_index": {
                "exchange": "CME",
                "globex_hours_ct": "17:00-16:00",
                "products": ["ES", "NQ", "YM", "RTY", "MES"],
                "note": "daily maintenance 16:00-17:00",
            }
        },
    },
}


@pytest.mark.asyncio
async def test_gather_status_block_happy_path_contains_all_four_sections():
    toolkit = MagicMock()
    toolkit.client.ping.return_value = True
    toolkit.execute.side_effect = lambda name, inputs: (f"{name} text", None)
    with patch("claudia.opening_status.get_live_pnl_text", return_value="pnl text"):
        block, offline = await gather_status_block(toolkit)
    assert offline is False
    assert "**Account Summary**\nget_account_summary text" in block
    assert "**Open Positions**\nget_positions text" in block
    assert "**Account P&L**\npnl text" in block
    assert "**Live Orders**\nget_live_orders text" in block


@pytest.mark.asyncio
async def test_gather_status_block_offline_when_ping_false():
    """ping() returning False means unreachable/unauthenticated — the 4 status
    calls must be SKIPPED entirely (toolkit.execute swallows exceptions into
    error strings, so calling it offline would render 4 error blobs)."""
    toolkit = MagicMock()
    toolkit.client.ping.return_value = False
    block, offline = await gather_status_block(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS
    toolkit.execute.assert_not_called()


@pytest.mark.asyncio
async def test_gather_status_block_offline_when_ping_raises():
    toolkit = MagicMock()
    toolkit.client.ping.side_effect = ConnectionError("gateway down")
    block, offline = await gather_status_block(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS


def test_build_trade_lines_flex_not_configured_still_appends_calendar():
    toolkit = _make_toolkit(flex=False)
    toolkit._store.get_market_calendar_context.return_value = _MKT
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "Flex not configured" in status
    toolkit._store.get_trade_date_coverage.assert_not_called()
    # app.py:511 subtlety: the calendar block lands in trade_context even when
    # Flex is unconfigured — (trade_context or "") + _cal_block.
    assert context is not None
    assert "## Market Calendar" in context
    assert "NYSE: 2026-12-25" in context
    assert "CME Futures: no holidays this year/next" in context
    assert "Equity Index (CME): 17:00-16:00 [ES, NQ, YM, RTY…]" in context
    assert "CME open when NYSE is closed: 2026-11-27" in context


def test_build_trade_lines_flex_configured_with_data():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": "2024-01-02",
        "newest": "2026-07-22",
        "total_trades": 1234,
        "days_since_newest": 1,
    }
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "1234 trades" in status
    assert "last refreshed 2026-07-22" in status
    assert "connect IBKR to refresh" not in status
    assert context is not None
    assert "## Trade History" in context
    assert "1234 executions from 2024-01-02 to 2026-07-22" in context


def test_build_trade_lines_offline_notes_connect_to_refresh():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": "2024-01-02",
        "newest": "2026-07-22",
        "total_trades": 1234,
        "days_since_newest": 1,
    }
    status, _context = build_trade_lines(toolkit, ibkr_offline=True)
    assert "(1d ago) — connect IBKR to refresh" in status


def test_build_trade_lines_no_data_yet():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": None,
        "newest": None,
        "total_trades": 0,
        "days_since_newest": None,
    }
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "no data yet" in status
    assert context is not None
    assert "sync_flex_trades" in context


def test_build_trade_lines_coverage_error_degrades_to_syncing():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.side_effect = RuntimeError("db locked")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert status == "Trade history: syncing…"
    assert context is None  # calendar mock returns None → nothing appended


def test_build_trade_lines_calendar_error_is_swallowed():
    toolkit = _make_toolkit(flex=False)
    toolkit._store.get_market_calendar_context.side_effect = RuntimeError("boom")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "Flex not configured" in status
    assert context is None
```

Run: `pytest tests/test_opening_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudia.opening_status'`.

- [x] **Step 4: Implement `claudia/opening_status.py`**

```python
"""UI-free builders for the opening status message (Panel entry point).

Faithful port of the Chainlit startup status logic (app.py:399-514) restructured
into pure/thread-friendly functions so panel_app._init_session stays readable and
tests can feed dict fixtures directly. app.py is deliberately left untouched — it
is deleted wholesale at Phase 11 (cutover). Uses toolkit._config/_store — the
same sanctioned reach-in app.py itself uses (app.py:433,466); ClaudeToolkit
exposes no public config/store properties.
"""

import asyncio
import logging
from typing import Any

from ibkr_core_mcp import ClaudeToolkit

from claudia.execution_listener import get_live_pnl_text

log = logging.getLogger(__name__)

OFFLINE_STATUS = "*IBKR gateway not connected — data will load when gateway is online.*"

_EXCHANGE_LABELS = {
    "XNYS": "NYSE", "CME": "CME Futures",
    "XLON": "LSE London", "XETR": "Xetra Frankfurt", "XEUR": "Eurex",
    "XPAR": "Euronext Paris", "XMIL": "Borsa Italiana",
    "XTKS": "TSE Tokyo", "XHKG": "HKEX Hong Kong", "XSHG": "SSE Shanghai",
    "XBOM": "BSE Mumbai", "XKRX": "KRX Seoul", "XASX": "ASX Sydney",
    "XTSE": "TSX Toronto", "BVMF": "B3 São Paulo", "XMEX": "BMV Mexico City",
    "XJSE": "JSE Johannesburg", "XSAU": "Tadawul (Sun–Thu week)",  # noqa: RUF001 — correct en-dash for a day range
    "XIDX": "IDX Jakarta", "XIST": "Borsa Istanbul",
}


async def gather_status_block(toolkit: ClaudeToolkit) -> tuple[str, bool]:
    """(status_block_markdown, ibkr_offline).

    toolkit.execute() swallows all exceptions and returns an error string instead
    of raising, so we pre-check reachability and skip the calls when the gateway
    is unreachable. ping() verifies authentication (not just reachability); it
    retries once internally for the IBKR first-call quirk where
    authenticated=false on a fresh session. The 4-way gather over to_thread
    matches app.py's cl.make_async concurrency exactly (same thread-pool
    parallelism against IBKRClient — no new hazard)."""
    try:
        gateway_up = await asyncio.to_thread(toolkit.client.ping)
        if not gateway_up:
            raise ConnectionError("IBKR gateway not reachable")
        (opening_text, _), (orders_text, _), (positions_text, _), pnl_text = (
            await asyncio.gather(
                asyncio.to_thread(toolkit.execute, "get_account_summary", {}),
                asyncio.to_thread(toolkit.execute, "get_live_orders", {}),
                asyncio.to_thread(toolkit.execute, "get_positions", {}),
                asyncio.to_thread(get_live_pnl_text, toolkit),
            )
        )
        return (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Account P&L**\n{pnl_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        ), False
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        return OFFLINE_STATUS, True


def build_trade_lines(toolkit: ClaudeToolkit, ibkr_offline: bool) -> tuple[str, str | None]:
    """(trade_status_line, trade_context_or_None) — the welcome status line and
    the system-prompt trade/calendar context for agent._trade_context.

    Blocking (SQLite reads) — call via asyncio.to_thread. Port of app.py:426-513,
    including the subtlety that the market-calendar block appends to
    trade_context even when Flex is unconfigured."""
    config = toolkit._config
    flex_configured = bool(config and config.flex_token and config.flex_query_id)
    trade_context: str | None = None
    if flex_configured:
        try:
            cov = toolkit._store.get_trade_date_coverage()
            if cov["oldest"]:
                if ibkr_offline:
                    days = cov["days_since_newest"]
                    sync_note = f"last refreshed {cov['newest']} ({days}d ago) — connect IBKR to refresh"
                else:
                    sync_note = f"last refreshed {cov['newest']}"
                trade_status = f"Historical dataset loaded: {cov['total_trades']} trades ({cov['oldest']} → {cov['newest']}, integrity validated) — {sync_note}"
                trade_context = (
                    f"## Trade History (local store — integrity validated)\n"
                    f"{cov['total_trades']} executions from {cov['oldest']} to {cov['newest']}. "
                    f"Last refreshed: {cov['newest']}. Dataset is complete and verified — no missing imports.\n"
                    f"Flex data lags 1 day (T+1). Newest entry being yesterday is normal, not stale. "
                    f"Do not flag the data as stale or suggest syncing unless the user explicitly asks "
                    f"or days_since_newest > 3 on a weekday.\n"
                    f"Date gaps in the dataset are verified inactivity periods (no trading). "
                    f"Do not mention gaps or suggest XML backfill unless the user specifically asks about data integrity.\n"
                    f"Use `get_trades` (default: source='store') for any analysis beyond 6 days. "
                    f"Today's intraday trades: use `get_trades source='live'`."
                )
            else:
                trade_status = "Trade history: no data yet — syncing…"
                trade_context = (
                    "## Trade History (local store)\n"
                    "No trade data yet in the local store. Run `sync_flex_trades` to import recent data, "
                    "or `sync_flex_archive` to import historical XMLs from Drive."
                )
        except Exception:
            trade_status = "Trade history: syncing…"
    else:
        trade_status = "Trade history: Flex not configured (set IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID)"

    # Append market calendar context (holidays, last/next trading day, futures
    # schedule). app.py:511 parity: appends even when trade_context is None.
    try:
        mkt = toolkit._store.get_market_calendar_context()
        if mkt:
            trade_context = (trade_context or "") + _format_market_calendar(mkt)
    except Exception:
        pass
    return trade_status, trade_context


def _format_market_calendar(mkt: dict[str, Any]) -> str:
    """Pure formatting of get_market_calendar_context's dict → the '## Market
    Calendar' system-prompt block (verbatim app.py:468-510 port)."""
    holiday_lines = []
    for xcode, holidays in mkt.get("holidays_by_exchange", {}).items():
        name = _EXCHANGE_LABELS.get(xcode, xcode)
        holiday_lines.append(
            f"{name}: {', '.join(holidays)}" if holidays else f"{name}: no holidays this year/next"
        )

    fut = mkt.get("futures", {})
    cme_extra = fut.get("cme_open_nyse_closed", [])
    group_lines = []
    for gname, g in fut.get("product_groups", {}).items():
        syms = ", ".join(g["products"][:4]) + ("…" if len(g["products"]) > 4 else "")
        group_lines.append(
            f"  {gname.replace('_', ' ').title()} ({g['exchange']}): "
            f"{g['globex_hours_ct']} [{syms}]"
            + (f" — {g['note']}" if "note" in g else "")
        )

    return (
        f"\n\n## Market Calendar\n"
        f"Today: {mkt['today']} ({'trading day' if mkt['is_trading_day'] else 'non-trading day'} on NYSE).\n"
        f"Last trading day (NYSE): {mkt['last_trading_day']}. "
        f"Next trading day (NYSE): {mkt['next_trading_day']}.\n\n"
        f"### Exchange Holidays (current + next year)\n" +
        "\n".join(f"  - {line}" for line in holiday_lines) + "\n\n"
        f"### Futures vs Securities — Key Distinction\n"
        f"{fut.get('note', '')}\n"
        f"Maintenance break: {fut.get('maintenance_break_ct', 'N/A')}\n"
        f"CME open when NYSE is closed: {', '.join(cme_extra) if cme_extra else 'none this period'}\n\n"
        f"### CME Globex Product Schedule (all times CT)\n" +
        "\n".join(group_lines) + "\n"
    )
```

Run: `pytest tests/test_opening_status.py -v` → 9/9 pass.

- [x] **Step 5: Write the failing integration test in `tests/test_panel_app.py`**

```python
@pytest.mark.asyncio
async def test_init_sends_opening_status_and_stamps_trade_context():
    """Task 5.3: after the agent is built, init must send the status message
    (status block + trade status line) and stamp agent._trade_context BEFORE the
    input gate opens (app.py:399-514 parity) — an agent published without its
    trade context would silently answer without trade-history grounding."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch(
            "claudia.panel_app.gather_status_block",
            new=AsyncMock(return_value=("STATUS BLOCK", False)),
        ),
        patch(
            "claudia.panel_app.build_trade_lines",
            return_value=("trade status line", "TRADE CTX"),
        ),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("STATUS BLOCK" in t and "trade status line" in t for t in texts)
    assert mock_agent_cls.return_value._trade_context == "TRADE CTX"
    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")
```

Run: `pytest tests/test_panel_app.py::test_init_sends_opening_status_and_stamps_trade_context -v`
Expected: FAIL — `AttributeError: <module 'claudia.panel_app'> does not have the
attribute 'gather_status_block'` (patch of a name that doesn't exist yet).

- [x] **Step 6: Implement the panel_app wiring**

In `claudia/panel_app.py` — new import:

```python
from claudia.opening_status import build_trade_lines, gather_status_block
```

New module-level helper (after `_register_doc_version`):

```python
async def _send_opening_status(
    chat: pn.chat.ChatInterface, toolkit: ClaudeToolkit, agent: ClaudIAAgent
) -> None:
    """Second chat message with live account status + trade/calendar context
    (Task 5.3 — app.py:399-514 parity). Effectively non-raising: both builders
    catch their own IBKR/store failures internally and degrade to offline/
    fallback text; an unexpected escape is caught by _init_session's generic
    handler."""
    status_block, ibkr_offline = await gather_status_block(toolkit)
    trade_status, trade_context = await asyncio.to_thread(
        build_trade_lines, toolkit, ibkr_offline
    )
    agent._trade_context = trade_context
    chat.send(
        f"{status_block}\n\n_{trade_status}_\n\n"
        "_TradingView: not connected in the Panel preview._",
        user="ClaudIA",
        respond=False,
    )
```

In `_init_session`, replace the direct `_session["agent"] = ClaudIAAgent(...)`
assignment with a local variable, and publish it only AFTER the status send:

```python
            agent = ClaudIAAgent(
                toolkit=toolkit,
                store=store,
                context_loader=loader,
                session_id=session_id,
                sink=sink,
                model=_MODEL,
                doc_version=version_label,
            )
            # Stamp trade context + send the status message BEFORE publishing the
            # agent: an agent visible to the input gate without _trade_context
            # would silently answer without trade-history grounding.
            await _send_opening_status(chat, toolkit, agent)
            _session["agent"] = agent
```

Then add `patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),` to
the patch stack of the 9 EXISTING tests whose init completes (wired-callback, gating,
sink, D1-order, drive-fail, doc-version-metadata, hash-warning, no-warning, drive-read) —
NOT to the two failure-path tests (missing-docs, unexpected-failure), whose init never
reaches the status code. Without the patch those 9 tests would still pass via the
offline-degrade path, but only through incidental MagicMock behavior (unpacking a
MagicMock raises TypeError inside gather_status_block's try) — patching keeps them
focused and deterministic.

Run: `pytest tests/test_panel_app.py -v` → 12/12 pass.

- [x] **Step 7: Full suite + linters**

Run: `pytest -m "not integration" -q`
Expected: 379 baseline + 9 (opening_status) + 1 (integration) = 389, 0 failures.
`ruff check claudia/opening_status.py claudia/panel_app.py tests/test_opening_status.py
tests/test_panel_app.py` + `mypy claudia/opening_status.py claudia/panel_app.py` → clean.

- [x] **Step 8: Manual smoke (no gateway needed)**

`uvicorn claudia.panel_app:app --port 8001` → welcome line renders immediately; a beat
later the status message arrives with "*IBKR gateway not connected…*" (offline fallback —
gateway is down), a real trade-status line (Flex + SQLite work offline), and the
TradingView note. No server-side errors.

- [x] **Step 9: Commit**

```bash
git add claudia/opening_status.py claudia/panel_app.py tests/test_opening_status.py tests/test_panel_app.py
git commit -m "feat: Panel opening status block — account/positions/P&L/orders + trade & calendar context"
```

### Task 5.4: Watchdog hot-reload alert via the D4 loop bridge

**✅ Completed 2026-07-23.** Commit `24a19aa`, single-commit full cycle: implement → spec
review (COMPLIANT — insertion point, byte-identical alert text vs app.py:286-287, and
both gates re-verified independently) → quality review (**Approve** outright; the alert
tests were re-run 5x with no flakes and both timing constructs proven deterministic, not
racy — `call_soon_threadsafe` enqueues before the thread returns, so any yield runs it).
Live smoke: Playwright browser rendered the real "**Document updated:** `context.md`
reloaded." alert ~3s after `touch docs/context.md`, clean logs. Tests 13 → 15; suite
390 → 392. **Riders recorded for Task 5.6** (from both reviews, deferrable but must land
with cleanup): (1) session-end cleanup must also cover the init-FAILURE path — if
anything after `start_watching` raises, the watch leaks with a live closure over `chat`;
(2) the watchdog **shared-ObservedWatch unschedule trap**: `ObservedWatch.__eq__`
compares by (path, recursive) and `BaseObserver.unschedule` deletes ALL handlers for
that key — a naive `loader.stop_watching()` on one session's end would silently kill
hot-reload alerts in every other live session (pre-exists in app.py's `on_chat_end`
too; Task 5.6 must verify actual multi-session behavior of `stop_watching` before
wiring it); (3) optional: wrap the deferred `chat.send` in a session-tagged function
instead of a bare partial so a send failure logs with session_id; (4) optional
test-teeth: assert the "Principles apply from your next message." tail too.

Grounded 2026-07-23: `ContextLoader.start_watching(on_reload: Callable[[str, str], None])`
(`context_loader.py:104` — registers on the shared module-level Observer, per-instance
handlers, so multiple concurrent sessions each watching their own loader is safe;
`stop_watching()` at `:121` is Task 5.6's cleanup concern). The callback receives
`(filename, new_prompt)`; like app.py's `_on_doc_change` (`app.py:283-292`), we ignore
`new_prompt` — the loader has already applied the reload internally; the callback's only
job is the user-visible alert. The delivery idiom is D4's proven loop bridge (see the
"D4 RESOLVED" note above — probe + official docs in agreement).

**Design notes:**

- The watcher starts in `_init_session` immediately after the FileNotFoundError guard
  (app.py parity position: watching begins as soon as the docs are known-valid, before
  versioning — a reload alert during the rest of init is acceptable and correct).
- `loop` is captured inside `_init_session` (which runs ON the session's event loop);
  the thread-side call is wrapped in try/except per the D4 caveat (hygiene against a
  per-session-loop topology; under uvicorn's process-wide loop a closed-session delivery
  is already a harmless no-op).
- Watcher lifecycle: `loader` is already stored in `_session["loader"]` (Task 5.1);
  Task 5.6's session-end cleanup calls `stop_watching()`. Until 5.6 lands, a closed
  session's watch persists until process exit — same interim state app.py would have
  without its `on_chat_end`, accepted for the transition window.

**Files:**

- Modify: `claudia/panel_app.py` (`functools.partial` import; watcher block in
  `_init_session`)
- Modify: `tests/test_panel_app.py` (2 new tests)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_panel_app.py` (existing helpers/patch idioms; `threading` needs
adding to the imports):

```python
@pytest.mark.asyncio
async def test_init_starts_doc_watcher_with_alert_callback():
    """Task 5.4: init must register a hot-reload callback on the loader
    (app.py:275-294 parity)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    loader = mock_loader_cls.return_value
    loader.start_watching.assert_called_once()
    assert callable(loader.start_watching.call_args.args[0])


@pytest.mark.asyncio
async def test_doc_change_callback_delivers_alert_from_a_plain_thread():
    """The D4-verified loop bridge: the watchdog callback fires in a plain OS
    thread with no asyncio/Bokeh context; the alert must still land in the chat
    via loop.call_soon_threadsafe. The test invokes the REAL registered callback
    from a real thread — if the bridge is replaced with a naive chat.send-only
    callback this still passes (direct sends work too, per the D4 probe), but if
    the callback raises on a foreign thread or the partial wiring breaks, the
    alert never renders and this fails."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        on_reload = mock_loader_cls.return_value.start_watching.call_args.args[0]
        t = threading.Thread(target=on_reload, args=("context.md", "new prompt text"))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        # call_soon_threadsafe scheduled the send onto THIS loop — yield to run it.
        await asyncio.sleep(0.05)

    texts = _message_texts(chat)
    assert any("Document updated" in t_ and "context.md" in t_ for t_ in texts)
```

Run: `pytest tests/test_panel_app.py -k "watcher or doc_change" -v`
Expected: both FAIL (`start_watching` never called).

- [x] **Step 2: Implement the watcher block**

`claudia/panel_app.py` — add `from functools import partial` to the stdlib imports. In
`_init_session`, immediately after the FileNotFoundError guard's `return` (i.e. once the
docs are known-valid), insert:

```python
            # Hot-reload alert (app.py:275-294 parity). The watchdog fires in a
            # plain OS thread; the D4-verified loop bridge serializes the entire
            # chat.send onto this session's event loop (see the D4 RESOLVED note
            # in the migration plan — probe + official Panel docs in agreement).
            loop = asyncio.get_running_loop()

            def _on_doc_change(filename: str, new_prompt: str) -> None:
                try:
                    loop.call_soon_threadsafe(
                        partial(
                            chat.send,
                            f"**Document updated:** `{filename}` reloaded. "
                            "Principles apply from your next message.",
                            user="System",
                            respond=False,
                        )
                    )
                except RuntimeError:  # loop closed — session gone, alert moot
                    log.debug("Dropped doc-change alert for closed session %s", session_id)

            loader.start_watching(_on_doc_change)
```

- [x] **Step 3: Run tests to verify pass**

Run: `pytest tests/test_panel_app.py -v` → 15/15 pass.

- [x] **Step 4: Full suite + linters**

Run: `pytest -m "not integration" -q` → 390 + 2 = 392, 0 failures.
`ruff check claudia/panel_app.py tests/test_panel_app.py` + `mypy claudia/panel_app.py`
→ clean.

- [x] **Step 5: Manual smoke (no gateway needed)**

`uvicorn claudia.panel_app:app --port 8001`, open http://localhost:8001/ with the
Playwright browser, wait for the welcome + status messages, then `touch docs/context.md`
(mtime change is enough for watchdog's on_modified; content untouched). Snapshot within
~3s: the "**Document updated:** `context.md` reloaded." message renders in the page.
No server tracebacks. Kill the server.

- [x] **Step 6: Commit**

```bash
git add claudia/panel_app.py tests/test_panel_app.py
git commit -m "feat: Panel hot-reload alert via D4 loop bridge (watchdog thread -> session chat)"
```

### Task 5.5: ConnectivityChecker + ExecutionListener singletons (D6)

**✅ Completed 2026-07-23.** Commits `7bdd9b3` (implementation) + `05ac71b` (review
hardening). Full cycle: implement → spec review (COMPLIANT; adversarial pass caught the
suite-wide hygiene gap below) → quality review (Approve-with-fixes: 1 Important + 1
Minor, both applied). The smoke test exceeded plan: the gateway container was up
(unauthenticated, `/tickle` → 401), so the checker's keepalive was verified against the
REAL gateway — `GET /v1/api/tickle` at exact 60s spacing — and ExecutionListener's
websocket connect → unauthenticated drop → clean 5s reconnect cycle observed, zero
tracebacks. Key hardening: the 9 pre-existing happy-path tests were constructing +
starting REAL checker/listener objects from MagicMock config (26 real "started" log
lines per suite run, abandoned cross-loop asyncio tasks leaked into module globals —
and the new tests' monkeypatch teardown was RE-INSTALLING the leaked object, since
monkeypatch restores rather than resets). Fixed with an autouse `backend_singletons`
fixture patching both classes suite-wide and resetting (never restoring) the globals on
both sides; A/B proven: 26 real starts before the fixture, 0 after, with a
positive-control grep showing log capture works. Plus: `gdrive_sync` kwarg now pinned
via sentinel. Tests 15 → 18; suite 392 → 395.

Grounded 2026-07-23 against verified signatures: `ConnectivityChecker(gateway_url: str,
gdrive_token_file: Path, tv_bridge=None, gdrive_sync=None)` (`status.py:60`), `.start()`
idempotent — no-ops on a running task, recreates a finished/cancelled one
(`status.py:176-185`), `.subscribe(cb) -> unsubscribe` (`status.py:193`);
`ExecutionListener(gateway_url: str, store: SQLiteStore)` (`execution_listener.py:131`),
`.start()` same idempotent contract (`execution_listener.py:136-142`). Parity source:
`app.py:348-377`. Both `.start()`s create asyncio tasks on the CURRENT running loop —
in Panel that's the process-wide uvicorn loop (empirically confirmed by the D4 probe:
the session factory runs on MainThread with the process-wide loop), so the singleton
tasks survive individual sessions exactly as they do under Chainlit.

**Design notes:**

- Per D6: constructed + started in `_init_session` under `is None` guards; `.start()`
  called unconditionally each session (app.py:360-361 parity — restarts a cancelled
  task). **NO per-session `subscribe`** — chat-alert delivery is Phase 6's remaining
  work (the D4 idiom is now proven, but the Phase 5 goal is backend keepalive only:
  the checker's 60s `/tickle` poll is the IBKR session keepalive, a live-session
  protection gap Panel currently has).
- `tv_bridge=None` (D5 — no TV in Phase 5; Phase 9 wires `set_tv_bridge`).
- Config via `toolkit._config` (same sanctioned reach-in as Task 5.3), gdrive_sync via
  the module global; construction is synchronous with no `await` between the None-check
  and assignment, so no lock is needed on the single-threaded loop (same reasoning as
  app.py:369-371's comment).
- Placement: after `store.create_session(...)`, before the sink/agent block —
  app.py's relative order.

**Files:**

- Modify: `claudia/panel_app.py` (imports, 2 module globals, singleton block)
- Modify: `tests/test_panel_app.py` (3 new tests)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_panel_app.py`:

```python
@pytest.mark.asyncio
async def test_init_starts_connectivity_and_execution_singletons(monkeypatch):
    """Task 5.5 (design D6): first session constructs + starts both process
    singletons. The checker's 60s /tickle poll is the IBKR session KEEPALIVE —
    a live-session-protection requirement, not cosmetics (app.py:348-377
    parity). No per-session subscribe in Phase 5 (chat alerts are Phase 6)."""
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", None)
    monkeypatch.setattr("claudia.panel_app._execution_listener", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
        patch("claudia.panel_app.ConnectivityChecker") as mock_checker_cls,
        patch("claudia.panel_app.ExecutionListener") as mock_listener_cls,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    checker_kwargs = mock_checker_cls.call_args.kwargs
    assert checker_kwargs["gateway_url"] is mock_toolkit._config.gateway_url
    assert checker_kwargs["gdrive_token_file"] is mock_toolkit._config.gdrive_token_file
    assert checker_kwargs["tv_bridge"] is None
    mock_checker_cls.return_value.start.assert_called_once()
    mock_checker_cls.return_value.subscribe.assert_not_called()
    mock_listener_cls.assert_called_once_with(
        mock_toolkit._config.gateway_url, mock_toolkit._store
    )
    mock_listener_cls.return_value.start.assert_called_once()


@pytest.mark.asyncio
async def test_second_session_reuses_singletons_but_restarts_them(monkeypatch):
    """app.py:360-361 parity: construction happens once, but .start() is called
    unconditionally every session (idempotent — restarts a cancelled task)."""
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", None)
    monkeypatch.setattr("claudia.panel_app._execution_listener", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
        patch("claudia.panel_app.ConnectivityChecker") as mock_checker_cls,
        patch("claudia.panel_app.ExecutionListener") as mock_listener_cls,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat1 = _build_chat_app()
        await asyncio.wait_for(chat1.callback("a", "User", chat1), timeout=_CALLBACK_TIMEOUT)
        chat2 = _build_chat_app()
        await asyncio.wait_for(chat2.callback("b", "User", chat2), timeout=_CALLBACK_TIMEOUT)

    mock_checker_cls.assert_called_once()
    mock_listener_cls.assert_called_once()
    assert mock_checker_cls.return_value.start.call_count == 2
    assert mock_listener_cls.return_value.start.call_count == 2


@pytest.mark.asyncio
async def test_singletons_not_started_when_docs_missing(monkeypatch):
    """Setup-required parity with app.py control flow: the missing-docs guard
    returns before app.py's singleton block runs — Panel matches (keepalive only
    for sessions that got past doc validation)."""
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", None)
    monkeypatch.setattr("claudia.panel_app._execution_listener", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app.ConnectivityChecker") as mock_checker_cls,
        patch("claudia.panel_app.ExecutionListener") as mock_listener_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.side_effect = FileNotFoundError(
            "docs/context.md not found"
        )
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_checker_cls.assert_not_called()
    mock_listener_cls.assert_not_called()
```

Run: `pytest tests/test_panel_app.py -k "singleton" -v`
Expected: the first two FAIL on `AttributeError` (patching
`claudia.panel_app.ConnectivityChecker`, which doesn't exist yet); the third fails the
same way at patch time.

- [x] **Step 2: Implement**

`claudia/panel_app.py` — new imports:

```python
from claudia.execution_listener import ExecutionListener
from claudia.status import ConnectivityChecker
```

New module globals (next to `_gdrive_sync`):

```python
_connectivity_checker: ConnectivityChecker | None = None
_execution_listener: ExecutionListener | None = None
```

In `_init_session`, after `store.create_session(...)` and before the sink construction:

```python
            # Backend singletons (design D6, app.py:348-377 parity). The
            # checker's 60s /tickle poll is the IBKR session KEEPALIVE — live-
            # session protection, not cosmetics. Constructed once per process,
            # started unconditionally each session (start() is idempotent and
            # restarts a cancelled task). No per-session subscribe in Phase 5 —
            # chat-alert delivery is Phase 6's work. Construction is synchronous
            # (no await between the None-check and assignment on this single-
            # threaded loop), so no lock is needed — same reasoning as
            # app.py:369-371. tv_bridge stays None until Phase 9 (D5).
            global _connectivity_checker, _execution_listener
            cfg = toolkit._config
            if _connectivity_checker is None:
                _connectivity_checker = ConnectivityChecker(
                    gateway_url=cfg.gateway_url,
                    gdrive_token_file=cfg.gdrive_token_file,
                    tv_bridge=None,
                    gdrive_sync=_gdrive_sync,
                )
            _connectivity_checker.start()
            if _execution_listener is None:
                _execution_listener = ExecutionListener(cfg.gateway_url, toolkit._store)
            _execution_listener.start()
```

(The existing `global _gdrive_sync` line at the top of `_init_session` extends to
`global _gdrive_sync, _connectivity_checker, _execution_listener` — Python allows only
one binding site; keep a single global statement.)

- [x] **Step 3: Run tests to verify pass**

Run: `pytest tests/test_panel_app.py -v` → 18/18 pass.

- [x] **Step 4: Full suite + linters**

Run: `pytest -m "not integration" -q` → 392 + 3 = 395, 0 failures.
`ruff check claudia/panel_app.py tests/test_panel_app.py` + `mypy claudia/panel_app.py`
→ clean.

- [x] **Step 5: Manual smoke (no gateway needed)**

`uvicorn claudia.panel_app:app --port 8001`, load the page, watch the log ~70s: the
ConnectivityChecker's first poll cycle runs (expect its IBKR-offline log lines — gateway
is down — with no tracebacks); ExecutionListener's websocket retry loop logs its
connection failure and backs off without spinning. Kill the server.

- [x] **Step 6: Commit**

```bash
git add claudia/panel_app.py tests/test_panel_app.py
git commit -m "feat: Panel backend singletons — ConnectivityChecker keepalive + ExecutionListener (D6)"
```

## Phase 6: Connectivity status — designed Panel-native, not ported

**This phase was fully redesigned 2026-07-22 per the "no Chainlit-shape mimicry" principle
above — the original outline (port `/api/status` + `_send_alert`'s chat-message push) is
superseded by the design below, not extended.**

**What stays exactly as-is, unchanged:** `ConnectivityChecker` (`claudia/status.py`) —
already 100% framework-agnostic (the poll loop, `check_ibkr`/`check_gdrive`/`check_tradingview`,
the soft-recovery logic, the cached `_status` dict) except for `_send_alert()`'s one
Chainlit call site. Single source of truth for connectivity state, shared across sessions,
zero changes to its polling/caching behavior. `ExecutionListener` needs no changes at all
(never touches the UI directly).

**The Panel-native design, verified live before being written down here (not assumed):**
- Each session builds its own 3 status indicators using `pn.indicators.BooleanStatus` — a
  real, built-in Panel component (confirmed: `value: bool`, `color: Selector` with options
  `['primary', 'secondary', 'success', 'info', 'danger', 'warning', 'light', 'dark']`,
  `label: str`). Maps directly onto the existing `ServiceStatus` enum: `OK` →
  `value=True, color="success"`, `ERROR` → `value=True, color="danger"`, `UNKNOWN` →
  `value=False, color="dark"` (matching the current "gray dot for not-configured, not an
  error" rule already in `_run_checks()`).
- Each session registers `pn.state.add_periodic_callback(_refresh_indicators, period=5000)`
  (5s, matching the current JS-poll interval) inside `_build_chat_app()`. The callback reads
  `_connectivity_checker.get_status()` — already a thread-safe shallow copy per its existing
  implementation — and updates that session's own 3 `BooleanStatus` widgets.
- **Verified live, source-level, that this is correctly session-scoped with automatic
  cleanup — not something to hope works or wrap in manual teardown:**
  `pn.state.add_periodic_callback`'s own source registers the callback against
  `self._periodic[self.curdoc]` (the *calling* session's document, confirmed since this
  runs while `curdoc` is set inside `_eval_panel`'s session-build context); `pn.state`'s
  `_destroy_session` (fires automatically when a session ends) walks that exact same dict
  and calls `cb._cleanup(session_context)` on every periodic callback registered against
  the destroyed document, then deletes the entry. No leaked timers, no manual disconnect
  logic needed — this is Panel's own session lifecycle doing the right thing natively.
- **`/api/status` and the browser-JS-polling pattern are dropped entirely, not ported.**
  That pattern existed only because Chainlit's frontend was an opaque compiled SPA with no
  other way to get live data in — polling an HTTP endpoint from hand-written JS was the
  only option. Panel's own WebSocket already pushes param changes (like a `BooleanStatus`
  widget's `value`/`color`) to the browser the moment they change server-side, as a core
  part of its architecture — no separate HTTP endpoint, no browser-side polling code, no
  custom JS to write or maintain. The `/api/status` route from `app.py:89-94` has no reason
  to exist in the Panel app at all.
- **Chat alerts on state transitions** (`_send_alert`'s current job — a "GDrive
  disconnected" message appearing in the chat feed when a service goes down) stay in
  scope for this phase too, but need a live per-session subscriber list, not a port of the
  Chainlit call site: `ConnectivityChecker` doesn't currently know which sessions exist. Add
  a small subscriber registry (`_subscribers: list[Callable]`, `subscribe()`/`unsubscribe()`
  methods) that `_run_checks()` notifies on a transition, alongside the existing cached-dict
  update it already does — each session's periodic-callback setup subscribes a closure over
  its own `chat` object, and unsubscribes via the same `add_periodic_callback`-style
  automatic session cleanup (register the unsubscribe as that callback's own teardown, or
  via `pn.state.on_session_destroyed` — verify the exact mechanism during implementation,
  don't assume the pattern from `add_periodic_callback` transfers unchanged).

**Files:** Modify `claudia/status.py` (`ConnectivityChecker`: subscriber registry,
`_send_alert` becomes framework-agnostic — notifies subscribers, no more direct `cl`
import), `claudia/panel_app.py` (`_build_chat_app()`: 3 `BooleanStatus` widgets + periodic
callback + alert subscription, wired into the template/layout — exact placement depends on
Phase 7's styling/template work, coordinate rather than guess a layout now). New:
`tests/test_status.py` additions for the subscriber registry (framework-agnostic, testable
without Panel at all) and a new `tests/test_panel_app.py` or similar for the Panel-side
wiring.

### Task 6.1: Subscriber registry in `ConnectivityChecker`

Framework-agnostic — no template/layout dependency, safe to do ahead of Phase 7.

**✅ Completed 2026-07-23.** Landed across two commits: `a329ea4` (implementation +
spec-review doc fixes) and `6683018` (code-quality-review fixes: docstring-accuracy
correction, a self-unsubscribe-mid-notify test proven to fail against a live-list
iteration, and `exc_info=True` on the per-subscriber failure log). Full subagent-driven
cycle applied (implement → independent spec review → code-quality review → fixes, each
independently re-verified, not taken on report). Final: `tests/test_status.py` 37 → 43
tests (5 new registry tests + 1 mid-notify guard test; 2 pre-existing tests de-Chainlited),
full unit suite 350 → 356, `ruff`/`mypy` clean. Two known-and-intentional deferrals recorded
below survive this task unchanged: the Panel-side `BooleanStatus` widget wiring (blocked on
Phase 7's layout decision) and multi-session alert-routing correctness (a pre-existing
property of the shipped poll loop, to be fixed by the later Panel-native per-session tasks).

**Why this piece first:** the `BooleanStatus`-widget wiring depends on Phase 7's template
decision (where in the page layout do 3 status dots + a logo live?) — genuinely blocked on
work not yet done. The subscriber registry itself has no such dependency: it's a pure
`status.py` refactor, fully testable without Panel *or* Chainlit involved, and unblocks
Phase 7 by having the notification mechanism ready before there's a widget to notify.

**Necessary, minimal touch to `claudia/app.py` — flagged explicitly, not silent scope
creep:** `_send_alert` is `ConnectivityChecker`'s *only* remaining Chainlit dependency, and
each entry point (`app.py`, `panel_app.py`) runs its own separate `ConnectivityChecker`
singleton instance (separate processes, not shared) — so removing the hardcoded `import
chainlit as cl` from `_send_alert` means `app.py` must explicitly subscribe its own
`cl.Message`-sending callback to keep its *existing* alert behavior working, since nothing
does that implicitly anymore. This is a preservation of current behavior via one new line,
not a redesign of `app.py`.

**Files:**

- Modify: `claudia/status.py` (`ConnectivityChecker.__init__`, new `subscribe()` method,
  `_send_alert`)
- Modify: `claudia/app.py` (one new line: subscribe a small Chainlit-alert callback after
  constructing `_connectivity_checker`)
- Modify: `tests/test_status.py`

**Grounded, 2026-07-23, against the real `tests/test_status.py`** (read in full before
writing the tests below — not a sketch): the file uses a `checker` pytest fixture
(`ConnectivityChecker(gateway_url=..., gdrive_token_file=tmp_path / "token.json")`, no
custom constructor helper function) and `@pytest.mark.asyncio` throughout. Two *existing*
tests currently patch Chainlit directly and must be updated as part of this task, not left
behind: `test_run_checks_unknown_to_ok_no_alert` (`patch("chainlit.Message")`, asserts
`mock_msg.assert_not_called()`) and `test_stop_cancels_task` (`patch("chainlit.Message.send",
AsyncMock())`, incidental — that test isn't really about alerting, the patch is just
defensive). Both patches becomes unnecessary once `_send_alert` no longer imports
`chainlit` at all — remove them; `test_run_checks_unknown_to_ok_no_alert`'s assertion needs
rewriting to check the subscriber list was never notified instead of `mock_msg`.

- [x] **Step 1: Write the failing tests**

Add to `tests/test_status.py`:

```python
@pytest.mark.asyncio
async def test_subscribe_returns_unsubscribe_callable(checker):
    async def _subscriber(msg: str) -> None:
        pass
    unsubscribe = checker.subscribe(_subscriber)
    assert callable(unsubscribe)
    assert _subscriber in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_notifies_all_subscribers_with_formatted_message(checker):
    received_a, received_b = [], []
    async def _sub_a(msg: str) -> None:
        received_a.append(msg)
    async def _sub_b(msg: str) -> None:
        received_b.append(msg)
    checker.subscribe(_sub_a)
    checker.subscribe(_sub_b)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert received_a == received_b
    assert "disconnected" in received_a[0].lower()


@pytest.mark.asyncio
async def test_send_alert_unknown_to_ok_notifies_no_subscribers(checker):
    """Mirrors the pre-existing test_run_checks_unknown_to_ok_no_alert's intent —
    startup settling into a good state is silent, not an alert-worthy transition."""
    received = []
    async def _subscriber(msg: str) -> None:
        received.append(msg)
    checker.subscribe(_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.OK)

    assert received == []


@pytest.mark.asyncio
async def test_send_alert_unsubscribed_callback_stops_receiving(checker):
    received = []
    async def _subscriber(msg: str) -> None:
        received.append(msg)
    unsubscribe = checker.subscribe(_subscriber)
    unsubscribe()

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert received == []
    assert _subscriber not in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_one_subscriber_exception_does_not_block_others(checker):
    """Mirrors the existing try/except-per-send pattern _send_alert already has for its
    single Chainlit call site today — a failing subscriber must not prevent other
    subscribers (or the status update itself) from proceeding."""
    received = []
    async def _broken_subscriber(msg: str) -> None:
        raise RuntimeError("subscriber blew up")
    async def _good_subscriber(msg: str) -> None:
        received.append(msg)
    checker.subscribe(_broken_subscriber)
    checker.subscribe(_good_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert len(received) == 1
```

Also **modify** the two existing tests identified above (exact current text, verified
2026-07-23 at `tests/test_status.py:188-199` and `:499-514`):

`test_run_checks_unknown_to_ok_no_alert` — before:

```python
@pytest.mark.asyncio
async def test_run_checks_unknown_to_ok_no_alert(checker_with_token):
    """UNKNOWN → OK at startup: _send_alert is called but no Chainlit message sent."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
         patch("chainlit.Message") as mock_msg:
        mock_msg.return_value.send = AsyncMock()
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    assert checker_with_token.get_status()["gdrive"] == ServiceStatus.OK
    # UNKNOWN→OK: no Chainlit message instantiated
    mock_msg.assert_not_called()
```

after:

```python
@pytest.mark.asyncio
async def test_run_checks_unknown_to_ok_no_alert(checker_with_token):
    """UNKNOWN → OK at startup: _send_alert runs but notifies no subscribers."""
    received = []
    async def _subscriber(msg: str) -> None:
        received.append(msg)
    checker_with_token.subscribe(_subscriber)

    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    assert checker_with_token.get_status()["gdrive"] == ServiceStatus.OK
    # UNKNOWN→OK: no alert dispatched to subscribers
    assert received == []
```

`test_stop_cancels_task` — before:

```python
@pytest.mark.asyncio
async def test_stop_cancels_task(checker):
    """stop() cancels the poll loop; start() can restart it."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
         patch("chainlit.Message.send", AsyncMock()):
        checker.start()
        assert checker._task is not None
        assert not checker._task.done()
        checker.stop()
        import asyncio
        await asyncio.sleep(0)   # let cancellation propagate
        assert checker._task.done()
        # restart works after cancellation
        checker.start()
        assert not checker._task.done()
        checker.stop()
```

after (only the `with` block's patch target changes — `chainlit.Message.send` was always
incidental defensive patching here, nothing in this test's own assertions touches alerting):

```python
@pytest.mark.asyncio
async def test_stop_cancels_task(checker):
    """stop() cancels the poll loop; start() can restart it."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
        checker.start()
        assert checker._task is not None
        assert not checker._task.done()
        checker.stop()
        import asyncio
        await asyncio.sleep(0)   # let cancellation propagate
        assert checker._task.done()
        # restart works after cancellation
        checker.start()
        assert not checker._task.done()
        checker.stop()
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_status.py -v -k "subscribe or alert"`
Expected: failures — `subscribe`/`_subscribers` don't exist yet on `ConnectivityChecker`.

- [x] **Step 3: Implement** (see the code block below — already complete, grounded code,
  not a sketch)

- [x] **Step 4: Run to verify pass**

Run: `pytest tests/test_status.py -v`
Expected: all pass — compute the exact total once you've confirmed `tests/test_status.py`'s
real current test count (don't assume a number here; this file wasn't fully inventoried
before this note was written, unlike every other file this plan has touched so far — count
it directly, e.g. `grep -cE "^(async )?def test_" tests/test_status.py`, both before and
after, the same discipline Task 3.2 established after its own test-count miscount).

- [x] **Step 5: Run the full unit suite**

Run: `pytest -m "not integration" -q`
Expected: 350 baseline + (Step 4's real delta), 0 failures.

- [x] **Step 6: Commit**

```bash
git add claudia/status.py claudia/app.py tests/test_status.py
git commit -m "refactor: subscriber registry in ConnectivityChecker, remove hardcoded chainlit dependency"
```

**Implementation sketch for `claudia/status.py`** (real code, unlike the tests above — this
part doesn't depend on reading anything else first):

```python
    def __init__(self, ...) -> None:  # existing params unchanged
        ...  # existing body unchanged
        self._subscribers: list[Callable[[str], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[str], Awaitable[None]]) -> Callable[[], None]:
        """Register a callback to receive future alert text (e.g. the same strings
        _DISCONNECT_MESSAGES/_RECONNECT_MESSAGES already produce today). Returns an
        unsubscribe function."""
        self._subscribers.append(callback)
        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._subscribers.remove(callback)
        return _unsubscribe

    async def _send_alert(self, service: str, prev: ServiceStatus, new: ServiceStatus) -> None:
        if new == ServiceStatus.ERROR:
            msg = _DISCONNECT_MESSAGES.get(service, f"⚠️ {service} disconnected.")
        elif new == ServiceStatus.OK and prev == ServiceStatus.ERROR:
            msg = _RECONNECT_MESSAGES.get(service, f"✅ {service} reconnected.")
        else:
            return  # UNKNOWN → OK at startup: silent
        for subscriber in list(self._subscribers):  # copy: a subscriber unsubscribing
                                                       # itself mid-notify must not skip
                                                       # or corrupt the remaining iteration
            try:
                await subscriber(msg)
            except Exception as exc:
                log.warning("Could not push connectivity alert to a subscriber: %s", exc)
```

Needs `from collections.abc import Awaitable, Callable` and `from contextlib import
suppress` added to imports if not already present — check the real file. **Verified
2026-07-23:** `contextlib.suppress` is already this project's established idiom for
"ignore one specific exception type," used the same way in `context_loader.py:124`,
`execution_listener.py:150,201,270`, and `conversation_store.py:126` — match it rather than
a bare `try/except ValueError: pass`.

**Also fix two now-stale docstrings while in this file** (verified stale against the real
current file, not hypothetical — leaving Chainlit-specific claims in place after removing
the actual Chainlit dependency would be exactly the kind of doc/code accuracy gap this
project's own recent history (`git log`: "docs: fix accuracy gaps in CLAUDE.md and
SECURITY.md") treats as worth fixing on sight, not deferring):

1. Module docstring (`status.py:1-7`), the line `Pushes cl.Message alerts to chat on state
   transitions.` → `Notifies registered subscribers on state transitions; alert delivery
   (e.g. Chainlit chat messages) is wired externally via subscribe().`
2. `ConnectivityChecker`'s class docstring (`status.py:49-56`): the line `Emits Chainlit
   chat alerts on state transitions (UNKNOWN/OK → ERROR, ERROR → OK).` → `Notifies
   registered subscribers on state transitions (UNKNOWN/OK → ERROR, ERROR → OK) — see
   subscribe().` Also **remove** the trailing `Source: https://docs.chainlit.io/api-reference/lifecycle-hooks/on-chat-start`
   line from this docstring — it was citing Chainlit's `on_chat_start` hook (CLAUDE.md's
   "API Docs First" convention requires a citation for external-API behavior claims made
   in the code; this class makes none anymore once decoupled). Do **not** invent a
   replacement citation here since `ConnectivityChecker` itself now makes no Chainlit
   claims to cite. If a citation still belongs anywhere, it belongs on
   `_chainlit_connectivity_alert` in `app.py` (the one remaining piece of code that calls
   `cl.Message(...).send()`) — optional, the existing `app.py` module docstring already
   lists `cl.Message` under its "Chainlit API references used in this file" table
   (`app.py:11`), so a second inline citation there is not required to satisfy the
   convention, just permitted if it reads better to the implementer.

**`claudia/app.py` — the one required line.** `import chainlit as cl` is already at module
level (`app.py:33`), so no new import is needed for the alert function itself.

**Verified 2026-07-23 — placement is inside the singleton guard, not a sibling of
`.start()`:** `on_chat_start` (`app.py:215`) runs on *every* new chat session, but the
`ConnectivityChecker` singleton is only constructed once, guarded by
`if _connectivity_checker is None:` (`app.py:341-349`). `.start()` at `app.py:351` is
deliberately called unconditionally every session (it's idempotent — a no-op once the poll
task is already running). `.subscribe()` is **not** idempotent the same way — calling it
every session would register a new subscriber closure per session, so by the Nth session
each alert would fire N times. It must go as the **last line inside the `if` block**
(`app.py:341-349`), immediately after the `ConnectivityChecker(...)` constructor call, so it
runs exactly once — the same one-time cardinality as the singleton construction itself, not
`.start()`'s every-session cardinality:

```python
    global _connectivity_checker
    if _connectivity_checker is None:
        cfg = _config or Config.from_env()
        _config = cfg  # cache for future sessions; may already be set by GDriveSync block above
        _connectivity_checker = ConnectivityChecker(
            gateway_url=cfg.gateway_url,
            gdrive_token_file=cfg.gdrive_token_file,
            tv_bridge=_tv_bridge,
            gdrive_sync=_gdrive_sync,
        )
        _connectivity_checker.subscribe(_chainlit_connectivity_alert)
    # Call unconditionally — start() is idempotent and restarts a cancelled task
    _connectivity_checker.start()
```

Only the added `_connectivity_checker.subscribe(_chainlit_connectivity_alert)` line is new;
everything else shown above is unchanged existing code, included only to make the exact
insertion point unambiguous.

New module-level function, placed alongside `on_chat_start` (either just above it or just
below — implementer's call, no existing convention in this file pins helper-function
placement relative to the hook they support):

```python
async def _chainlit_connectivity_alert(msg: str) -> None:
    await cl.Message(content=msg, author="System").send()
```

This is the *only* Chainlit-specific code left touching connectivity alerts — exactly
mirroring how `ChainlitMessageSink` is the only place `claudia/agent.py` used to import
`chainlit` directly, before Phase 1's decoupling. Same pattern, same reasoning, applied to
a second subsystem.

**Note on pre-existing multi-session behavior (out of scope for this task):** the poll
loop's `asyncio.Task` (created inside `.start()`) inherits whatever Chainlit session context
was active on the first session that ever called `.start()`, since `asyncio.create_task`
snapshots the current `contextvars.Context`. This means `cl.Message(...).send()` inside
`_chainlit_connectivity_alert` targets that first session specifically, not "whichever
session is currently active" — a pre-existing property of the current shipped code
(`_send_alert` already calls `cl.Message(...).send()` from the same poll-loop task today),
unchanged by this task. Multi-session-correct alert delivery is exactly what Phase 6's
later Panel-native tasks (per-session `BooleanStatus` widgets driven by the same subscriber
registry this task adds) are designed to fix — do not attempt to fix it here.

## Phase 7: Styling — outline

**Goal:** Apply the two confirmed-working mechanisms from **[shadow-dom-test]** — inline
`style="..."` for P&L red/green coloring and per-element table styling, and Panel's
`stylesheets=[...]` component parameter with `:host`/`:host *` selectors for fonts/broader
theme — plus a status bar (connectivity dots + logo) and avatar. Do **not** port
`claudia/assets/custom.css`'s page-level-stylesheet approach forward as-is — confirmed not
to reach chat message content.

**Files:** New `claudia/panel_theme.py` or similar (scope during detailing — inventory
`claudia/assets/custom.css`/`custom.js` in full first, since this plan's grounding pass only
summarized them at the file level, not line-by-line, per the earlier scoping note).

## Phase 8: File upload — outline

**Goal:** Replace Chainlit's `spontaneous_file_upload` (used today for TradingView
screenshot attachments, `app.py:624-643`) with Panel's `FileInput`
(click + native drag-and-drop) **[verified-live: confirmed present in `pn.widgets`]** or
`FileDropper` for larger files **[research]**. Must produce the same base64 vision content
block shape `agent.py`'s `handle_message(images=...)` parameter already expects — no change
needed on the `agent.py` side, only how images arrive at the callback.

## Phase 9: TradingView action buttons + sidecar tool merge — outline

**Goal:** Port `tradingview.py`'s two `@cl.action_callback`s (`copy_pinescript`,
`inject_pinescript`, `tradingview.py:401-440`) and `render_pinescript()`
(`tradingview.py:377-398`) to Panel's button pattern (same mechanism as Phase 3). Also port
the `launch_tradingview` action button from `app.py:873-940`. `TradingViewBridge` itself
(sidecar process/MCP client, `tradingview.py:214-372`) needs zero changes — it has no
Chainlit dependency.

**One thing to fix, not just port:** `tradingview.py:428` does
`from claudia.app import _tv_bridge` inside `on_inject_pinescript` — a reach into the
Chainlit entry point's module-level singleton. The Panel port needs its own equivalent
singleton reference (likely `claudia.panel_app._tv_bridge`, mirroring the pattern, not
importing across the two entry-point modules).

## Phase 10: Dashboard + candlestick charting — outline

**Goal:** New capability, not a port — a dashboard pane next to the chat pane using
Panel's template system (`FastListTemplate`/`MaterialTemplate` or `panel.ui`/
`panel-material-ui`, per **[kickoff]**'s instruction to target the modern namespace) and
`df.hvplot.ohlc()` **[research]** for candlestick charts (Bokeh backend for any
live-updating pane, Matplotlib for any static chart embedded in a chat message — same call,
different `kind`/backend argument, per **[research]**). Requires `hvplot` as a new
dependency (not yet added — add during detailing, verify signature live the same way
Phase 1/2's Panel APIs were verified here, not from the research doc's citation alone).

## Phase 11: Cutover — outline

**Goal:** Side-by-side parity checklist against every requirement in the research doc's
"Requirements bar" (8 items) and every Hard Rule in `CLAUDE.md`, then: switch
`start-claudia.sh`/`CLAUDE.md`'s Dev Setup to `claudia/panel_app.py` as the default entry
point, remove `claudia/app.py`, `claudia/order_flow.py`'s Chainlit renderers (keep the
pure helper functions if `panel_order_flow.py` didn't already absorb them), the `chainlit`
dependency from `pyproject.toml`, and `claudia/assets/custom.css`/`custom.js`. Update
`CLAUDE.md`'s architecture diagram and all docs referencing Chainlit-specific concepts
(`cl.Action`, `cl.Message`, `cl.user_session`, etc.). This phase is where
`claudia/panel_app.py` would be renamed to `claudia/app.py` if desired — decide during
detailing whether that rename is worth the churn or whether keeping the name is clearer.

---

## Living-document protocol

This plan gets edited, not replaced, as implementation proceeds:

- **Before starting an outline phase (3-11):** add a "Detailed steps" subsection directly
  under that phase's outline in this same file, following the same bite-sized/TDD format as
  Phases 1-2, grounded in whatever Phases 1-2 (and any prior outline phases already built)
  actually revealed — not written speculatively before that point.
- **When a task reveals something the research/kickoff docs got wrong or didn't cover**
  (e.g., a Panel API that doesn't behave as documented, a current-codebase detail this
  audit missed): correct it in place in the relevant section above, and add a one-line note
  in that section naming the date and what changed, so later readers can tell a
  freshly-verified claim from a stale one.
- **Do not let an outline phase's implementer improvise from memory of the research docs.**
  If a phase's outline above doesn't yet have detailed steps, that's this plan's signal to
  stop and detail it — the same discipline this planning pass itself used before writing
  Phase 1-2's code (install the real dependency, inspect the real API, then write the step).

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-07-22-panel-migration.md`. Given the
existing session is already following **superpowers:subagent-driven-development**, Phase 1's
tasks are ready to dispatch to a fresh implementer subagent now, one task at a time, each
followed by spec-compliance review then code-quality review per that skill.
