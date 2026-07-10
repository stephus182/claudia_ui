# Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `IBKR_GATEWAY_URL` | ✅ | IBKR Client Portal Gateway URL |
| `IBKR_AUTH_BROWSER` | optional | Browser whose localhost cookies `BrowserCookieAuth` reads for IBKR session auth: `chrome`, `safari`, `firefox`, `edge` (default: `chrome`) |
| `GOOGLE_DRIVE_FOLDER_ID` | ✅ | Root Drive folder — parent of `db/`, `market_data/`, and `account_data/` subfolders |
| `GDRIVE_DB_FOLDER_ID` | optional | Drive folder for claudia.db (auto-created as `db/` inside root if unset) |
| `GDRIVE_CACHE_FOLDER_ID` | optional | Drive folder for Parquet cache (auto-created as `market_data/` inside root if unset) |
| `GDRIVE_ACCOUNT_FOLDER_ID` | optional | Drive folder for Flex XML archives + `store.db` backup (auto-created as `account_data/` inside root if unset) |
| `GDRIVE_TOKEN_FILE` | ✅ | OAuth2 token file path |
| `GDRIVE_CREDENTIALS_FILE` | ✅ | OAuth2 credentials file path |
| `IBKR_SQLITE_PATH` | ✅ | ibkr_core_mcp SQLite store path |
| `IBKR_FLEX_TOKEN` | optional | For full trade history sync |
| `IBKR_FLEX_QUERY_ID` | optional | For full trade history sync |
| `CLAUDIA_MODEL` | optional | Claude model (default: `claude-opus-4-8`) |
| `CLAUDIA_DOCS_PATH` | optional | Path to context.md / principles.md (default: `docs/`) |
| `CLAUDIA_DB_PATH` | optional | ClaudIA SQLite DB path (default: `data/claudia.db`) |
| `CLAUDIA_VOICE_ENABLED` | optional | Reserved — TTS output not yet implemented |
| `FIRECRAWL_API_KEY` | optional | Firecrawl API key — enables `firecrawl_search` and `firecrawl_crawl` tools; keyless free tier works without it (rate-limited) |
| `GDRIVE_WEB_DOCS_FOLDER_ID` | optional | Drive folder for `firecrawl_crawl` saved web docs (`web_docs/` subfolder of root if unset) |
| `CRAWL4AI_PROFILES_DIR` | optional | Directory for Crawl4AI browser login profiles (default: `~/.ibkr_core/crawl4ai_profiles`); used by the Playwright-based fallback scraper in `ibkr_core_mcp/scrape_fallback.py` when Firecrawl returns low-quality content |
| `TRADINGVIEW_MCP_PATH` | optional | Path to `tradingview-mcp` entry point (`src/server.js`); auto-discovered if unset |
| `TRADINGVIEW_DEBUG_PORT` | optional | Chrome debugging port (default: `9222`) |
