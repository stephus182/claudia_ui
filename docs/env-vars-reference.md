# Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key — the only variable `Config.from_env()` raises `ConfigError` for if unset |
| `IBKR_GATEWAY_URL` | optional | IBKR Client Portal Gateway URL (default: `https://localhost:5055/v1/api`) |
| `IBKR_AUTH_BROWSER` | optional | Browser whose localhost cookies `BrowserCookieAuth` reads for IBKR session auth: `chrome`, `safari`, `firefox`, `edge` (default: `chrome`) |
| `GOOGLE_DRIVE_FOLDER_ID` | optional | Root Drive folder — parent of `db/`, `market_data/`, and `account_data/` subfolders (default: `""` — Drive sync disabled) |
| `GDRIVE_DB_FOLDER_ID` | optional | Drive folder for claudia.db (auto-created as `db/` inside root if unset) |
| `GDRIVE_CACHE_FOLDER_ID` | optional | Drive folder for Parquet cache (auto-created as `market_data/` inside root if unset) |
| `GDRIVE_ACCOUNT_FOLDER_ID` | optional | Drive folder for Flex XML archives + `store.db` backup (auto-created as `account_data/` inside root if unset) |
| `GDRIVE_TOKEN_FILE` | optional | OAuth2 token file path (default: `~/.ibkr_core/token.json`) |
| `GDRIVE_CREDENTIALS_FILE` | optional | OAuth2 credentials file path (default: `~/.ibkr_core/credentials.json`) |
| `IBKR_SQLITE_PATH` | optional | ibkr_core_mcp SQLite store path (default: `~/.ibkr_core/store.db`) |
| `IBKR_FLEX_TOKEN` | optional | For full trade history sync |
| `IBKR_FLEX_QUERY_ID` | optional | For full trade history sync |
| `CLAUDIA_MODEL` | optional | Claude model (default: `claude-opus-4-8`). **Two independent requirements — see the note below.** Known-good: `claude-opus-4-8`, `claude-opus-5`, `claude-fable-5`, `claude-mythos-5`. |
| `CLAUDIA_DOCS_PATH` | optional | Path to context.md / principles.md (default: `docs/`) |
| `CLAUDIA_DB_PATH` | optional | ClaudIA SQLite DB path (default: `data/claudia.db`) |
| `CLAUDIA_VOICE_ENABLED` | optional | Reserved — TTS output not yet implemented |
| `FIRECRAWL_API_KEY` | optional | Firecrawl API key — enables `firecrawl_search` and `firecrawl_crawl` tools; keyless free tier works without it (rate-limited) |
| `GDRIVE_WEB_DOCS_FOLDER_ID` | optional | Drive folder for `firecrawl_crawl` saved web docs (`web_docs/` subfolder of root if unset) |
| `CRAWL4AI_PROFILES_DIR` | optional | Directory for Crawl4AI browser login profiles (default: `~/.ibkr_core/crawl4ai_profiles`); used by `ibkr_core_mcp/scrape_fallback.py` both as the Firecrawl fallback *and* as the `fetch_page` tool's direct route. **This is what makes paywalled sites (FT, WSJ, NYT) return full articles.** Create one per domain with `python -m ibkr_core_mcp.scrape_fallback create-profile <url>` — interactive, needs a real terminal. |
| `TRADINGVIEW_MCP_PATH` | optional | Path to `tradingview-mcp` entry point (`src/server.js`); auto-discovered if unset |
| `TRADINGVIEW_DEBUG_PORT` | optional | Chrome debugging port (default: `9222`). Forwarded to the sidecar under **three** names — `TV_CDP_PORT`, `CDP_PORT` (current sidecar) and `CHROME_REMOTE_DEBUG_PORT` (older builds + the `vendor/` fallback). The sidecar renamed this once and a rename fails silently at 9222; see `docs/tradingview-reference.md` § Upgrading the sidecar |

## `CLAUDIA_MODEL` — both requirements must hold

A model has to satisfy **two independent** constraints, and meeting one says nothing about
the other. Checking only the first is how `claude-sonnet-4-6` came to be recommended here:
it satisfies requirement 1, fails requirement 2, and was named as "the supported
alternative" from 2026-07-24 until this was corrected on 2026-08-05.

1. **Adaptive thinking.** `agent.py` sends `thinking={"type": "adaptive"}` unconditionally.
   Extended-thinking-only models (`claude-haiku-4-5`, `claude-sonnet-4-5`, `claude-opus-4-5`)
   reject it with a 400 on **every** message, so the failure is immediate and obvious.
   [Per-model table](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)

2. **Mid-conversation system messages.** `agent._append_operator_message` appends a
   `{"role": "system"}` entry to `messages` — the non-spoofable operator channel carrying
   the called-tool ledger, the proposal-emission and completed-order records, and any
   guardrail notices. Supported on **Claude Opus 4.8,
   Claude Opus 5, Claude Fable 5 and Claude Mythos 5 only**; the docs exclude the Sonnet
   line explicitly (*"not available on Claude Sonnet 5; use the top-level `system` field
   instead"*, read 2026-08-05).
   [Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

   Measured rather than inferred — the page states the exclusion but gives no error.
   Probed 2026-08-05 against `claude-sonnet-4-6` with the production message shape:

   ```text
   400 invalid_request_error: "role 'system' is not supported on this model"
   ```

**Requirement 2 fails partway through a session, not at startup.** The operator message is
appended only when there is something to deliver, so an unsupported model behaves perfectly
until the channel first carries a payload — and returns a 400 on every turn thereafter.

**When that happens moved on 2026-08-11, and it is now much earlier.** It used to be "the
first rendered order proposal", which made the failure rare and late. The **called-tool
ledger** rides the same channel, so it now activates on the turn *after any non-proposal
tool call*. Measured over this repo's own history: **53 of the 63 sessions that ever
received a user message (84%) made a ledger-eligible call**, against **10 (16%)** that
recorded a `trade_proposed` decision. So on an unsupported model this went from an edge case
to the normal path — worse if unnoticed, easier to spot if it is.

The channel that breaks carries the payloads that stop ClaudIA answering from memory about a
tool result it no longer holds, and stop it denying that a staged order exists.

`agent.warn_if_model_lacks_operator_channel` logs a loud ERROR at startup for anything
outside the known-good set. It warns rather than refusing, since the list can only go stale
by omitting a *newly supported* model — add it to `agent._OPERATOR_CHANNEL_MODELS` in that
case. There is deliberately **no** silent fallback to a `<system-reminder>` block in the
user turn (which the Anthropic docs suggest for unsupported models): that channel is
forgeable by the model, and quietly downgrading a non-spoofable one is worse than not
running on that model at all.
