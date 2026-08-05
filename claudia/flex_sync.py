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

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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


# ── "already updated today — do not check again" (user rule, 2026-08-05) ──────


@dataclass(frozen=True)
class LastImport:
    """When the store was last updated by a Flex pull, and by which statement."""

    at: datetime
    filename: str
    trade_count: int


@dataclass(frozen=True)
class ValidationOutcome:
    """A verdict plus **when it was established** — which is not always now.

    `reused` True means nothing was re-checked: a verdict from earlier today still
    describes this exact dataset. `validated_at` is the moment the checks actually ran,
    never the moment they were asked for, so the UI can state a time that is true.
    """

    validity: DatasetValidity
    validated_at: datetime
    reused: bool


def _record_path(sqlite_path: str | Path) -> Path:
    """Sidecar holding the last verdict. Named after the database it describes."""
    return Path(f"{sqlite_path}.validation.json")


def last_import(sqlite_path: str | Path) -> LastImport | None:
    """The most recent row of `flex_import_log`, or None if there is nothing to report.

    This is the honest answer to "when was the data last updated" — the moment a Flex
    statement was imported. It is **not** the newest trade date, which is what the
    opening line used to print under the label "last refreshed": on 2026-08-05 those
    were 08-05 12:18 UTC and 2026-08-04 respectively, a full day apart, because Flex is
    T+1. Showing the second and calling it the first tells the user the store is a day
    staler than it is.

    Returns None rather than a guess when the timestamp cannot be parsed: no time on
    screen is better than a wrong one.
    """
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT filename, imported_at, trade_id_count FROM flex_import_log "
            "ORDER BY imported_at DESC, id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None  # table absent on a store that has never imported
    finally:
        conn.close()

    if not row or not row[1]:
        return None
    try:
        at = datetime.fromisoformat(str(row[1]))
    except ValueError:
        log.warning("last_import: unparseable imported_at %r", row[1])
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return LastImport(at=at, filename=str(row[0]), trade_count=int(row[2] or 0))


def validate_dataset_daily(
    sqlite_path: str | Path, now: datetime | None = None
) -> ValidationOutcome:
    """Validate at most once per calendar day per dataset, and say when it was proven.

    The user's rule, 2026-08-05: *"If flex sync was performed and db already updated on
    T, do not check again. Just explicitly mention it was already updated: state the date
    and time."* Flex is T+1 and the store is pulled once a day, so re-running the checks
    on a byte-identical dataset cannot produce a new answer — it is work that buys
    nothing and, worse, prints an unqualified "integrity validated" with no indication of
    when anything was actually established.

    Reuse requires **both** conditions, and the fingerprint is the load-bearing one:

    * the stored verdict was reached **today**, and
    * `dataset_fingerprint` still matches what it was computed against.

    So a pull that lands mid-session re-validates immediately rather than coasting on a
    verdict about data that no longer exists. Two things are deliberately never reused: a
    **failure** (it must be re-measured, not cached forward into a day of silence) and a
    verdict from any earlier day.

    The cache is an optimisation and is treated as one — an unwritable location, a
    corrupt sidecar or a malformed record costs the reuse, never the check.
    """
    now = now or datetime.now(UTC)
    fingerprint = dataset_fingerprint(sqlite_path)

    reusable = _reusable_verdict(_read_record(_record_path(sqlite_path)), fingerprint, now)
    if reusable is not None:
        return reusable

    validity = validate_dataset(sqlite_path)
    _write_record(_record_path(sqlite_path), validity, fingerprint, now)
    return ValidationOutcome(validity=validity, validated_at=now, reused=False)


def _reusable_verdict(
    stored: object, fingerprint: tuple[int, int, str | None] | None, now: datetime
) -> ValidationOutcome | None:
    """A stored verdict that still describes this dataset today, or None.

    Every field is checked before it is believed. The sidecar is an ordinary JSON file on
    disk that a person can edit, truncate or copy between machines, so it is parsed as
    untrusted input: anything unexpected returns None and costs one re-validation, which
    is the cheap failure. The expensive failure would be trusting a record that says
    "valid" about a dataset it was not computed against.
    """
    if not isinstance(stored, dict) or stored.get("ok") is not True:
        return None
    if fingerprint is None or stored.get("fingerprint") != list(fingerprint):
        return None

    raw_at = stored.get("validated_at")
    if not isinstance(raw_at, str):
        return None
    try:
        validated_at = datetime.fromisoformat(raw_at)
    except ValueError:
        return None
    if validated_at.tzinfo is None:
        validated_at = validated_at.replace(tzinfo=UTC)
    # Local calendar day, not UTC: "already checked today" has to mean the reader's today.
    if validated_at.astimezone().date() != now.astimezone().date():
        return None

    raw_checks = stored.get("checks")
    checks = tuple(
        DatasetCheck(name=str(c["name"]), passed=True, detail=str(c.get("detail", "")))
        for c in (raw_checks if isinstance(raw_checks, list) else [])
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    )
    return ValidationOutcome(
        validity=DatasetValidity(checks, empty=bool(stored.get("empty"))),
        validated_at=validated_at,
        reused=True,
    )


def _read_record(path: Path) -> dict[str, object] | None:
    """The stored verdict, or None if there is not a usable one. Never raises."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write_record(
    path: Path,
    validity: DatasetValidity,
    fingerprint: tuple[int, int, str | None] | None,
    now: datetime,
) -> None:
    """Store the verdict beside the database. Never raises — see `validate_dataset_daily`.

    **Nothing is written without a fingerprint.** Such a record could never be reused —
    reuse requires a fingerprint match — so writing one is pure cost, and the cost turned
    out to be real: an unreadable path still produced a file, and a test passing a
    `MagicMock` config wrote `<MagicMock name='mock._config.sqlite_path' id=…>.validation.json`
    into the repository root. Fourteen of them reached a commit before a pre-push file
    listing caught it. A path we could not read is not a path we should write beside.
    """
    if fingerprint is None:
        return
    record = {
        "validated_at": now.isoformat(),
        "ok": validity.ok,
        "empty": validity.empty,
        "fingerprint": list(fingerprint) if fingerprint is not None else None,
        "checks": [{"name": c.name, "detail": c.detail} for c in validity.checks if c.passed],
        "summary": validity.summary,
    }
    try:
        path.write_text(json.dumps(record, indent=2))
        # 0600 for the same reason the session reports are (SECURITY.md §12): the record
        # summarises the account's trade dataset — row counts, the newest trade date, the
        # realised-P&L total the checks were satisfied by. `write_text` honours the umask
        # and does not touch an existing file's mode, so this is unconditional and after
        # every write. Found at 0644 on the live store, audit 2026-08-05.
        path.chmod(0o600)
    except OSError as exc:
        log.warning("Could not cache the dataset verdict (%s) — it will be re-checked", exc)
