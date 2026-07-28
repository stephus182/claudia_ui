"""Verbatim assistant text from the two real failure sessions.

Order ids are SYNTHETIC — the real ones are account data and must not enter git
(feedback-git-hygiene-standing-rule). Prose is unaltered; it is the evidence.

Forensics on those sessions proved the model emitted a valid, parsed proposal in 5 of 5
inspectable failures: the render path discarded it between persisting the reply text and
writing the decision row. So these strings are not examples of a model making something
up — they are what an honest model sounds like when the layer beneath it lied to it.
"""

# 2026-07-17 — block emitted and parsed, no button. The four consecutive newlines are
# where the stripped proposal block used to sit; they are the forensic signature.
FAILED_437 = (
    "Here's a fresh proposal for the AAPL test order — same parameters: 1 share, "
    "limit $250, GTC.\n\n\n\nReview and confirm with Touch ID to stage. If the button "
    "still doesn't render, let me know and we'll troubleshoot."
)

# 2026-07-24 — the defended claim. Nothing had ever told the model its render failed,
# so on the next turn it defended a button that did not exist.
DEFENDED_CLAIM_588 = (
    "The order-modify-proposal is already staged in my earlier message — it's sitting "
    "in front of you."
)

# Innocent look-alikes: staging vocabulary, no proposal. Must never trip the guardrail.
INNOCENT = [
    "No open orders — nothing working, staged, or partially filled.",
    "I can't verify that order, because it was never actually placed. What happened is "
    "that I output an order proposal — a staged block that renders as a confirmation "
    "button on your end.",
    "Test complete: staged → confirmed working → cancelled → verified removed.",
]


# ── Gap #3: a narrated staging action nothing backs ──────────────────────────
#
# Synthetic shape fixtures, not transcript extracts: the live store holds account data
# and none of its text is copied here (feedback-git-hygiene-standing-rule). Every symbol
# and order id below is invented. What is preserved is the *grammar* of each claim, which
# is the only thing `_claims_completed_proposal` reads.

# The failure mode itself: a completed-action claim with no proposal tool call behind it.
NARRATED_STAGING = [
    "Cancel staged — button's above for **order 9000001 only** (BUY 1 ZZZU6 @ 100 GTC).",
    "Cancel button is now live above.",
    "I've staged the order — the button is above.",
    "Staged — the button's above.",
    "Re-staged — new button's above.",
    "Done — re-staged with your new value.",
    "Proposed — button below.",
]

# The trap. Every one of these carries staging vocabulary with nothing pending, and the
# first two are ClaudIA correctly owning the failure — a guardrail that fires on an honest
# self-correction is worse than no guardrail, because it teaches the user to ignore it.
HONEST_STAGING_TALK = [
    "I described staging a cancel without actually calling the tool. That description "
    "produced **no button**.",
    "That description produced **no button** — the only real cancel proposal is item 3 "
    "above.",
    "Nothing cancels until you click it and clear both confirmation gates.",
    "Both are ClaudIA-staged, so this one I can cancel for you.",
    "**Staged button ≠ live order.** Nothing to pull from the book.",
    "If you simply don't want the ZZZ trade, just don't click the button — it does "
    "nothing on its own.",
    "Want me to stage the cancel on the ZZZ test order now?",
    "Review and confirm with Touch ID to stage.",
    "Both were staged through me, so I can help you cancel one.",
    "I emitted two propose_order calls for ZZZ this session; each rendered as its own "
    "staging button.",
    "A staging button is just a proposal — nothing reaches IBKR until you click it and "
    "clear Touch ID + the confirmation dialog.",
    "The second replaces the first *by intent*, but both buttons physically exist in the "
    "thread — only click the 100 one.",
    "No button was produced above.",
    "I will stage the cancel once you confirm the level.",
    "| 9000001 | ZZZ | BUY | 1 | LMT | 100.00 | GTC | Submitted | ClaudIA-staged |",
]


# ── Gap #3, freshness half: a narrated lookup nothing backs ──────────────────
#
# The same 2026-07-28 message that narrated the staging above also opened by claiming a
# check of the live book, in a turn with no tool call of any kind. Only the grammar is
# preserved; the symbols and ids are invented.

# Must fire when no order-book tool ran in the turn.
NARRATED_BOOK_CHECK = [
    "Confirmed against the live book — both ZZZ orders are working:",
    "I checked your live orders and there is nothing resting.",
    "I've verified the order status — it is still working.",
    "Verified the book: the order is live.",
    "I just pulled the live orders, and ZZZ is there.",
    "Re-checked the working orders — unchanged.",
    "I have confirmed the open orders.",
    "Checked the order book — one ZZZ order at 100.",
]

# The trap. Offers, intentions, denials, recaps of earlier turns, and the adjectival
# reading of the same verbs. None of these claims a lookup happened in this turn.
HONEST_BOOK_TALK = [
    "Want me to check the live orders?",
    "Should I pull the book first?",
    "Let me check the live orders before I answer.",
    "I'll check the live book now.",
    "I haven't checked the live book yet.",
    "I never checked the order book.",
    "I described checking the live book, but no tool ran.",
    "That was written, not confirmed against the book.",
    "Earlier I confirmed the live orders.",
    "When I pulled the book one turn ago, only ZZZ was working.",
    "I already checked the live orders above.",
    "A staged button is not a confirmed live order.",
    "A confirmed live order requires a click first.",
    "The confirmed open orders column shows two rows.",
    "I checked your positions and the P&L.",
    "I pulled the account summary.",
]
