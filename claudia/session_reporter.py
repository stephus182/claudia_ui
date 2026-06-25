"""
Auto-generate a structured test session report at session end.

Reads tool calls, decisions, and connectivity state from the current session
and writes a Markdown report to data/test-sessions/YYYY-MM-DD-HHmm.md.

Usage: tell Claude "update docs/project-status.md with the latest test session"
and it will read the report and add a row to the Live Test Log.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)

# Map tool name → readable label for the report.
# Covers all tools from ibkr_core_mcp.ClaudeToolkit, tradingview-mcp curated subset,
# and local tools defined in agent.py. Unlabelled tools fall back to raw tool name.
_TOOL_LABELS: dict[str, str] = {
    # IBKR — account & portfolio
    "get_positions": "IBKR: positions",
    "get_live_orders": "IBKR: live orders",
    "get_portfolio": "IBKR: portfolio",
    "get_account_summary": "IBKR: account summary",
    "get_allocation": "IBKR: allocation",
    "get_ledger": "IBKR: ledger",
    "get_pnl": "IBKR: P&L",
    "get_pa_performance": "IBKR: P&A performance",
    "get_pa_transactions": "IBKR: P&A transactions",
    # IBKR — market data
    "get_market_snapshot": "IBKR: market snapshot",
    "fetch_market_data": "IBKR: historical bars",
    "get_option_chain": "IBKR: option chain",
    "get_futures": "IBKR: futures",
    "get_trading_schedule": "IBKR: trading schedule",
    "get_watchlists": "IBKR: watchlists",
    "run_scanner": "IBKR: scanner",
    # IBKR — contract
    "search_contract": "IBKR: contract lookup",
    "get_contract_info": "IBKR: contract info",
    # IBKR — orders
    "get_order_status": "IBKR: order status",
    "diagnose_orders": "IBKR: diagnose orders",
    "preview_order": "IBKR: preview order",
    # IBKR — alerts
    "create_price_alert": "IBKR: create alert",
    "get_alerts": "IBKR: list alerts",
    "delete_alert": "IBKR: delete alert",
    "activate_alert": "IBKR: toggle alert",
    "modify_price_alert": "IBKR: modify alert",
    "get_notifications": "IBKR: notifications",
    # IBKR — analytics & backtest
    "add_indicators": "Analytics: add indicators",
    "get_analytics": "Analytics: portfolio analytics",
    "run_backtest": "Analytics: backtest",
    "generate_pinescript": "Analytics: generate PineScript",
    # IBKR — Flex trade history
    "get_trades": "Trades: query history",
    "sync_flex_trades": "Trades: Flex sync",
    "sync_flex_archive": "Trades: Flex archive sync",
    "import_flex_file": "Trades: import Flex file",
    "check_flex_coverage": "Trades: coverage check",
    # IBKR — market data cache
    "check_cache": "Cache: check",
    "list_cache": "Cache: list",
    "delete_cache": "Cache: delete",
    # TradingView
    "chart_get_state": "TV: chart state",
    "quote_get": "TV: live quote",
    "data_get_ohlcv": "TV: OHLCV",
    "data_get_study_values": "TV: indicator values",
    "chart_set_symbol": "TV: set symbol",
    "chart_set_timeframe": "TV: set timeframe",
    "indicator_set_inputs": "TV: set indicator inputs",
    "pine_set_source": "TV: inject Pine Script",
    "pine_smart_compile": "TV: compile Pine",
    "pine_get_errors": "TV: Pine errors",
    "pine_get_source": "TV: Pine source",
    "data_get_strategy_results": "TV: strategy results",
    "data_get_equity_curve": "TV: equity curve",
    "tv_health_check": "TV: health check",
    "capture_screenshot": "TV: screenshot",
    # Local tools (agent.py)
    "list_doc_versions": "Docs: list versions",
    "get_doc_version": "Docs: retrieve version",
    "search_past_conversations": "Memory: search history",
    "fetch_web_page": "Web: fetch page",
}


_ERROR_KEYWORDS = ("error", "failed", "exception", "timeout", "unauthorized", "traceback")


def _tool_counts(messages: list[dict]) -> Counter:
    return Counter(
        m["tool_name"] for m in messages
        if m.get("role") == "tool" and m.get("tool_name")
    )


def _error_lines(messages: list[dict]) -> list[str]:
    """Extract tool result snippets that look like failures.

    Reads tool_result_json — TEXT column stored as JSON per conversation_store schema.
    """
    lines = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        result = m.get("tool_result_json") or ""
        if isinstance(result, str) and any(kw in result.lower() for kw in _ERROR_KEYWORDS):
            snippet = result.replace("\n", " ")[:180]
            lines.append(f"`{m['tool_name']}` → {snippet}")
    return lines


def _tool_section(counts: Counter) -> list[str]:
    if not counts:
        return ["- (no tool calls this session)"]
    return [
        f"- {_TOOL_LABELS.get(name, name)}{f' ×{counts[name]}' if counts[name] > 1 else ''}"
        for name in sorted(counts)
    ]


def _decisions_section(decisions: list[dict]) -> list[str]:
    if not decisions:
        return ["- (none)"]
    return [
        f"- [{d.get('decision_type', '?')}] {d.get('summary_text', '')} "
        f"(symbol: {d.get('symbol', '—')})"
        for d in decisions
    ]


def generate_session_report(
    session_id: str,
    store: "ConversationStore",
    connectivity: dict[str, str] | None = None,
    doc_version: str | None = None,
) -> Path | None:
    """Generate a Markdown session report and write it to data/test-sessions/.

    Called from app.py on_stop. Returns the report path, or None on error.
    """
    try:
        report_dir = Path("data/test-sessions")
        report_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        path = report_dir / now.strftime("%Y-%m-%d-%H%M.md")

        messages = store.get_history(session_id, limit=9999)
        decisions = store.get_decisions(session_id)
        conn = connectivity or {}

        lines: list[str] = [
            f"# Test Session — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**Session ID:** `{session_id}`  ",
            f"**Doc version:** {doc_version or 'unknown'}  ",
            (
                f"**Connectivity at end:** "
                f"IBKR={conn.get('ibkr', 'UNKNOWN')}  "
                f"GDrive={conn.get('gdrive', 'UNKNOWN')}  "
                f"TradingView={conn.get('tradingview', 'UNKNOWN')}"
            ),
            "",
            "## Tools Called",
            *_tool_section(_tool_counts(messages)),
            "",
            "## Decisions",
            *_decisions_section(decisions),
            "",
            "## Errors / Anomalies",
            *(_error_lines(messages) or ["- None detected"]),
            "",
            "---",
            "",
            "> Auto-generated by `claudia/session_reporter.py` at session end.  ",
            "> Tell Claude: *\"update docs/project-status.md with the latest test session\"*",
        ]

        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("Session report: %s", path)
        return path

    except Exception:
        log.exception("Failed to write session report for %s", session_id)
        return None
