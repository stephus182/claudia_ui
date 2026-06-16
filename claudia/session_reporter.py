"""
Auto-generate a structured test session report at session end.

Reads tool calls, decisions, and connectivity state from the current session
and writes a Markdown report to data/test-sessions/YYYY-MM-DD-HHmm.md.

Usage: tell Claude "update docs/project-status.md with the latest test session"
and it will read the report and check off the live test items.
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

# Map tool name → readable label for the report
_TOOL_LABELS: dict[str, str] = {
    # IBKR
    "get_positions": "IBKR: positions",
    "get_orders": "IBKR: open orders",
    "get_portfolio": "IBKR: portfolio",
    "get_account_summary": "IBKR: account summary",
    "get_market_snapshot": "IBKR: market snapshot",
    "search_contract": "IBKR: contract lookup",
    "create_price_alert": "IBKR: create alert",
    "get_alerts": "IBKR: list alerts",
    "delete_alert": "IBKR: delete alert",
    "activate_alert": "IBKR: toggle alert",
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
    # Local
    "list_doc_versions": "Docs: list versions",
    "get_doc_version": "Docs: retrieve version",
}

# Map tool names → live test plan section items (matches wording in project-status.md)
_LIVE_TEST_COVERAGE: list[tuple[set[str], str]] = [
    ({"get_positions"}, "IBKR tools: get_positions"),
    ({"get_orders"}, "IBKR tools: get_orders"),
    ({"create_price_alert"}, "IBKR tools: create_price_alert"),
    ({"get_alerts"}, "IBKR tools: get_alerts"),
    ({"chart_get_state", "quote_get", "data_get_ohlcv"}, "TradingView: chart/quote tools"),
    ({"chart_set_symbol", "chart_set_timeframe"}, "TradingView: chart control"),
    ({"pine_set_source"}, "TradingView: Pine Script inject"),
    ({"data_get_strategy_results", "data_get_equity_curve"}, "TradingView: strategy results"),
    ({"list_doc_versions", "get_doc_version"}, "Doc versioning tools"),
]


def generate_session_report(
    session_id: str,
    store: "ConversationStore",
    connectivity: dict[str, str] | None = None,
    doc_version: str | None = None,
) -> Path | None:
    """
    Generate a Markdown session report and write it to data/test-sessions/.

    Called from app.py on_stop. Returns the report path, or None on error.
    The report is readable by Claude to update docs/project-status.md.
    """
    try:
        report_dir = Path("data/test-sessions")
        report_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        path = report_dir / now.strftime("%Y-%m-%d-%H%M.md")

        # Pull session data
        messages = store.get_history(session_id, limit=9999)
        decisions = store.get_decisions(session_id)

        # Tool calls: messages with role='tool'
        tool_names = [
            m["tool_name"] for m in messages
            if m.get("role") == "tool" and m.get("tool_name")
        ]
        tool_counts = Counter(tool_names)

        # Errors: tool results that look like failures
        error_lines: list[str] = []
        for m in messages:
            if m.get("role") != "tool":
                continue
            result = m.get("tool_result") or ""
            if isinstance(result, str) and any(
                kw in result.lower()
                for kw in ("error", "failed", "exception", "timeout", "unauthorized", "traceback")
            ):
                snippet = result.replace("\n", " ")[:180]
                error_lines.append(f"`{m['tool_name']}` → {snippet}")

        # Infer live test coverage from tools called
        tool_set = set(tool_names)
        covered: list[str] = []
        for required_tools, label in _LIVE_TEST_COVERAGE:
            if required_tools & tool_set:  # any intersection
                covered.append(label)

        proposed = [d for d in decisions if d.get("decision_type") == "trade_proposed"]
        staged   = [d for d in decisions if d.get("decision_type") == "trade_staged"]
        if proposed:
            symbols = ", ".join(d.get("symbol", "?") for d in proposed)
            covered.append(f"Order proposed ({symbols})")
        if staged:
            symbols = ", ".join(d.get("symbol", "?") for d in staged)
            covered.append(f"Order staged — full gate flow ({symbols})")

        # Connectivity at session end
        conn = connectivity or {}
        ibkr   = conn.get("ibkr",         "UNKNOWN")
        gdrive = conn.get("gdrive",        "UNKNOWN")
        tv     = conn.get("tradingview",   "UNKNOWN")

        # Build report lines
        lines: list[str] = [
            f"# Test Session — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**Session ID:** `{session_id}`  ",
            f"**Doc version:** {doc_version or 'unknown'}  ",
            f"**Connectivity at end:** IBKR={ibkr}  GDrive={gdrive}  TradingView={tv}",
            "",
            "## Tools Called",
        ]

        if tool_counts:
            for name in sorted(tool_counts):
                label = _TOOL_LABELS.get(name, name)
                count = tool_counts[name]
                suffix = f" ×{count}" if count > 1 else ""
                lines.append(f"- {label}{suffix}")
        else:
            lines.append("- (no tool calls this session)")

        lines += ["", "## Decisions"]
        if decisions:
            for d in decisions:
                lines.append(
                    f"- [{d.get('decision_type', '?')}] {d.get('summary_text', '')} "
                    f"(symbol: {d.get('symbol', '—')})"
                )
        else:
            lines.append("- (none)")

        lines += ["", "## Errors / Anomalies"]
        if error_lines:
            for e in error_lines:
                lines.append(f"- {e}")
        else:
            lines.append("- None detected")

        lines += ["", "## Live Test Coverage Inferred"]
        if covered:
            for item in covered:
                lines.append(f"- [x] {item}")
        else:
            lines.append("- (no test items inferred — check manually)")

        lines += [
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
