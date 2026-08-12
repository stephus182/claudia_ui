"""Detector precision against the real conversation corpus.

The four claim detectors in `claudia.agent` were each shipped only after a measurement
against the live store — "precision is a measurement, not an argument". This module
freezes the 2026-08-12 measurement of the two newest ones as a regression test, so a
regex edit that widens either detector cannot pass the suite silently.

The corpus is `data/claudia.db` — git-ignored, holds real account data, and exists only
on the development machine, so everything here skips on a fresh clone
(`feedback-public-repo-account-data-rule`). Only message *ids* appear in this file:
row ordinals, not account data.

The window is frozen at message id <= 768 (the store's last row when the measurement
was made). Rows are append-only, so the assertions below are about immutable data; new
sessions extend the corpus without invalidating them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parent.parent / "data" / "claudia.db"
_FROZEN_LAST_ID = 768

# Every zero-tool turn in the frozen window whose text reports a completed action —
# individually read and verified as a fabrication during the 2026-08-12 audit (each
# reports tool-sourced data in a turn with no tool row, in a session that records tool
# rows normally elsewhere). The two known misses, ids 345 and 475, are the detector's
# documented give-ups and are asserted as misses below so a widening that "fixes" them
# is forced to re-measure rather than land silently.
_VERIFIED_ACTION_FIRES = frozenset({
    184, 192, 194, 196, 211, 258, 306, 308, 310, 349, 351, 368, 378, 387,
    562, 720, 722, 748, 750, 752,
})
_DOCUMENTED_MISSES = frozenset({345, 475})
_VERIFIED_RESULT_FIRES = frozenset({380})

pytestmark = pytest.mark.skipif(
    not _DB.exists(), reason="local conversation corpus not present (fresh clone)"
)


def _frozen_turns() -> list[tuple[int, str, int]]:
    """(message_id, assistant_text, tools_run_that_turn) for the frozen window."""
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE id <= ? ORDER BY id",
            (_FROZEN_LAST_ID,),
        ).fetchall()
    finally:
        conn.close()
    turns, tools = [], 0
    for mid, role, content in rows:
        if role == "user":
            tools = 0
        elif role == "tool":
            tools += 1
        elif role == "assistant":
            turns.append((mid, content or "", tools))
            tools = 0
    return turns


def test_action_detector_precision_is_frozen():
    """Exactly the verified fabrications fire; everything else stays silent.

    Both directions are load-bearing. A new id in `fired` is a false positive — the
    detector got wider. A verified id missing from `fired` is a lost catch — the
    detector got narrower. Either way the regexes changed behaviour against the corpus
    they were measured on, and the change must be re-measured, not slipped in.
    """
    from claudia.agent import _claims_completed_action

    fired = {
        mid for mid, text, tools in _frozen_turns()
        if tools == 0 and _claims_completed_action(text) is not None
    }
    assert fired == _VERIFIED_ACTION_FIRES
    assert not (fired & _DOCUMENTED_MISSES)


def test_result_detector_precision_is_frozen():
    """The fabricated-payload detector fires on the one measured instance and no other."""
    from claudia.agent import _claims_verbatim_tool_result

    fired = {
        mid for mid, text, tools in _frozen_turns()
        if tools == 0 and _claims_verbatim_tool_result(text) is not None
    }
    assert fired == _VERIFIED_RESULT_FIRES


def test_evidence_cleared_turns_stay_cleared():
    """Turns whose text matches but whose tools really ran must never fire at the call
    site — the verdict is `called_tools`, and these are the corpus's proof that the
    same sentence is honest when the tool ran (msg 746 is the controlled pair of a
    firing instance: near-identical text, tools called, cleared)."""
    from claudia.agent import _claims_completed_action

    matched_with_tools = {
        mid for mid, text, tools in _frozen_turns()
        if tools > 0 and _claims_completed_action(text) is not None
    }
    assert 746 in matched_with_tools
    # The call-site verdict clears every one of these; the assertion here is that the
    # set is non-trivial — the discriminator is doing real work, not matching nothing.
    assert len(matched_with_tools) >= 20
