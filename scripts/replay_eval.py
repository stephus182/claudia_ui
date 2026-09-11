"""Offline replay eval for the anti-fabrication framework (design 2026-09-11, §4).

Rebuilds the request ClaudIA sent at a stored user turn — the same system blocks, the same
tool declarations (minus TradingView, whose sidecar tools are not persisted), the same
history window and operator message — from a **copy** of the store cut at that turn, and
samples the model's first response N times under each request-shape variant. The metric
is what the first response *does*: calls the expected tool, calls some tool, or narrates.

No tool executes. Nothing reaches IBKR. The real store is opened read-only (SQLite backup
API) and never written. Proposal tools reach nothing by construction (CLAUDE.md Hard Rule 1).

Variants (the design's layers, prototyped here as pure functions over stored rows — the
production implementation follows the plan, with TDD, once the numbers are in):

  v0  today's `_history_to_messages` (baseline)
  v1  L1a block separator + L1c withdrawal of detector-contradicted turns
  v1a L1a alone (separator only)      — sub-variants, to see which half carries v1's effect
  v1c L1c alone (withdrawal only)
  v2  L1b faithful tool cycles: real tool_use blocks, value-free stub results
  v3  v1 + v2
  v4  v3 + effort xhigh (L5)
  v5  v0 + the unbacked-claim operator note + tool_choice any (the L4 retry shape)

Two deviations from production, applied to every variant so the comparison stays fair:
the window is trimmed to start on a user row, and TradingView tools are not declared.
Stored text is un-glued for v1/v3/v4 by inserting a paragraph break at each glued block
boundary — an approximation of what L1a would have stored.

Opt-in and billed:

    CLAUDIA_LIVE_SCHEMA_CHECK=1 python scripts/replay_eval.py --turn 1074 --expect get_order_status \
        --variant v0 --variant v5 --n 8
    CLAUDIA_LIVE_SCHEMA_CHECK=1 python scripts/replay_eval.py --turn 1074 --variant v0 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from claudia.agent import (
    _HISTORY_LIMIT,
    _MAX_TOKENS,
    _UNBACKED_CLAIM_OPERATOR_NOTE,
    ClaudIAAgent,
    _claims_completed_proposal,
    _history_to_messages,
    _with_history_cache_marker,
)
from claudia.context_loader import ContextLoader
from claudia.conversation_store import ConversationStore
from claudia.message_sink import ToolStepHandle
from claudia.opening_status import build_trade_lines

VARIANTS = ("v0", "v1", "v1a", "v1c", "v2", "v3", "v4", "v5")
_GLUE = re.compile(r"([a-z0-9)][.!?:])([A-Z])")
_STUB = "[result not replayed — {n:,} chars. Stale by now: call the tool for the current state.]"


class _NullStep:
    """Tool-step handle that records nothing (the harness never runs the tool loop)."""

    input: str = ""
    output: str = ""

    async def __aenter__(self) -> ToolStepHandle:
        return self  # type: ignore[return-value]

    async def __aexit__(self, *exc: object) -> None:
        return None


class _NullSink:
    """MessageSink double: the agent is built only to assemble a request, never to render."""

    async def send_message(self, text: str) -> None:
        raise AssertionError("the eval harness must never render")

    def tool_step(self, name: str) -> ToolStepHandle:
        raise AssertionError("the eval harness must never run a tool")

    async def send_max_tokens_warning(self) -> None:
        raise AssertionError("the eval harness must never render")

    async def send_system_note(self, text: str) -> None:
        raise AssertionError("the eval harness must never render")

    async def send_order_proposal(self, proposal: dict[str, Any]) -> None:
        raise AssertionError("the eval harness must never render")

    async def send_cancel_proposal(self, proposal: dict[str, Any]) -> None:
        raise AssertionError("the eval harness must never render")

    async def send_modify_proposal(self, proposal: dict[str, Any]) -> None:
        raise AssertionError("the eval harness must never render")


# --- the store, cut at the turn ------------------------------------------------------------


def cut_store(src: Path, turn_id: int, workdir: Path) -> tuple[Path, str, str | None]:
    """Copy the store (backup API, safe against a live writer) and delete everything after
    the turn: messages by id, decisions by time. Returns (copy path, session id, doc version)."""
    dst = workdir / f"claudia-cut-{turn_id}.db"
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as ro, sqlite3.connect(dst) as rw:
        ro.backup(rw)
    with sqlite3.connect(dst) as c:
        row = c.execute(
            "SELECT session_id, created_at, role FROM messages WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None or row[2] != "user":
            raise SystemExit(f"message {turn_id} is not a stored user turn")
        session_id, created_at = row[0], row[1]
        c.execute("DELETE FROM messages WHERE id > ?", (turn_id,))
        c.execute("DELETE FROM decisions WHERE created_at > ?", (created_at,))
        doc_version = c.execute(
            "SELECT doc_version FROM sessions WHERE id=?", (session_id,)
        ).fetchone()[0]
    return dst, session_id, doc_version


def withdrawn_ids(db: Path) -> set[int]:
    """Assistant rows a claim detector contradicted (L1c drops these from the replay)."""
    with sqlite3.connect(db) as c:
        return {
            r[0]
            for r in c.execute(
                "SELECT message_id FROM decisions WHERE decision_type LIKE '%claim_unbacked' "
                "AND message_id IS NOT NULL"
            )
        }


# --- the agent, built as panel_app builds it -----------------------------------------------


def build_agent(db: Path, session_id: str, doc_version: str | None, model: str) -> ClaudIAAgent:
    """The production constructor path minus the UI: real toolkit declarations, real docs."""
    from ibkr_core_mcp import (
        BrowserCookieAuth,
        ClaudeToolkit,
        Config,
        GDriveCache,
        IBKRClient,
        SQLiteStore,
    )

    config = Config.from_env()
    ibkr = IBKRClient(
        config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome"))
    )
    toolkit = ClaudeToolkit(
        client=ibkr, cache=GDriveCache(config), store=SQLiteStore(config), config=config
    )
    loader = ContextLoader(Path(os.environ.get("CLAUDIA_DOCS_PATH", "docs")))
    loader.load_system_prompt()
    try:
        _status, trade_context = build_trade_lines(toolkit, True)
    except Exception as exc:  # the calendar block is context, not the subject under test
        print(f"trade context unavailable ({type(exc).__name__}: {exc}); continuing without it")
        trade_context = None
    return ClaudIAAgent(
        toolkit=toolkit,
        store=ConversationStore(db),
        context_loader=loader,
        session_id=session_id,
        sink=_NullSink(),
        model=model,
        doc_version=doc_version,
        trade_context=trade_context,
    )


# --- history → messages, per variant --------------------------------------------------------


def _trim_to_first_user(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, r in enumerate(rows):
        if r["role"] == "user":
            return rows[i:]
    return []


def _append(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Append content blocks, merging into the previous message when the role repeats."""
    if messages and messages[-1]["role"] == role:
        prev = messages[-1]["content"]
        if isinstance(prev, str):
            prev = [{"type": "text", "text": prev}]
        messages[-1]["content"] = [*prev, *blocks]
    else:
        messages.append({"role": role, "content": blocks})


