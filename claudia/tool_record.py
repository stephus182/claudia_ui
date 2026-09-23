"""Running a real tool outside the agent loop, and leaving a record that it ran.

**Why this exists (gap #21).** `agent.py`'s tool loop writes a `tool` row for every call the
model makes. Code paths that run tools *without* going through `handle_message` — a button
click, a startup task — wrote nothing, so the call was invisible to the called-tool ledger
and to the store's forensics. The Pine Inject button was fixed in 2026-08-12 as a single
instance; this module is the class it belonged to, so the origin stamp is applied in one
place instead of being remembered at each call site.

**Why it matters for the fabrication work specifically.** The measured
*"Checking cache first, then fetching. Cache miss — … It worked"* family of fabrications is
about exactly the bar cache an unrecorded chart-pane fetch mutates. Without a row, the model
can later narrate a fetch it never made and neither the ledger nor the database can show
what actually ran.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ibkr_core_mcp import ClaudeToolkit

    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)


UI_BUTTON_ORIGIN = "ui_button"
"""`content` stamp marking a `tool` row the USER produced by clicking, not the model.

A model-initiated call writes `content=""` (agent.py's tool loop), so this one field
separates the two origins in the store. It is not decoration: the forensic rule of
`reference-proving-a-tool-call-never-happened` — *a `tool` row between a user row and an
assistant row is the model's evidence* — becomes false the moment a click can write one,
and a click can land mid-turn because the Pine buttons stay live while a turn is in
flight. Without the stamp a future corpus audit would credit a user's click to the model
and clear a genuine fabrication in that very turn. Read by the audit, never by the model:
the called-tool ledger is names-only, so this never reaches the prompt.
"""

STARTUP_ORIGIN = "startup"
"""`content` stamp for a `tool` row produced by session startup, not by a person or the model.

Distinct from `UI_BUTTON_ORIGIN` for the same forensic reason the button stamp exists: an
audit that cannot tell a background sync from a user action cannot reconstruct who caused
what. Startup calls also land *before* any user turn, so a row carrying no stamp at all
would sit at the head of the transcript looking like evidence for a turn that had not
happened yet.
"""


async def record_and_execute(
    toolkit: ClaudeToolkit,
    name: str,
    inputs: dict[str, Any],
    *,
    store: ConversationStore | None,
    session_id: str | None,
    origin: str,
) -> tuple[str, Any]:
    """Run a tool off the agent loop and persist a `tool` row for it. Transparent to callers.

    `toolkit.execute` is synchronous, so it runs on a worker thread — every existing
    out-of-loop caller already wrapped it in `asyncio.to_thread`, and that stays here rather
    than at each site.

    **Success and failure alike; the raw result is the record.** The row is written before
    the caller gets a chance to interpret the outcome, which is `panel_pinescript`'s rule
    kept intact: a failing sink must not cost the record. A store that raises is logged and
    swallowed — the caller still gets its result, because bookkeeping must never cost the
    action it is describing.

    **A raising tool is recorded and then re-raised.** An attempted call is a fact, and
    recording only the successes would hide the more interesting half. Callers keep the
    exception they already handle.

    With no `store` or no `session_id` — a startup path that runs before a session row
    exists — nothing is written and the tool still runs. Silence here is the absence of a
    place to write, not a decision to hide the call.
    """

    def _record(result: Any) -> None:
        """Persist one `tool` row, or do nothing if there is nowhere to write it."""
        if store is None or session_id is None:
            return
        try:
            store.add_message(
                session_id,
                "tool",
                content=origin,
                tool_name=name,
                tool_input=inputs,
                tool_result=result,
            )
        except Exception:
            log.exception("Could not persist the %s tool call %r", origin, name)

    try:
        result = await asyncio.to_thread(toolkit.execute, name, inputs)
    except Exception as exc:
        _record(f"{type(exc).__name__}: {exc}")
        raise
    _record(result)
    return result
