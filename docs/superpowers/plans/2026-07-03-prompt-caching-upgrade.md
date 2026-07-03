# Prompt Caching Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Anthropic prompt caching (`cache_control: ephemeral`) to ClaudIA's full stable prefix — tools, system prompt, **and conversation history** — so every tool-loop turn and every message within the 5-minute TTL reads the prefix at 0.1× cost instead of reprocessing it at full price.

**Architecture:** Three cache breakpoints (of the 4-per-request maximum): one on the last tool dict in `_all_tools`, one on the system prompt converted to block form, and one on the final content block of the messages array (refreshed per API call). The first two implement the note `docs/prompt-caching-upgrade.md` (commit 57978c6); the third closes the history gap found during plan verification — without it, the growing conversation is re-processed at full input price on every tool-loop turn. The system prompt itself is **built once per session** — doc version and documents are resolved when ClaudIA loads, never per prompt (user decision 2026-07-03); hot-reload stays event-driven via a watchdog reload counter. Plus permanent INFO-level logging of the cache usage fields from the `message_start` stream event.

**Tech Stack:** Python 3.11, `anthropic` SDK (`AsyncAnthropic.messages.stream`), pytest.

---

## Spec verification (done during planning — 2026-07-03)

The implementation note was verified against the live official docs
(https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md, WebFetch 2026-07-03)
and against the current code. **All facts in the note are confirmed correct:**

| Note claim | Verified |
|---|---|
| Syntax `"cache_control": {"type": "ephemeral"}` on content blocks; last-block marker caches the whole prefix | ✅ |
| Minimum cacheable length for `claude-opus-4-8` is **1,024 tokens**; below-minimum fails silently | ✅ (doc quote: "1,024 tokens for Claude Opus 4.8, Claude Sonnet 5, …") |
| Pricing: writes 1.25× (5m TTL), reads 0.1× ($0.50/MTok on opus-4-8) | ✅ |
| Streaming compatible; usage arrives in `message_start` | ✅ |
| Tool-definition changes invalidate the whole cache; `set_tv_bridge` mid-session swap invalidates — expected | ✅ (agent.py:289) |
| No `cache_control` anywhere in the codebase today | ✅ (`grep -r cache_control claudia/` is empty) |

**Silent-invalidator audit of the current prefix (all clean):**
- `toolkit.tools` returns the module-level constant `TOOL_DEFINITIONS` (claude_tools.py:886) — deterministic order. ✅
- `_LOCAL_TOOLS` is a module-level constant, always last. ✅
- `load_system_prompt()` (context_loader.py:85) concatenates file/Drive content — no timestamps, UUIDs, or per-request interpolation. Changes only on hot-reload. ✅
- `_build_system_prompt` inputs (`doc_version`, `trade_context`) are fixed per session. ✅

**Context & principles integration (reviewed 2026-07-03; revised same day per user decision):**

**Design principle (user decision 2026-07-03):** doc-version and document checks happen **when ClaudIA loads**, never per prompt. The documents can be modified, but only periodically and never without the operator knowing — the agent must not pay recurring per-prompt overhead re-verifying them. The pre-existing code violated this: `handle_message` rebuilt the system prompt on **every message** (two file reads via `load_system_prompt` + one `doc_versions` DB query via `_build_version_note`, producing an identical string each time). **Task 3** removes this: the system prompt is built once per session and rebuilt only on an event-driven watchdog signal; per-prompt cost drops to one integer comparison.

