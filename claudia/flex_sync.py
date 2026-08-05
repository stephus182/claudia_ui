"""Session-start Flex dataset validation, and the decision to refresh the Drive backup.

Pure SQLite — no network, no IBKR, no Panel. `panel_app` owns the orchestration and the
chat surface; this module owns the two questions that orchestration has to answer:

* **Is the dataset sound?** (`validate_dataset`) — run at every session start, whether or
  not a pull happened.
* **Did this pull change anything?** (`dataset_fingerprint`) — the gate on a 53 MB Drive
  upload.

## Why this module exists (2026-08-05 live session)

The `store.db` Drive backup used to hang off `panel_app`'s *startup* sync alone. A
`sync_flex_trades` triggered any other way — ClaudIA calling the tool mid-session, a
script, the MCP server — updated the local store and left `account_data/store.db` at its
previous version, silently. Found live: the 08-05 statement imported locally at 12:18Z
while the Drive copy still read 2026-08-04T17:47Z.

The user's rule, and the reason the fix is a *decision* rather than an unconditional
upload: Flex is T+1 data, so **one pull per session is the whole refresh budget**, and
re-uploading 53 MB that did not change is cost with no benefit. Hence the fingerprint.

## Why validation runs even when no pull happens

The opening line has always read "…, integrity validated" and the system prompt has
always told the model "Dataset is complete and verified — no missing imports". Nothing
checked. `get_trade_date_coverage` — the only thing consulted — is an *activity report*
and says so in its own docstring; it counts trades and finds date gaps, which is not an
integrity check by any reading. So the claim was decoration on a number.

These checks make it true. **0.19s measured end to end** on the real 53 MB store.db —
that is the whole function, not the pragma alone (the pragma is 0.07-0.13s of it):

| Check | Catches |
|---|---|
| `PRAGMA integrity_check` | a torn or truncated file — the exact risk the WAL-safe snapshot upload exists to prevent |
| `execution_key` unique | the duplicate-row defect that once produced 75 phantom trades |
| `execution_key` non-null | rows that can never merge with a later statement, so they duplicate on the next pull |
| Trade == Lot + WashSale | the realised-P&L identity CLAUDE.md names outright, and the one that decides whether `flex_trade` may be reported as realised P&L at all |

The identity is IBKR's documented behaviour ("For wash sales, the Realized P/L column
will contain the net realized amount, including loss disallowed" —
https://www.ibkrguides.com/reportingreference/reportguide/trades_realizedsummary.htm),
verified exact against 20 of 20 archived statements. It is deliberately the same check as
`scripts/audit_flex_dataset.py`'s #17 in ibkr_core_mcp, so the cheap startup gate and the
full 42-check gate cannot disagree about what "valid" means.

What this does NOT do, and will not: reconcile against the source XML. That is
`verify_flex_import`'s job and it needs the statement corpus; a session start is the
wrong place for it.
"""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Cent-denominated sums do not survive binary float exactly, so the identity is compared
# to the cent rather than to zero — the same allowance audit_flex_dataset.py #17 uses.
_PENNY = 0.01