def _text(s: str | None) -> list[dict[str, Any]]:
    return [{"type": "text", "text": s or ""}]


def build_messages(
    rows: list[dict[str, Any]], variant: str, withdrawn: set[int]
) -> list[dict[str, Any]]:
    """The replay for one variant. v0 is the production function, untouched."""
    rows = _trim_to_first_user(rows)
    if variant in ("v0", "v5"):
        return _history_to_messages(rows)
    unglue = variant in ("v1", "v1a", "v3", "v4")
    withdraw = variant in ("v1", "v1c", "v3", "v4")
    cycles = variant in ("v2", "v3", "v4")

    messages: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush_cycle() -> None:
        if not pending:
            return
        _append(
            messages,
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": f"toolu_replay_{t['id']}",
                    "name": t["tool_name"],
                    "input": json.loads(t["tool_input_json"] or "{}"),
                }
                for t in pending
            ],
        )
        _append(
            messages,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": f"toolu_replay_{t['id']}",
                    "content": _STUB.format(n=len(t["tool_result_json"] or "")),
                }
                for t in pending
            ],
        )
        pending.clear()

    for r in rows:
        if r["role"] == "tool":
            if cycles:
                pending.append(r)
            continue
        flush_cycle()
        if r["role"] == "user":
            _append(messages, "user", _text(r["content"]))
            continue
        if withdraw and r["id"] in withdrawn:
            continue
        text = r["content"] or ""
        if unglue:
            text = _GLUE.sub(r"\1\n\n\2", text)
        if not text.strip():
            continue  # an empty text block is a 400; the cycle above already stands for the turn
        _append(messages, "assistant", _text(text))
    flush_cycle()
    return messages


# --- one request, one sample ----------------------------------------------------------------


def request_kwargs(
    agent: ClaudIAAgent, messages: list[dict[str, Any]], variant: str, model: str
) -> dict[str, Any]:
    """The `messages.create` body for one sample — handle_message's, plus the variant's knob."""
    if variant == "v5":
        agent._pending_operator_notes.append(_UNBACKED_CLAIM_OPERATOR_NOTE)
    agent._append_operator_message(messages)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": agent._get_system_blocks(),
        "messages": _with_history_cache_marker(messages),
        "tools": agent._all_tools,
    }
    if variant == "v4":
        kwargs["output_config"] = {"effort": "xhigh"}
    if variant == "v5":
        kwargs["tool_choice"] = {"type": "any"}
    return kwargs