| Pipeline stage | When resolved (after Task 3) | Verified |
|---|---|---|
| Drive override (`context.md`/`principles.md` fetched at session start) | Session start — `_context_override`/`_principles_override` set once in `on_chat_start` | ✅ context_loader.py:75 |
| Local-file path (no override, or after hot-reload) | Read once at the session-start build; re-read **only** when the watchdog reports an edit (event-driven, not per prompt); `.strip()` applied identically on both paths (matches hash stability) | ✅ context_loader.py:75-76 |
| Version note (`_build_version_note`) | Computed once at the session-start build — `doc_version` is fixed per session and registration happens only in `on_chat_start`. (Concurrent-session check: registry is append-only ordered `created_at ASC`, so another session registering v(n+1) cannot change this session's "previous:" line even on rebuild) | ✅ agent.py:205, conversation_store.py:178 |
| Market-calendar block (`trade_context`) | Fixed per session (built in `on_chat_start`); calendar cache is date-keyed, so two sessions on the same day produce byte-identical blocks — a tab reload within the 5-min TTL re-reads the cache | ✅ |
| `_SAFETY_BLOCK` | Hardcoded module constant, appended last | ✅ agent.py:46 |
| Past principles versions | Retrieved via the `get_doc_version` **tool** (just-in-time, lands after the cached prefix as a tool_result) — never injected into the system prompt. This is the docs' recommended shape for dynamic data. | ✅ agent.py:463 |

**Considered and rejected (docs-offered alternatives — decision recorded so they aren't re-litigated):**
1. **Mid-conversation `role: "system"` messages on hot-reload.** Official docs (S1): "On Claude Opus 4.8, you can add a new system instruction partway through a conversation without invalidating the system or message caches. Append a `{"role": "system"}` message to `messages` instead of editing the top-level `system` field." Rejected for principles hot-reload: a principles edit is a rules **replacement**, not an additive instruction — appending a delta would leave the *stale* principles authoritative in the cached prefix alongside the new text, and the doc-versioning system (hash, `doc_versions`, v1→v2 WARNING) keys off the top-level content. Full system+messages invalidation on reload is the semantically correct hard boundary, and reloads are rare. The mechanism remains the right tool for future *additive* mid-session operator instructions (e.g., mode toggles) — just not for principles replacement.
2. **Splitting `system` into multiple blocks with separate breakpoints** (context block + principles block) so a principles-only edit reuses the tools+context prefix. Rejected: pays off only on rare hot-reload events, consumes the 4th and last breakpoint, and adds structure for no steady-state gain. Single block (Task 2) is the fewest-breakpoints shape that matches the actual stability boundary. YAGNI.

Hot-reload and doc-version changes therefore remain the *only* intentional system-cache invalidators (tools cache survives both, per the invalidation hierarchy) — and both are visible in the Task 4 `prompt cache:` log line as a one-time `created>0` bump.

**Three gaps found during verification — ALL addressed by this plan:**
1. **Mutation hazard** — `_all_tools` concatenates lists of *shared* dicts. Setting `cache_control` by in-place mutation would permanently alter the module-level `_LOCAL_TOOLS[-1]` / toolkit constants. → **Task 1** uses a copy-safe helper with a regression test.
2. **Hot-reload invalidation** — editing `context.md`/`principles.md` mid-session changes the system block and invalidates the system+messages cache. Expected and harmless, same category as the TV-bridge case. → **Task 6** documents it in the note.
3. **Conversation history uncached** — the growing `messages` array (up to 40 history rows + all tool_use/tool_result turns) is re-processed at full input price every turn. → **Task 5** adds the third breakpoint on the final message content block — the officially documented multi-turn pattern ("the cache breakpoint automatically moves to the last cacheable block in each request"; lookback finds the prior entry, no need to remove old marker positions). Four documented caveats:
   - **20-block lookback window:** each breakpoint walks back at most 20 content blocks to find the previous cache entry. A single turn with **10+ parallel tool calls** (1 text + N tool_use + N tool_result > 20 blocks) silently misses the prior entry — that turn re-writes the cache instead of reading it, and the `_log_cache_usage` line from Task 4 makes this visible. Accepted: ClaudIA's tool turns are typically 1–3 calls.
   - **Breakpoint budget:** 3 of the maximum 4 used (tools, system, messages).
   - **History-window slide:** once a session exceeds `_HISTORY_LIMIT=40` rows, each new user turn drops the oldest row from the front of the rebuilt history, shifting every messages byte — from then on the messages cache misses once per user turn (tools+system unaffected; within-turn tool loops still cache fully). Bounded cost: ≤40 rows of text re-read once per turn. Optional follow-up if the usage logs show it matters: evict with hysteresis (drop rows in blocks of ~10 so the front stays stable for several turns) — not in this plan's scope.
   - **Cross-turn shape divergence:** during a turn the live array holds block-form assistant text plus tool_use/tool_result messages; the history rebuilt at the *next* user turn is plain strings with tool rows skipped (`_history_to_messages`). So a new user turn resumes from the cache entry written at the end of the reconstructed history (the first request of the previous turn), not from mid-tool-loop entries. Expected; live verification step 3 in Task 6 covers it.

Per-claim source evidence for everything above: `docs/2026-07-03-llm-best-practices-sources.md`.

---

### Task 1: Cache breakpoint on the tools array

**Files:**
- Modify: `claudia/agent.py` (add `_with_cache_marker` helper near `_LOCAL_TOOL_NAMES`, ~line 202; change `_all_tools` property, line 295-297)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py` (imports go at the top of the file with the existing ones):

```python
from claudia.agent import _with_cache_marker, _LOCAL_TOOLS


def test_with_cache_marker_marks_only_last_tool():
    tools = [
        {"name": "a", "input_schema": {"type": "object"}},
        {"name": "b", "input_schema": {"type": "object"}},
    ]
    marked = _with_cache_marker(tools)
    assert marked[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked[0]
    assert marked[-1]["name"] == "b"


def test_with_cache_marker_does_not_mutate_input():
    original_last = {"name": "b", "input_schema": {"type": "object"}}
    tools = [{"name": "a", "input_schema": {"type": "object"}}, original_last]
    _with_cache_marker(tools)
    # The shared dict must be untouched — _LOCAL_TOOLS / toolkit constants are module-level
    assert "cache_control" not in original_last
    assert "cache_control" not in tools[-1]


def test_with_cache_marker_empty_list():
    assert _with_cache_marker([]) == []


def test_local_tools_constant_never_carries_cache_control():
    # Regression guard: repeated calls must not leak the marker into the module constant
    _with_cache_marker(list(_LOCAL_TOOLS))
    _with_cache_marker(list(_LOCAL_TOOLS))
    assert all("cache_control" not in t for t in _LOCAL_TOOLS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k "cache_marker or local_tools_constant" -v`
Expected: FAIL with `ImportError: cannot import name '_with_cache_marker'`

- [ ] **Step 3: Implement the helper and wire it into `_all_tools`**

In `claudia/agent.py`, after the `_LOCAL_TOOL_NAMES` definition (line 202), add:

```python
def _with_cache_marker(tools: list[dict]) -> list[dict]:
    """Return tools with a prompt-cache breakpoint on the last entry.

    The last dict is copied, never mutated — the inputs are shared module-level
    constants (_LOCAL_TOOLS, ibkr_core_mcp TOOL_DEFINITIONS).
    Marking the last tool caches the entire tools array (prefix hierarchy:
    tools -> system -> messages).
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    if not tools:
        return tools
    marked = list(tools)
    marked[-1] = {**marked[-1], "cache_control": {"type": "ephemeral"}}
    return marked
```

Change the `_all_tools` property (line 295-297) to:

```python
    @property
    def _all_tools(self) -> list[dict]:
        return _with_cache_marker(self._toolkit.tools + self._extra_tools + _LOCAL_TOOLS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: all PASS (new tests plus the existing agent tests untouched)

- [ ] **Step 5: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "feat: prompt-cache breakpoint on tools array (copy-safe, no shared-dict mutation)"
```

---

### Task 2: System prompt in block form with cache breakpoint

**Files:**
- Modify: `claudia/agent.py` (add `_system_blocks` helper next to `_build_system_prompt`, ~line 232; change `system=` at the stream call, line 338)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
from claudia.agent import _system_blocks


def test_system_blocks_shape():
    blocks = _system_blocks("You are ClaudIA.")
    assert blocks == [
        {
            "type": "text",
            "text": "You are ClaudIA.",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_system_blocks_preserves_full_prompt():
    prompt = _build_system_prompt("# Role\nTrader assistant.\n\n# Principles\nRisk first.")
    blocks = _system_blocks(prompt)
    assert len(blocks) == 1
    assert blocks[0]["text"] == prompt  # byte-identical — any change invalidates the cache
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k system_blocks -v`
Expected: FAIL with `ImportError: cannot import name '_system_blocks'`

- [ ] **Step 3: Implement the helper and use it in the stream call**

In `claudia/agent.py`, after `_build_system_prompt` (line 231), add:

```python
def _system_blocks(system_prompt: str) -> list[dict]:
    """Wrap the system prompt in block form with a prompt-cache breakpoint.

    The marker on the last (only) system block caches tools + system together.
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
```

In `handle_message`, change the stream call (line 338) from `system=system,` to:

```python
                system=_system_blocks(system),
```

(`system` is built once before the `while True:` loop, so every tool-loop turn sends
byte-identical blocks — required for cache hits. Task 3 then hoists this build from
per-message to once per session.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "feat: system prompt to block form with prompt-cache breakpoint"
```

---

### Task 3: Build the system prompt once per session — version check at load, not per prompt

**Why (user decision 2026-07-03):** context/principles change only periodically; the agent
must not re-verify them on every prompt. Today `handle_message` (agent.py:321) rebuilds the
system prompt per message — two file reads + one `doc_versions` DB query producing an
identical string. This task hoists the build to session start. Hot-reload semantics are
unchanged ("applies from your next message"): the watchdog increments a reload counter when
a document is edited, and the next message rebuilds — event-driven, so the steady-state
per-prompt cost is a single integer comparison.

Threading note: the watchdog thread writes the counter, the event loop reads it — an int
under the GIL, no lock needed. A message racing an edit uses the previous prompt for that
one turn, exactly as today.

**Files:**
- Modify: `claudia/context_loader.py` (reload counter in `__init__` ~line 71 and `_handle_change`, line 125)
- Modify: `claudia/agent.py` (`__init__` cache fields; new `_get_system_blocks` method; `handle_message` uses it)
- Test: `tests/test_context_loader.py`, `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context_loader.py`:

```python
def test_reload_count_increments_on_change(tmp_path):
    (tmp_path / "context.md").write_text("# Role\nX")
    (tmp_path / "principles.md").write_text("# P\nY")
    loader = ContextLoader(tmp_path)
    assert loader.reload_count == 0
    loader._handle_change("context.md")
    assert loader.reload_count == 1
    loader._handle_change("principles.md")
    assert loader.reload_count == 2
```

Append to `tests/test_agent.py`:

```python
from claudia.agent import ClaudIAAgent


class _StubLoader:
    def __init__(self):
        self.reload_count = 0
        self.calls = 0

    def load_system_prompt(self):
        self.calls += 1
        return "# Role\nStub.\n\n# Principles\nStub."


class _StubToolkit:
    tools: list = []


def _make_agent(monkeypatch, loader):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # AsyncAnthropic() constructs, no network
    return ClaudIAAgent(
        toolkit=_StubToolkit(), store=None, context_loader=loader, session_id="s"
    )


def test_system_prompt_built_once_per_session(monkeypatch):
    loader = _StubLoader()
    agent = _make_agent(monkeypatch, loader)
    b1 = agent._get_system_blocks()
    b2 = agent._get_system_blocks()
    assert b1 is b2           # same cached object — no rebuild between messages
    assert loader.calls == 1  # documents read exactly once per session
    assert b1[0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_rebuilt_after_reload(monkeypatch):
    loader = _StubLoader()
    agent = _make_agent(monkeypatch, loader)
    agent._get_system_blocks()
    loader.reload_count += 1  # watchdog fired: a document was edited
    agent._get_system_blocks()
    assert loader.calls == 2  # rebuilt exactly once more
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_context_loader.py::test_reload_count_increments_on_change tests/test_agent.py -k "built_once or rebuilt_after" -v`
Expected: FAIL — `AttributeError: 'ContextLoader' object has no attribute 'reload_count'` and `AttributeError: ... '_get_system_blocks'`

- [ ] **Step 3: Implement**

In `claudia/context_loader.py` `__init__` (after line 71), add:

```python
        # Incremented on every document-change event. Agents cache the built
        # system prompt keyed on this counter, so version/document checks run
        # at load time and on edits only — never per prompt.
        self.reload_count: int = 0
```

In `_handle_change` (line 125), increment after clearing the overrides:

```python
    def _handle_change(self, changed_file: str) -> None:
        # Both overrides are cleared atomically regardless of which file changed —
        # local files become the sole source of truth after any edit.
        self._context_override = None
        self._principles_override = None
        self.reload_count += 1
        if self._reload_callback:
```

In `claudia/agent.py` `__init__` (after `self._client = AsyncAnthropic()`), add:

```python
        self._system_blocks_cache: list[dict] | None = None
        self._system_reload_seen: int = -1
```

Add the method after `set_tv_bridge`:

```python
    def _get_system_blocks(self) -> list[dict]:
        """Return the cached system-prompt blocks, built at most once per session.

        Version note, documents, and market calendar are resolved when ClaudIA
        loads — not on each prompt. The only rebuild trigger is the loader's
        reload_count (event-driven hot-reload); steady-state per-message cost is
        one int comparison. Byte-identical blocks across calls also guarantee
        prompt-cache stability for the system segment.
        """
        count = self._loader.reload_count
        if self._system_blocks_cache is None or count != self._system_reload_seen:
            prompt = _build_system_prompt(
                self._loader.load_system_prompt(), self._doc_version, self._store,
                self._trade_context,
            )
            self._system_blocks_cache = _system_blocks(prompt)
            self._system_reload_seen = count
        return self._system_blocks_cache
```

In `handle_message`, replace the per-message build (agent.py:321-324):

```python
        system = _build_system_prompt(
            self._loader.load_system_prompt(), self._doc_version, self._store,
            self._trade_context,
        )
```

with:

```python
        system_blocks = self._get_system_blocks()
```

and change the stream call (Task 2 set it to `system=_system_blocks(system),`) to:

```python
                system=system_blocks,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_context_loader.py tests/test_agent.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudia/context_loader.py claudia/agent.py tests/test_context_loader.py tests/test_agent.py
git commit -m "perf: build system prompt once per session — doc/version checks at load, event-driven hot-reload"
```

---

### Task 4: Always-on cache usage logging (`message_start`)

The note's verification section says the two usage fields are "worth logging permanently —
they are the cheap, always-on signal that caching stays healthy as tools evolve."
The current event loop (agent.py:342-362) does not handle `message_start` at all.

**Files:**
- Modify: `claudia/agent.py` (add `_log_cache_usage` helper near `_system_blocks`; add a `message_start` branch in the event loop, ~line 344)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
import logging
from types import SimpleNamespace

from claudia.agent import _log_cache_usage


def test_log_cache_usage_reports_all_three_fields(caplog):
    usage = SimpleNamespace(
        cache_creation_input_tokens=12000,
        cache_read_input_tokens=0,
        input_tokens=450,
    )
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert "created=12000" in caplog.text
    assert "read=0" in caplog.text
    assert "uncached=450" in caplog.text


def test_log_cache_usage_warns_when_cache_inactive(caplog):
    # Both cache fields zero = caching silently failed (note: "Verification — do not skip")
    usage = SimpleNamespace(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_tokens=30000,
    )
    with caplog.at_level(logging.WARNING, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_log_cache_usage_handles_missing_fields(caplog):
    # SDK may omit the fields on models/paths without caching — must not raise
    usage = SimpleNamespace(input_tokens=100)
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert "created=0" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k log_cache_usage -v`
Expected: FAIL with `ImportError: cannot import name '_log_cache_usage'`

- [ ] **Step 3: Implement the helper and hook the event**

In `claudia/agent.py`, after `_system_blocks`, add:

```python
def _log_cache_usage(usage) -> None:
    """Log prompt-cache health from a message_start usage object.

    created > 0  -> prefix written this call (1.25x input price)
    read > 0     -> prefix served from cache (0.1x input price)
    both zero    -> caching silently failed (below-minimum prefix, misplaced
                    marker, or a >20-block turn outside the lookback window)
                    -- warn so it is caught as tools evolve.
    """
    created = getattr(usage, "cache_creation_input_tokens", None) or 0
    read = getattr(usage, "cache_read_input_tokens", None) or 0
    uncached = getattr(usage, "input_tokens", None) or 0
    log.info("prompt cache: created=%d read=%d uncached=%d", created, read, uncached)
    if created == 0 and read == 0:
        log.warning(
            "prompt cache inactive (created=0, read=0) — check cache_control placement"
        )
```

In the event loop inside `handle_message` (after `etype = event.type`, line 343), add a
first branch:

```python
                    if etype == "message_start":
                        _log_cache_usage(event.message.usage)

                    elif etype == "content_block_start":
```

(The existing `if etype == "content_block_start":` becomes `elif`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "feat: log prompt-cache usage from message_start; warn when cache inactive"
```

---

### Task 5: Cache breakpoint on conversation history (gap 3)

Without this, every tool-loop turn re-processes the entire messages array — history rows,
tool_use blocks, and tool results — at full input price, even with tools+system cached.
The marker goes on the **final content block of the final message**, rebuilt per API call
on a **copy** (the source `messages` list is never mutated, so stale markers never
accumulate and the breakpoint count stays at exactly 3).

Incremental behavior across the tool loop: call N marks block X; call N+1 marks a later
block Y — the entry written at X is found within the 20-block lookback and read at 0.1×,
and only the blocks added since the previous call are written at the 1.25× cache-write
rate (write pricing applies to the newly written tokens only, per the official docs).

**Files:**
- Modify: `claudia/agent.py` (add `_with_history_cache_marker` helper after `_history_to_messages`, ~line 250; change `messages=` at the stream call, line 339)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
from claudia.agent import _with_history_cache_marker


def test_history_marker_string_content_becomes_marked_block():
    messages = [{"role": "user", "content": "hello"}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]
    # Source untouched — markers must not accumulate across tool-loop iterations
    assert messages[-1]["content"] == "hello"


def test_history_marker_block_content_marks_last_block_only():
    tool_results = [
        {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
    ]
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [{"type": "text", "text": "calling tools"}]},
        {"role": "user", "content": tool_results},
    ]
    marked = _with_history_cache_marker(messages)
    blocks = marked[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier messages and the source blocks are untouched
    assert "cache_control" not in messages[-1]["content"][-1]
    assert marked[0]["content"] == "question"


def test_history_marker_empty_messages():
    assert _with_history_cache_marker([]) == []


def test_history_marker_empty_string_content_left_alone():
    # An empty text block would be rejected by the API — skip marking instead
    messages = [{"role": "user", "content": ""}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k history_marker -v`
Expected: FAIL with `ImportError: cannot import name '_with_history_cache_marker'`

- [ ] **Step 3: Implement the helper and use it at the stream call**

In `claudia/agent.py`, after `_history_to_messages` (line 249), add:

```python
def _with_history_cache_marker(messages: list) -> list:
    """Return a copy of messages with a prompt-cache breakpoint on the final content block.

    Third breakpoint (after tools and system): caches the conversation prefix so
    each tool-loop call reads the prior prefix at 0.1x and writes only the newly
    added blocks at 1.25x. Copies the last message and its block list — the
    caller's list is the loop's working state and must never carry markers
    between iterations.

    Caveat (documented in docs/prompt-caching-upgrade.md): a single turn adding
    more than 20 content blocks (10+ parallel tool calls) falls outside the
    20-block lookback window and re-writes instead of reading — visible as
    created>0/read=0 in the _log_cache_usage line.
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last["content"]
    if isinstance(content, str):
        if not content:
            return messages  # "empty text blocks cannot be cached" (official docs)
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = list(content)
        if not blocks:
            return messages
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    return messages[:-1] + [last]
```

In `handle_message`, change the stream call (line 339) from `messages=messages,` to:

```python
                messages=_with_history_cache_marker(messages),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full unit suite**

Run: `pytest -m "not integration"`
Expected: all PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "feat: prompt-cache breakpoint on conversation history (3rd breakpoint)"
```

---

### Task 6: Live verification and documentation

The note marks verification **"do not skip"** — below-minimum or misplaced markers fail
silently.

**Files:**
- Modify: `docs/prompt-caching-upgrade.md` (status + findings)
- Modify: `docs/live-test-log.md` (run entry, per project convention)

- [ ] **Step 1: Live verification**

Start ClaudIA (`./start-claudia.sh` or `chainlit run claudia/app.py`) and read the
`prompt cache:` log lines:

1. Send message 1 (e.g. "hello"): expect `created > 0` (prefix: 42+ tool schemas +
   system prompt + first message — well above the 1,024-token minimum), `read = 0`.
2. Send message 2 within 5 minutes: expect `read > 0` roughly equal to message 1's
   `created`; `uncached` drops to only the new turn.
3. Send a message that triggers a tool call (e.g. "check IBKR connection status"):
   the tool-loop's second API call should log `read > 0` covering tools + system +
   the pre-tool conversation — this validates the Task 5 history breakpoint.
4. Any `prompt cache inactive` WARNING → stop, re-check block placement before
   committing docs.

- [ ] **Step 2: Update the implementation note**

In `docs/prompt-caching-upgrade.md`:
- Change `**Status:** To do.` to `**Status:** Implemented 2026-07-03 (plan: docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md). Verified live: cache write, cache read, and tool-loop history reads observed in logs.`
- Append an `## Implementation findings` section recording:
  1. The last-tool marker must copy, not mutate, the shared tool dicts (`_with_cache_marker`).
  2. Context/principles hot-reload invalidates the system+messages cache — expected, same category as the TV-bridge swap.
  3. A **third breakpoint on the final message content block** was added beyond the note's original scope (the note cached only tools+system, leaving the growing conversation uncached on every tool-loop turn). Rebuilt per call on a copy; 3 of 4 breakpoints used. Caveats: (a) a single turn adding >20 content blocks (10+ parallel tool calls) exceeds the 20-block lookback window and re-writes instead of reading — visible in the `prompt cache:` log line; (b) once history exceeds `_HISTORY_LIMIT=40` rows, the sliding window shifts the messages prefix and the messages cache misses once per user turn (tools+system unaffected; optional hysteresis eviction is a follow-up if logs show it matters); (c) a new user turn resumes from the cache entry at the end of the *reconstructed* history, not from mid-tool-loop entries (tool rows are skipped on rebuild).
  4. The **system prompt is now built once per session** (user decision 2026-07-03: doc-version and document checks happen at load, never per prompt). Hot-reload stays event-driven via `ContextLoader.reload_count`; the previous per-message rebuild (2 file reads + 1 `doc_versions` query per prompt) is removed.

- [ ] **Step 3: Append the live-test-log entry**

Add a dated run entry to `docs/live-test-log.md` following its existing anchored format:
prompt-caching verification, the observed `created`/`read`/`uncached` numbers from all
three checks (fresh write, warm read, tool-loop read), pass/fail.

- [ ] **Step 4: Commit**

```bash
git add docs/prompt-caching-upgrade.md docs/live-test-log.md
git commit -m "docs: prompt caching implemented + live-verified; record findings"
```

---

## Self-review notes

- **Spec coverage:** Note §Change item 1 (tools marker) → Task 1; item 2 (system block) → Task 2; §Verification (usage fields, permanent logging) → Tasks 4 and 6. Gap 1 (mutation) → Task 1; gap 2 (hot-reload) → Task 6 docs; gap 3 (history uncached) → Task 5. Load-time version principle (user decision 2026-07-03) → Task 3. Nothing deferred.
- **Beyond the note's scope, deliberately:** Tasks 3 and 5 extend the note's "no other architecture change" — decided 2026-07-03 (user: address all gaps; version checks at load, not per prompt). The note update in Task 6 records this.
- **Out of scope:** 1h TTL (note rejects it for interactive use), `max_tokens` changes, intermediate markers for >20-block turns (documented caveat instead — ClaudIA turns are typically 1–3 tool calls).
- **Type consistency:** `_with_cache_marker(list[dict]) -> list[dict]`, `_system_blocks(str) -> list[dict]`, `_get_system_blocks(self) -> list[dict]` (reads `ContextLoader.reload_count: int`), `_log_cache_usage(usage) -> None`, `_with_history_cache_marker(list) -> list` — names match between tasks and tests.
