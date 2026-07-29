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
| `CLAUDIA_MODEL` | optional | Claude model (default: `claude-opus-4-8`). Must support **adaptive thinking** — `agent.py` sends `thinking={"type": "adaptive"}` unconditionally, and extended-thinking-only models (`claude-haiku-4-5`, `claude-sonnet-4-5`, `claude-opus-4-5`) reject it with a 400 on every message. `claude-sonnet-4-6` is the supported alternative. [Per-model table](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting) |
| `CLAUDIA_DOCS_PATH` | optional | Path to context.md / principles.md (default: `docs/`) |
| `CLAUDIA_DB_PATH` | optional | ClaudIA SQLite DB path (default: `data/claudia.db`) |
| `CLAUDIA_VOICE_ENABLED` | optional | Reserved — TTS output not yet implemented |
| `FIRECRAWL_API_KEY` | optional | Firecrawl API key — enables `firecrawl_search` and `firecrawl_crawl` tools; keyless free tier works without it (rate-limited) |
| `GDRIVE_WEB_DOCS_FOLDER_ID` | optional | Drive folder for `firecrawl_crawl` saved web docs (`web_docs/` subfolder of root if unset) |
| `CRAWL4AI_PROFILES_DIR` | optional | Directory for Crawl4AI browser login profiles (default: `~/.ibkr_core/crawl4ai_profiles`); used by `ibkr_core_mcp/scrape_fallback.py` both as the Firecrawl fallback *and* as the `fetch_page` tool's direct route. **This is what makes paywalled sites (FT, WSJ, NYT) return full articles.** Create one per domain with `python -m ibkr_core_mcp.scrape_fallback create-profile <url>` — interactive, needs a real terminal. |
| `TRADINGVIEW_MCP_PATH` | optional | Path to `tradingview-mcp` entry point (`src/server.js`); auto-discovered if unset |
| `TRADINGVIEW_DEBUG_PORT` | optional | Chrome debugging port (default: `9222`) |