def sample(client: anthropic.Anthropic, kwargs: dict[str, Any], expect: set[str]) -> dict[str, Any]:
    """One first response, reduced to what the eval measures (plus the text, for the record)."""
    t0 = time.monotonic()
    response = client.messages.create(**kwargs)
    latency = time.monotonic() - t0
    blocks = [b.type for b in response.content]
    tools = [b.name for b in response.content if b.type == "tool_use"]
    text = "\n\n".join(b.text for b in response.content if b.type == "text")
    usage = response.usage
    details = getattr(usage, "output_tokens_details", None)
    thinking = getattr(details, "thinking_tokens", None) if details else None
    first_tool = tools[0] if tools else None
    return {
        "blocks": blocks,
        "tools": tools,
        "first_tool": first_tool,
        "expected_hit": first_tool in expect if expect else None,
        "called_any_tool": bool(tools),
        "text_blocks": blocks.count("text"),
        "narrated_action": _claims_completed_proposal(text) is not None,
        "stop_reason": response.stop_reason,
        "latency_s": round(latency, 1),
        "input_tokens": usage.input_tokens,
        "cache_read": usage.cache_read_input_tokens,
        "cache_created": usage.cache_creation_input_tokens,
        "output_tokens": usage.output_tokens,
        "thinking_tokens": thinking,
        "text": text,
    }


# --- main -----------------------------------------------------------------------------------


def main() -> int:
    """Parse the grid, cut the store, build the agent, sample, summarise."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--turn", type=int, required=True, help="stored user message id to replay")
    parser.add_argument("--variant", action="append", choices=VARIANTS, required=True)
    parser.add_argument(
        "--expect", action="append", default=[], help="expected first tool (repeatable)"
    )
    parser.add_argument("--n", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true", help="count tokens, sample nothing")
    parser.add_argument(
        "--db", type=Path, default=Path(os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db"))
    )
    parser.add_argument("--out", type=Path, default=Path("data/test-sessions/replay-eval"))
    args = parser.parse_args()

    load_dotenv(override=False)
    if os.environ.get("CLAUDIA_LIVE_SCHEMA_CHECK") != "1":
        raise SystemExit("billed: set CLAUDIA_LIVE_SCHEMA_CHECK=1 to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("no ANTHROPIC_API_KEY resolved (checked .env)")
    model = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")

    args.out.mkdir(parents=True, exist_ok=True)
    db, session_id, doc_version = cut_store(args.db, args.turn, args.out)
    agent = build_agent(db, session_id, doc_version, model)
    rows = agent._store.get_history(session_id, limit=_HISTORY_LIMIT)
    withdrawn = withdrawn_ids(db)
    expect = set(args.expect)
    client = anthropic.Anthropic()
    print(
        f"turn {args.turn} · session {session_id[:8]} · {len(rows)} rows in window · "
        f"withdrawn in window: {sorted(r['id'] for r in rows if r['id'] in withdrawn)} · model {model}"
    )

    for variant in args.variant:
        if args.dry_run:
            kwargs = request_kwargs(agent, build_messages(rows, variant, withdrawn), variant, model)
            kwargs.pop("max_tokens")
            count = client.messages.count_tokens(**kwargs)
            print(
                f"  {variant}: {count.input_tokens:,} input tokens, "
                f"{len(kwargs['messages'])} messages, {len(kwargs['tools'])} tools"
            )
            continue
        out = args.out / f"{args.turn}-{variant}.jsonl"
        results: list[dict[str, Any]] = []
        with out.open("a") as f:
            for i in range(args.n):
                kwargs = request_kwargs(
                    agent, build_messages(rows, variant, withdrawn), variant, model
                )
                try:
                    res = sample(client, kwargs, expect)
                except anthropic.BadRequestError as exc:
                    res = {"error": str(exc)}
                    print(f"  {variant} #{i + 1}: 400 {exc}")
                    f.write(
                        json.dumps({"turn": args.turn, "variant": variant, "i": i, **res}) + "\n"
                    )
                    break
                results.append(res)
                f.write(
                    json.dumps(
                        {"turn": args.turn, "variant": variant, "i": i, "model": model, **res}
                    )
                    + "\n"
                )
                print(
                    f"  {variant} #{i + 1}: blocks={res['blocks']} first_tool={res['first_tool']} "
                    f"thinking={res['thinking_tokens']} in={res['input_tokens']}+cache_read={res['cache_read']} "
                    f"{res['latency_s']}s"
                )
        if results:
            n = len(results)
            hit = sum(1 for r in results if r["expected_hit"]) if expect else None
            any_tool = sum(1 for r in results if r["called_any_tool"])
            narrated = sum(
                1
                for r in results
                if r["narrated_action"] or (r["text_blocks"] >= 2 and not r["called_any_tool"])
            )
            thinking = [r["thinking_tokens"] for r in results if r["thinking_tokens"] is not None]
            print(
                f"  == {variant}: n={n} expected_tool={hit}/{n} any_tool={any_tool}/{n} "
                f"narrated={narrated}/{n} thinking_mean={sum(thinking) / len(thinking) if thinking else None} "
                f"latency_mean={sum(r['latency_s'] for r in results) / n:.1f}s"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
