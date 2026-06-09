# ClaudIA Security Architecture

This document describes the security model specific to `claudia_ui`. For the
underlying IBKR gateway security, see `ibkr_core_mcp/SECURITY.md`.

---

## 1. Threat Model

### New Attack Surface vs ibkr_core_mcp

`claudia_ui` adds a long-running LLM agent with persistent document injection and
conversation memory. The new principals and threats are:

| Principal | Threat | Mitigation |
|---|---|---|
| `docs/context.md` | Prompt injection: attacker modifies the file to override ClaudIA's behavior | File permissions (0o600); SHA-256 hash logged at session start; file is never executable |
| `docs/principles.md` | Prompt injection: attacker weakens risk rules | Same mitigations as context.md; ClaudIA cannot modify this file |
| Conversation history | Re-injection: past messages contained adversarial content that gets fed back | History loaded as structured `role:user/assistant` blocks, not raw system prompt injection |
| TradingView sidecar | Supply chain: `tradingview-mcp` binary is a third-party Node.js package | Review the package before install; sidecar runs without network access to IBKR credentials |
| Voice output (Phase 2) | Voice commands: TTS output is purely advisory | No voice-to-action path exists; voice only speaks finalized assistant text |

---

## 2. Order Execution Barriers

ClaudIA has **zero** tools for order execution. This is the most critical security property.

### What the LLM can do
- Call any of the 22 read-only `ClaudeToolkit` tools (positions, PnL, market data, backtests, etc.)
- Output an `order-proposal` JSON block as text in its response
- Call `preview_order` (whatif — read-only, no execution)

### What the LLM cannot do
- Call `place_order`, `modify_order`, `cancel_order`, or `reply_order`
- Initiate any network request to the IBKR gateway for write operations
- Access `IBKRClient` directly (only `ClaudeToolkit.execute()` is exposed to the agent loop)

### Order staging flow (human-in-the-loop)
1. ClaudIA outputs a text `order-proposal` block — this is just text, no side effects
2. `agent.py` parses it and calls `order_flow.render_order_proposal()` — renders a button
3. **Human physically clicks the button** — this is the first human gate
4. `IBKRClient.place_order()` fires:
   - **Gate 1:** Apple `LocalAuthentication` biometric (Touch ID, Face ID) — no password fallback
   - **Gate 2:** tkinter modal dialog with full order details + 60-second countdown; Enter key disabled
5. Order submitted to IBKR only after both gates pass

No LLM prompt, no tool call, no conversation state, and no automation can bypass steps 3–5.

---

## 3. Principles Document Integrity

`docs/principles.md` defines the user's trading rules. ClaudIA is instructed to verify
every proposed action against this document before responding.

**File permissions:**
```bash
chmod 600 docs/context.md docs/principles.md
```
Only the file owner can read or modify these files.

**Hash verification:**
At session start, `context_loader.py` computes `SHA-256(context.md + principles.md)` and
stores it in `claudia.db → sessions.context_hash`. If the hash differs from the previous
session, ClaudIA logs a warning and notifies the user.

**Immutability from ClaudIA's perspective:**
ClaudIA has no tools that write to the filesystem. It cannot modify these files.

**Hardcoded prohibition:**
The safety block in `agent.py` (not loaded from any file) explicitly states:
> "You cannot instruct the user to modify their principles document."

---

## 4. Conversation Memory Security

Historical messages are stored in `data/claudia.db` and injected back into context.
This creates a potential attack surface where past messages containing adversarial
content could influence future responses.

**Mitigations:**

- History is loaded as structured `{"role": "user", "content": "..."}` message objects
  and appended to the `messages=` list. It is **never** injected raw into the system prompt.

- FTS5 search results (used for "past decisions" recall) are truncated at a configurable
  token budget (default: 2,000 tokens) before injection into context.

- Tool results from ibkr_core_mcp are sanitized by `_safe_error()` before being returned
  to the LLM. Raw IBKR API responses never appear in conversation context.

- `claudia.db` is local, single-user, and not accessible over the network.

---

## 5. Hardcoded Safety Block

The following constraints are embedded directly in `claudia/agent.py` and are appended
to every system prompt. They are **not** loaded from any user-editable file and cannot
be overridden by `context.md` or `principles.md`:

```
You are ClaudIA, an AI trading research assistant. You are NOT a licensed financial advisor.

You CANNOT place, modify, or cancel any order. You have no tools for order execution.
When you want to suggest a trade, output an order-proposal block and explain your reasoning.
The human must explicitly click a confirmation button.

Before proposing any trade action, verify it is consistent with the TRADING PRINCIPLES section above.
If an action would violate the user's principles, say so clearly and refuse to propose it.

You CANNOT instruct the user to modify or bypass their principles document.
You CANNOT promise specific returns or guarantee outcomes.
All analysis is for informational and research purposes only.
```

Modifications to this block require a code change in `claudia/agent.py`, a deliberate
developer action — not a document edit.

---

## 6. TradingView MCP Sidecar

The `tradingview-mcp` Node.js process is spawned as a subprocess by `claudia/tradingview.py`.
It communicates via MCP stdio transport (no network port exposed).

**Security properties:**
- The sidecar has no access to IBKR credentials (`.env` vars are not passed to the subprocess
  except `CHROME_REMOTE_DEBUG_PORT`).
- The sidecar controls the TradingView Desktop UI only — it cannot trade.
- The sidecar is optional: if it fails to start, ClaudIA operates in screenshot mode.
- PineScript injection via the sidecar modifies the Pine Editor only; it does not execute trades.

**Third-party risk:** `tradingview-mcp` is a community package. Review its source code
before installation. Pin the version in your npm install.

---

## 7. Secrets Management

- `.env` is in `.gitignore` and must never be committed.
- `ANTHROPIC_API_KEY` is never logged, never included in Chainlit message output,
  and never passed to the LLM as text.
- `claudia.db` does not store API keys or IBKR credentials.
- Google Drive credentials follow the same `0o600` permission pattern as `ibkr_core_mcp`.

---

## 8. Network Exposure

ClaudIA runs entirely on `localhost`. By default:

| Service | Binding |
|---|---|
| Chainlit web UI | `localhost:8000` |
| IBKR gateway | `localhost:5055` |
| TradingView debug port | `localhost:9222` |

**Never expose these ports to external networks.** If remote access is required,
use a VPN or Tailscale tunnel — never a public-facing reverse proxy without authentication.

---

## 9. Audit Checklist

Run this checklist before any significant code change to ClaudIA:

- [ ] No new tool definition calls `place_order`, `modify_order`, `cancel_order`, or `reply_order`
- [ ] Hardcoded safety block in `agent.py` is intact and unmodified
- [ ] `ANTHROPIC_API_KEY` does not appear in any log output
- [ ] New conversation history injections use structured message objects, not raw string injection
- [ ] `docs/context.md` and `docs/principles.md` have `chmod 600` permissions
- [ ] `.env` is in `.gitignore` and was not staged for commit
- [ ] Any new `IBKRClient` usage goes through `ClaudeToolkit` (not direct calls in tool handlers)
