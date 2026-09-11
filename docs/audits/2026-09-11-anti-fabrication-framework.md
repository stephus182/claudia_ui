# 2026-09-11 — Narrated tool cycles: mechanism, measurement, and what shipped

Point-in-time record of gap #51 (`docs/project-status.md`) and the framework built for it.
The design, the eval grid and the plan are git-ignored working documents in `docs/plans/`
(`2026-09-11-anti-fabrication-framework-design.md`, `…-plan.md`); the durable reference is
`docs/agent-behavior-reference.md` §4d. Numbers here are the ones that decided what shipped.

## Symptom

Five firings of the claim detectors in two days (2026-09-10 22:09; 2026-09-11 11:30, 11:54,
11:54, plus one on a re-ask): ClaudIA wrote a complete tool cycle — *"I need the current field
set for order 2030859591 before staging the modify.Order confirmed live and editable
(`order_not_editable: false`). Staging the modify … Modify is staged as a button above"* —
with no `get_order_status` call, no `propose_modify` call, and zero thinking tokens. The
guard held every time (no button, nothing staged, IBKR untouched); the cost was friction and
trust, and a fabricated tool result riding inside the claim.

## Mechanism (measured, not inferred)

- `agent.py` joined content blocks with **no separator**, so a block boundary printed as
  `staging.Confirmed` on screen and in the store. Replayed as history, every prior tool turn
  read "announce → report" with **no `tool_use` between** — and the model copied that shape,
  glue included, inside single text blocks.
- Corpus proxy over the whole store (`scripts/…` in the scratchpad, recorded in the design
  doc): a glued sentence boundary in a zero-tool assistant turn catches **23 of 23** audited
  fabrications (`tests/test_corpus_precision.py`), including the two documented misses of the
  prose detectors and the payload case; every hit followed at least one glued tool turn in
  its own session; none occurred in a session without prior tool use.
- Once a narrated action was in the history it was **copied on the re-ask** (1075 → 1078
  near-verbatim, with the operator note answered *inside* the copy and still no call).
- Prose was in context when it happened: the safety block, the tool description, the ⚠️
  notice and the `role: system` operator note. Anthropic's Claude API reference names the
  class: *"the model occasionally writes a tool call into its visible text instead of a
  `tool_use` block: the turn succeeds, the call never runs, no error is raised, and in an
  agentic loop that text pollutes later turns."*

## Measurement — `scripts/replay_eval.py`

The exact request ClaudIA sent at a stored user turn (system blocks, tool declarations,
history window, operator message; a backup-API copy of the store cut at the turn), sampled
n = 16 per cell; no tool executes. Turns: 1074 (plain modify), 1077 (re-ask after a
fabrication), 1058 (re-ask after a Gate 2 refusal).

| request shape | narrated (of 48) |
| --- | --- |
| today's replay | **17** — 2/16, **14/16**, 1/16 |
| paragraph break at the block boundary only | 0 of 32 (1074, 1077) |
| contradicted turn withdrawn only | 3 of 32 |
| separator + withdrawal | 0 |
| faithful tool cycles (real `tool_use` + stub results) | 0 |
| both, and both + `xhigh` effort | 0 |
| retry shape: operator note + `tool_choice: any` | 0 — and **0 thinking tokens on all 48** |

Live probes on `claude-opus-4-8` (`tests/test_agent.py`, opt-in `live_api`): a prior turn
replayed as a synthetic-id `tool_use` with no thinking block is accepted (plain, cache-marked,
tool undeclared); `tool_choice: any` under adaptive thinking is accepted with a real `tool_use`
back; `claude-fable-5-1` returns the documented 400 verbatim.

## What shipped (claudia_ui `7eb5fbc` … `d23c56c`)

1. Text blocks joined with a paragraph break — within a response and across passes.
2. `message_withdrawals`: a row a detector contradicted is not replayed as the model's
   words; kept in the store, the report and the chat. A side table — `messages` stays
   append-only.
3. Same-turn retry: a zero-tool turn whose text claims a tool result or an action is
   persisted, recorded, withdrawn and retried **before display**, with the operator note
   delivered and `tool_choice: any` on the first request where the model is probed for it
   (`_FORCED_TOOL_CHOICE_MODELS`, `auto` elsewhere); one retry, then the ⚠️ notice; a
   System-log line is the visible trace. A turn that ran any tool keeps the correction path.
4. The fifth replay record: refused / rejected / dispatched-unverified clicks, named as the
   user's decision.
5. `propose_modify` refused unless `get_order_status` ran in the same turn.
6. Per-pass `response shape` log line (measurement); the four detectors as one pure
   decision and one table (no behaviour change).

Not shipped: `xhigh` effort (no measurable gain), a block-count detector (a fabrication was
measured inside one block), more prompt text. Faithful tool-cycle replay: gated behind the
harness at larger n, since it cannot show a benefit over the separator at this resolution.

## Sources

- https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use (forced
  tool use per model; strict tool use)
- https://platform.claude.com/docs/en/api/errors (the 400 text)
- https://platform.claude.com/docs/en/build-with-claude/thinking (thinking blocks across
  turns), …/adaptive-thinking, …/thinking-steering-and-cost, …/effort
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8
- https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations
- https://www.anthropic.com/engineering/building-effective-agents,
  https://www.anthropic.com/engineering/writing-tools-for-agents
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching (mid-conversation
  system messages; placement probed 2026-07-27)
