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
| Flex error codes (20 codes) | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm |

**Scraped 2026-07-21 (Firecrawl keyless tier).** Verbatim findings:

- flex3.htm (setup): generated tokens are *"valid for a 6 hour period by
  default"* and can optionally be restricted to a specific *"Valid For IP
  Address"*; generating a new token *"invalidate[s] the current one."* The
  `SendRequest` call requires `t` (token), `q` (Flex Query ID), and `v=3`
  (*"if you do not specify a Version, the system will use Version 2"*), plus
  optional `fd`/`td` (yyyymmdd date-range override, up to 365 days) or `p`
  (period override). Programmatic access *"requires the User-Agent HTTP
  header to be set"* (e.g. `Python/3.4.1`). **No content on this page
  discusses a T+1 cutoff or a fixed daily generation schedule for Flex
  statements** — only token/request mechanics; that question is not
  answered by this URL and needs a different source. Page footer: "Last
  updated on June 8, 2026."
- flex3error.htm (error codes): the scraped table lists **20**
  ErrorCode/ErrorMessage rows (1001, 1003–1021 — note **1002 is absent**
  from the published table), not 21 as this doc previously stated; the
  "Topic" cell above has been corrected accordingly. Error 1018 documents
  an explicit rate limit: *"Too many requests have been made from this
  token... Limited to one request per second, 10 requests per minute (per
  token)."* Several codes (1001, 1004–1009, 1019, 1021) are explicitly
  transient/"try again shortly" conditions, distinct from hard failures
  (1011 inactive, 1012 expired, 1013 IP restriction, 1015 invalid token,
  1016 invalid account, 1017 invalid reference code). Page footer: "Last
  updated on August 18, 2025."

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

**Scraped 2026-07-21 (Firecrawl keyless tier).** Note: both URLs now
301/soft-redirect to a `.../workspace/drive/api/...` path segment
(`developers.google.com/workspace/drive/api/reference/rest/v3` and
`.../workspace/drive/api/guides/manage-uploads`); the originally-cited
URLs without `workspace/` still resolve and served the current content —
no action needed. Verbatim findings:

- Reference page: service is `googleapis.com/drive/v3`; *"This service has
  the following service endpoint and all URIs below are relative to this
  service endpoint: `https://www.googleapis.com`."* No v3 deprecation
  notice appeared on the scraped page. Resource list: `about`,
  `accessproposals`, `approvals`, `apps`, `changes`, `channels`,
  `comments`, `drives`, `files`, `operations`, `permissions`, `replies`,
  `revisions` — includes `accessproposals`/`approvals`, which are newer
  additions not present in older v3 snapshots.
- Manage-uploads page: three upload types are documented. *"Simple upload
  (`uploadType=media`)... a small media file (5 MB or less) without
  supplying metadata."* *"Multipart upload (`uploadType=multipart`)... a
  small file (5 MB or less) along with metadata."* *"Resumable upload
  (`uploadType=resumable`)... for large files (greater than 5 MB) and when
  there's a high chance of network interruption... also a good choice for
  most applications because they also work for small files at a minimal
  cost of one additional HTTP request per upload."* — i.e. 5 MB is the
  documented simple/multipart ceiling, not an overall API upload-size
  limit. Whether `gdrive_sync.py`'s `claudia.db` upload currently uses
  simple, multipart, or resumable was not checked as part of this
  citation pass — worth a follow-up read of `claudia/gdrive_sync.py`
  against this contract if `claudia.db` can exceed 5 MB.

## TradingView MCP (`claudia/tradingview.py`)

| Topic | Official source |
|---|---|
| tradingview-mcp tool list and usage | https://github.com/tradesdontlie/tradingview-mcp |
| Chrome DevTools Protocol | https://chromedevtools.github.io/devtools-protocol/ |

**Scraped 2026-07-21 (Firecrawl keyless tier).** `tradingview-mcp` is a
third-party GitHub repo, not TradingView Inc. documentation — verified
against its actual README and repo metadata, not a vendor docs site.
Verbatim findings:

