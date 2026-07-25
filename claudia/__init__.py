"""ClaudIA — a Panel-based trading assistant over Interactive Brokers.

Conversational access to IBKR data, backtesting, technical analysis, TradingView, an
external candlestick chart pane, and human-confirmed order staging. `ibkr_core_mcp` is a
direct Python import rather than an MCP server: its `ClaudeToolkit` tools drop straight
into the Anthropic SDK `tools=` parameter.

Entry point:

    python -m claudia.panel_app     # serves the UI on http://localhost:8001

Before changing anything in this package, read the Hard Rules in `CLAUDE.md` and the
security model in `SECURITY.md`. The load-bearing ones:

- The LLM has **no** order-execution tools. Placing, modifying, or cancelling an order is a
  UI-layer action behind a physical button click plus Touch ID and a native confirmation
  dialog — never a tool call.
- Order parameters are immutable: ClaudIA proposes the user's exact values or refuses.
- The Panel server binds loopback only, and there is no auth layer.
- All chat rendering goes through `panel_markdown`; Panel's Markdown pane executes raw
  HTML by default.

No re-exports live here deliberately: `panel_app` and both `panel_chart` / `panel_sink`
rely on deferred imports to break cycles, and hoisting names into this module would
reintroduce them.
"""
