# API Reference — Source of Truth

**Never assume API behavior, error codes, endpoint paths, or field names from memory. Always
verify against official documentation first. This applies to every external API claudia_ui
touches.**

**Protocol:** Use `WebFetch` to load the relevant doc page before writing any error message,
fix, or diagnosis. Cite the source URL in the error string and in the commit message.

This rule exists because two bugs in one session were caught instantly by checking the
official docs — and had gone undetected for months because nobody checked:
- Flex error 1001 mislabeled twice (rate limit → auth failure → actually transient generation failure)
- Flex endpoint URL wrong from day one (`gdcdyn` vs `ndcdyn`) — Flex API never worked until the doc was read

## IBKR Client Portal API (`ibkr_core_mcp/client.py`, `claude_tools.py`)

| Topic | Official source |
|---|---|
| Client Portal API reference | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/ |
| Web API reference | https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-ref/ |
| Orders / modify (two-call pattern) | https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/ |
| IBKR Campus (general) | https://www.interactivebrokers.com/campus/ibkr-api-page/ |
| Session lifecycle FAQ (timeout duration, `/tickle` interval, 24h/midnight cap) | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#tickle |
| `POST /iserver/auth/ssodh/init` — current, non-deprecated brokerage-session init | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ssodh-init |
| `POST /iserver/reauthenticate` — **Deprecated**, superseded by `ssodh/init` | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#reauthenticate |
| Competing-session / gateway launch walkthrough | https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/ |

**Scraped 2026-07-17 (Firecrawl keyless tier — `interactivebrokers.com` 403s direct `WebFetch`,
see [[feedback-documentation-firecrawl]]).** Verbatim findings, load-bearing for any gateway
session-resilience work:

- *"A session can remain authenticated for up to 24 hours, resetting at midnight for New York,
  U.S.; Zug, Switzerland; or Hong Kong time depending on your nearest connection."* — an absolute
  cap, independent of activity. No keepalive/tickle mechanism can prevent it; a fresh browser +
  2FA login is required at least once every 24h. *"Daily maintenance of IBKR's servers could
  result in a disconnect earlier than the 24 hour period."*
- *"Sessions will time out after approximately 6 minutes without sending new requests or
  maintaining the /tickle endpoint at least every 5 minutes."* Separately: *"A Client Portal API
  brokerage session will timeout if no requests are received within a period of 5 minutes... it
  is recommended to call this endpoint approximately every minute."* — `ConnectivityChecker`'s
  60s poll (`claudia/status.py:28`) is comfortably inside every stated threshold.
