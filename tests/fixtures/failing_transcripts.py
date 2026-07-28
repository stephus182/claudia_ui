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
