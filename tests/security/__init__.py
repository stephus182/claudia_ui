"""Security architecture tests — the invariants CI must make impossible to violate silently.

One file per invariant of the ClaudIA Security Constitution
(`docs/audits/2026-09-13-security-architecture-audit.md` § 11). These are not ordinary unit
tests: each one exists because a future change could otherwise be perfectly linted, fully
typed and fully tested while breaking a property the whole system rests on.
"""