- *"If the brokerage session has timed out but the session is still connected to the IBKR
  backend, the response to /auth/status returns 'connected':true and 'authenticated':false.
  Calling the /iserver/auth/ssodh/init endpoint will initialize a new brokerage session."* —
  a documented, silent (no browser/2FA) recovery path for the soft-timeout case specifically,
  distinct from the deprecated `/iserver/reauthenticate` that the existing rule against
  proactively calling session-touching endpoints (see `claudia/status.py`'s `check_ibkr()`
  docstring and `ibkr_core_mcp/client.py`'s `reauthenticate()` docstring) already (correctly)
  bans calling proactively. That `reauthenticate()` docstring reasoned `ssodh/init` doesn't need
  implementing because it's "invoked by the browser-based login flow itself" — that predated
  this FAQ text, which describes the client calling it directly for this exact case.
  **Implemented 2026-07-17** in `claudia_ui` (not `ibkr_core_mcp` — kept intentionally scoped to
  `ConnectivityChecker`): `claudia/status.py`'s `_attempt_soft_recovery()`, wired into
  `_run_checks()` behind a narrow safety condition (previous poll confirmed `OK`, current poll
  shows this exact signature — never on a fresh/settling login or hard disconnect). Unit-tested
  (15 dedicated tests), not yet live-verified — see
  `docs/plans/2026-07-17-ibkr-soft-timeout-recovery.md` Task 5.
- `POST /iserver/auth/ssodh/init` body params: `publish` (bool, required, must be `true` or a
  500 is returned) and `compete` (bool, required — *"Determines if other brokerage sessions
  should be disconnected to prioritize this connection"*). `compete:true` would force-evict a
  concurrent IBKR Mobile/TWS session — hardcoded to `false` in `_attempt_soft_recovery()`. Note
  also: the docs page's own example response for this endpoint has the same shape as `/tickle`'s
  (`authenticated`/`competing`/`connected`/`message` fields) — not a verbatim-quoted guarantee
  that HTTP 200 is returned regardless of outcome the way `/tickle` is documented to (that
  specific sentence isn't in the scraped text for this endpoint), but `_attempt_soft_recovery()`
  checks the body's `authenticated` field rather than trusting the status code alone as a
  defensive precaution given the shared shape — cheap to do, and correct either way.
- *"You cannot be logged into the account you are authenticating with anywhere else before you
  authenticate. You should make sure to log out of the account before attempting to
  authenticate... Just closing the window or application may cause a stale login session."*
  — confirms the `authStatus.competing` warning path in `check_ibkr()` (`claudia/status.py:105-106`)
  reflects a real, documented failure mode, not a hypothetical.

## IBKR Flex Web Service (`ibkr_core_mcp/flex_query.py`)

| Topic | Official source |
|---|---|
| Flex Web Service setup (endpoints, headers) | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3.htm |
| Flex error codes (all 21 codes) | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm |

## Anthropic API (`claudia/agent.py`)

Note: `docs.anthropic.com` 301-redirects to `platform.claude.com` (verified 2026-07-02). New
references should use the canonical `platform.claude.com` host.

| Topic | Official source |
|---|---|
| Messages API (streaming, tool use) | https://docs.anthropic.com/en/api/messages |
| Tool use reference | https://docs.anthropic.com/en/docs/build-with-claude/tool-use |
| Model names and capabilities | https://docs.anthropic.com/en/docs/about-claude/models |
| Prompt caching (breakpoints, pricing, lookback, invalidation) | https://platform.claude.com/docs/en/build-with-claude/prompt-caching |
| Streaming events (`message_start` usage shape) | https://platform.claude.com/docs/en/build-with-claude/streaming |
| Context engineering for agents (just-in-time retrieval, memory, compaction) | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents |

Scraped-evidence convention: design docs and plans that assert API behavior carry a
claim→source table with verbatim quotes and scrape dates — see
`docs/audits/2026-07-03-llm-best-practices-sources.md` for the reference example.

## Claude Code Memory (`CLAUDE.md` itself)

| Topic | Official source |
|---|---|
| CLAUDE.md imports (`@path` syntax, eager-load-at-launch behavior, backtick escape, fenced-code-block exclusion, 4-hop recursion limit) | https://code.claude.com/docs/en/memory |

Verified 2026-07-10. Confirmed claims: a bare `@path` reference "is expanded and loaded into
context at launch," and "splitting into `@path` imports helps organization but does not
reduce context, since imported files load at launch." Backtick-wrapping a path (`` `docs/foo.md` ``)
keeps it a literal reference instead of an import; import parsing already skips Markdown
code spans and fenced code blocks. This repo's `CLAUDE.md` was found in violation (13 bare
`@docs/...` imports pulling 72,570 tokens into every session) and fixed —
`docs/plans/2026-07-10-claude-md-delink-imports.md` — reducing the per-session
load from 75,480 to 2,910 tokens. `CLAUDE.md`'s own "Pointers" section now carries a short
compliance note citing this same source.

## Google Drive API v3 (`claudia/gdrive_sync.py`)

| Topic | Official source |
|---|---|
| Drive API v3 reference | https://developers.google.com/drive/api/reference/rest/v3 |
| Files: upload / download | https://developers.google.com/drive/api/guides/manage-uploads |

## TradingView MCP (`claudia/tradingview.py`)

| Topic | Official source |
|---|---|
| tradingview-mcp tool list and usage | https://github.com/tradesdontlie/tradingview-mcp |
| Chrome DevTools Protocol | https://chromedevtools.github.io/devtools-protocol/ |

## Chainlit (`claudia/app.py`)

| Topic | Official source |
|---|---|
| Chainlit API reference (Message, Action, Step, Audio) | https://docs.chainlit.io/api-reference/message |
| Chainlit configuration (`.chainlit/config.toml`) | https://docs.chainlit.io/backend/config |
| Chainlit custom CSS / JS | https://docs.chainlit.io/customisation/custom-js |

## Standard libraries used in claudia_ui

| Library | Used in | Official reference |
|---|---|---|
| `requests` | `claudia/agent.py` (`fetch_web_page` tool) | https://docs.python-requests.org/ |
| `html2text` | `claudia/agent.py` (HTML → Markdown for web fetch) | https://github.com/Alir3z4/html2text |
| `watchdog` | `claudia/context_loader.py` (file system event monitoring) | https://watchdog.readthedocs.io/en/stable/ |
| `mcp` | `claudia/tradingview.py` (MCP stdio client for tradingview-mcp sidecar) | https://github.com/modelcontextprotocol/python-sdk |