- tradesdontlie/tradingview-mcp: MIT-licensed (*"MIT — see LICENSE... The
  MIT license applies to the source code of this project only. It does
  not grant any rights to TradingView's software, data, trademarks, or
  intellectual property"*). ~5k stars / 2.2k forks, 19 branches, **0
  tags** (no pinned releases — the repo tracks `main` directly, consistent
  with this project's `./scripts/archive-tv-mcp.sh` snapshot-to-`vendor/`
  approach rather than trusting a release tag). Latest commit at scrape
  time: `0ac960a` (PR #377, "docs/tool-count-84," Jul 21 2026). README:
  *"Personal AI assistant for your TradingView Desktop charts. Connects
  Claude Code to your locally running TradingView app via Chrome DevTools
  Protocol..."* Explicit warnings: *"This tool is not affiliated with,
  endorsed by, or associated with TradingView Inc."*; *"Requires a valid
  TradingView subscription. This tool does not bypass or circumvent any
  TradingView paywall or access control."*; and — directly relevant to
  `docs/tradingview-mcp-recovery.md`'s fragility framing — *"This tool
  accesses undocumented internal TradingView APIs via the Electron debug
  interface. These can change or break without notice in any TradingView
  update. Pin your TradingView Desktop version if stability matters to
  you."*
- chromedevtools.github.io/devtools-protocol/: *"The Chrome DevTools
  Protocol allows for tools to instrument, inspect, debug and profile
  Chromium, Chrome and other Blink-based browsers."* Three documented
  protocol variants: tip-of-tree (*"changes frequently and can break at
  any time... no backwards compatibility support guaranteed"*),
  v8-inspector (Node.js debugging), and *"stable 1.3 protocol... tagged at
  Chrome 64... a smaller subset of the complete protocol."* The page does
  not state which variant TradingView Desktop's Electron build exposes at
  `--remote-debugging-port=9222` — an open question this URL alone does
  not answer.

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

**Scraped 2026-07-21 (Firecrawl keyless tier).** The two GitHub-repo
entries (`html2text`, `mcp`) were verified against their actual README
and repo metadata, not a "documentation site." Verbatim findings:

- `requests`: `docs.python-requests.org` redirects to
  `requests.readthedocs.io`; current release *"Release v2.34.2"*;
  *"Requests officially supports Python 3.10+, and runs great on PyPy."*
- `html2text`: GitHub repo (`Alir3z4/html2text`), 2.2k stars / 297 forks /
  29 tags. *"html2text is a Python script that converts a page of HTML
  into clean, easy-to-read plain ASCII text. Better yet, that ASCII also
  happens to be valid Markdown."* The README's own license line:
  *"Originally written by Aaron Swartz. This code is distributed under
  the GPLv3."* — GPLv3, not a permissive license; not otherwise verified
  here whether that's a problem for claudia_ui (no stated dependency
  license policy found in this doc).
- `watchdog`: the "stable" ReadTheDocs alias this doc cites
  (`/en/stable/`) is headed **"watchdog 0.9.0 documentation"** verbatim
  in the scraped page — this looks stale relative to `watchdog`'s actual
  current PyPI releases (`watchdog` has shipped well past 0.9.0 for
  years). Flagged rather than assumed current, since this project's own
  convention is not to state library versions without a scraped source,
  and the scraped source itself looks out of date for what a "stable"
  alias implies — worth checking `pip show watchdog` in the venv directly
  rather than trusting this URL for version claims. Content itself:
  *"Python API library and shell utilities to monitor file system
  events... A cross-platform API."*
- `mcp`: GitHub repo (`modelcontextprotocol/python-sdk`), 23.7k stars /
  3.7k forks / 70 tags, MIT-licensed. Load-bearing find — a production
  warning on the scraped README: *"This README documents v2 of the MCP
  Python SDK — a pre-release (alpha/beta) line under active development.
  Do not use v2 in production... v1.x is the only stable release line and
  remains recommended for production... If your package depends on
  `mcp`, add a `<2` upper bound to your version constraint (for example
  `mcp>=1.27,<2`) before the stable release lands."* `claudia_ui`'s
  `pyproject.toml` pin for `mcp` was not checked as part of this citation
  pass — worth a follow-up to confirm it already excludes `2.0.0aN`/
  `2.0.0bN` pre-releases.
