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