@dataclass(frozen=True)
class DatasetCheck:
    """One named check and what it measured. `detail` is shown to the user on failure."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class DatasetValidity:
    """The verdict for one session start.

    `empty` is not a pass and not a failure — it is "there is nothing here yet", which is
    what a fresh install looks like. Callers must say nothing rather than either claiming
    validation or raising an alarm.
    """

    checks: tuple[DatasetCheck, ...]
    empty: bool = False

    @property
    def failures(self) -> tuple[DatasetCheck, ...]:
        """Only the checks that failed, in the order they ran."""
        return tuple(c for c in self.checks if not c.passed)

    @property
    def ok(self) -> bool:
        """True when nothing failed.

        Note that an `empty` verdict is `ok` — it carries no checks, so there is nothing
        to have failed. Callers that need to distinguish "passed" from "nothing to check"
        must read `empty`; `summary` already words the two differently.
        """
        return not self.failures

    @property
    def summary(self) -> str:
        """One line for a log or a chat notice — names the first failure, not a count."""
        if self.empty:
            return "no trade dataset yet — nothing to validate"
        if self.ok:
            return f"dataset validated ({len(self.checks)} checks)"
        first = self.failures[0]
        more = f" (+{len(self.failures) - 1} more)" if len(self.failures) > 1 else ""
        return f"{first.name}: {first.detail}{more}"


def _has_flex_tables(conn: sqlite3.Connection) -> bool:
    """Whether the three tables the checks read are present.

    Their absence means the Flex dataset was never built — a fresh install, not damage.
    All three are required because the realised identity spans them; checking only
    `flex_trade` would let a half-built schema look validatable.
    """
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"flex_trade", "flex_lot", "flex_wash_sale"} <= names


def validate_dataset(sqlite_path: str | Path) -> DatasetValidity:
    """Validate the local Flex dataset. Never raises — a failure to check is a failure.

    Read-only (`mode=ro`): this runs while ClaudIA and the dashboard poller hold the same
    database open, and validation must not be able to write, checkpoint or lock.
    """
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return DatasetValidity((DatasetCheck("dataset unreadable", False, str(exc)),))

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            # Stop here: every count below would be read off a file SQLite just called
            # damaged, and reporting those numbers would dress corruption as detail.
            return DatasetValidity(
                (DatasetCheck("file integrity", False, f"PRAGMA integrity_check: {integrity}"),)
            )

        if not _has_flex_tables(conn):
            return DatasetValidity((), empty=True)

        total = conn.execute("SELECT COUNT(*) FROM flex_trade").fetchone()[0]
        if total == 0:
            return DatasetValidity((), empty=True)

        dupes = conn.execute(
            "SELECT COUNT(*) FROM (SELECT execution_key FROM flex_trade "
            "GROUP BY execution_key HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        nulls = conn.execute(
            "SELECT COUNT(*) FROM flex_trade WHERE execution_key IS NULL"
        ).fetchone()[0]

        realised = conn.execute(
            "SELECT COALESCE(SUM(fifo_pnl_realized), 0) FROM flex_trade WHERE source='flex'"
        ).fetchone()[0]
        lots = conn.execute(
            "SELECT COALESCE(SUM(fifo_pnl_realized), 0) FROM flex_lot"
        ).fetchone()[0]
        wash = conn.execute(
            "SELECT COALESCE(SUM(fifo_pnl_realized), 0) FROM flex_wash_sale"
        ).fetchone()[0]
        identity_gap = round(abs(realised - (lots + wash)), 2)

        return DatasetValidity(
            (
                DatasetCheck("file integrity", True, "PRAGMA integrity_check: ok"),
                DatasetCheck(
                    "execution_key is unique", dupes == 0, f"{dupes:,} duplicated key(s)"
                ),
                DatasetCheck(
                    "execution_key is present", nulls == 0, f"{nulls:,} row(s) without a key"
                ),
                DatasetCheck(
                    "realised P&L identity (trades == lots + wash sale)",
                    identity_gap <= _PENNY,
                    f"{realised:,.2f} vs {lots + wash:,.2f} — off by {identity_gap:,.2f}",
                ),
            )
        )
    except sqlite3.DatabaseError as exc:
        # A malformed page can surface here rather than in integrity_check.
        return DatasetValidity((DatasetCheck("dataset unreadable", False, str(exc)),))
    finally:
        conn.close()


def dataset_fingerprint(sqlite_path: str | Path) -> tuple[int, int, str | None] | None:
    """A cheap signature of the dataset: (rows, Flex-sourced rows, newest trade date).

    Compared across a pull to decide whether the Drive backup needs refreshing. Returns
    None when it cannot be taken — the caller treats that as "unknown", and unknown must
    fall through to uploading rather than to skipping.

    Flex-sourced rows are counted separately for a reason measured live on 2026-08-05:
    the statement merged onto nine existing live-captured rows via `execution_key`, so
    the total was 1,110 before and 1,110 after. A count-only fingerprint would have read
    that as "nothing changed" and skipped the backup — losing exactly the settled
    statement figures the pull was for. The source mix moved even though the count did not.
    """
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        if not _has_flex_tables(conn):
            return None
        total = conn.execute("SELECT COUNT(*) FROM flex_trade").fetchone()[0]
        flex_rows = conn.execute(
            "SELECT COUNT(*) FROM flex_trade WHERE source='flex'"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(trade_date_iso) FROM flex_trade WHERE source='flex'"
        ).fetchone()[0]
        return (total, flex_rows, newest)
    except sqlite3.DatabaseError as exc:
        log.warning("dataset_fingerprint: %s", exc)
        return None
    finally:
        conn.close()
