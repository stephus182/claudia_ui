"""Tests for claudia/flex_sync.py — session-start dataset validation and the
Drive-backup decision.

Two rules drive this module, both set by the user on 2026-08-05 after a live session
found the Drive backup silently stale:

1. **A pull that does not refresh the backup is incomplete.** The backup used to hang
   off panel_app's *startup* sync only, so a `sync_flex_trades` triggered any other way
   updated the local store and left `account_data/store.db` on Drive at its previous
   version, with nothing said.
2. **But do not add weight.** Flex is T+1 data — one pull per session is the whole
   refresh budget, and re-uploading 53 MB that did not change is pure cost. So the
   upload is conditional on the data actually moving, which is what `dataset_fingerprint`
   decides.

The validation exists because the opening line has always *claimed* "integrity
validated" without anything having checked. These tests pin the claim to real checks.
"""

import sqlite3

import pytest

from claudia.flex_sync import dataset_fingerprint, validate_dataset

# ── fixtures ──────────────────────────────────────────────────────────────────


def _make_db(path, *, trades=1, lots=True, wash=True) -> None:
    """A miniature but structurally faithful store.db.

    The realised identity below is IBKR's documented behaviour and the one invariant
    CLAUDE.md names outright: Trade == Lot + WashSale.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE flex_trade (
            execution_key TEXT, source TEXT, fifo_pnl_realized REAL, trade_date_iso TEXT
        );
        CREATE TABLE flex_lot (fifo_pnl_realized REAL);
        CREATE TABLE flex_wash_sale (fifo_pnl_realized REAL);
        CREATE TABLE trades (execution_id TEXT);
        """
    )
    for i in range(trades):
        conn.execute(
            "INSERT INTO flex_trade VALUES (?, 'flex', ?, ?)",
            (f"key-{i}", -100.0, "2026-08-04"),
        )
    if lots:
        conn.executemany(
            "INSERT INTO flex_lot VALUES (?)", [(-250.0,)] * trades
        )
    if wash:
        conn.executemany(
            "INSERT INTO flex_wash_sale VALUES (?)", [(150.0,)] * trades
        )
    conn.commit()
    conn.close()


@pytest.fixture
def good_db(tmp_path):
    path = tmp_path / "store.db"
    _make_db(path)
    return str(path)


# ── validate_dataset ──────────────────────────────────────────────────────────


def test_a_sound_dataset_passes_every_check(good_db):
    validity = validate_dataset(good_db)
    assert validity.ok is True
    assert validity.empty is False
    assert validity.failures == ()
    assert len(validity.checks) >= 4


def test_duplicate_execution_keys_fail(good_db):
    """The exact defect that produced 75 duplicate rows before the merge fix."""
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-0', 'flex', -100.0, '2026-08-04')")
    conn.commit()
    conn.close()

    validity = validate_dataset(good_db)
    assert validity.ok is False
    assert any("execution_key" in f.name for f in validity.failures)


def test_null_execution_key_fails(good_db):
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES (NULL, 'flex', -100.0, '2026-08-04')")
    conn.commit()
    conn.close()

    validity = validate_dataset(good_db)
    assert validity.ok is False


def test_realised_identity_breaking_fails(good_db):
    """Trade == Lot + WashSale. Summing flex_lot instead of flex_trade is the trap
    CLAUDE.md warns about; if the relationship breaks, the dataset is not usable for
    the realised windows the dashboard reports."""
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_lot VALUES (-9999.0)")
    conn.commit()
    conn.close()

    validity = validate_dataset(good_db)
    assert validity.ok is False
    assert any("realised" in f.name.lower() for f in validity.failures)


def test_an_empty_dataset_is_not_a_corrupt_one(tmp_path):
    """A fresh install has no trades. That must read as 'nothing to validate', not as
    a failure — otherwise every first run opens with a false integrity alarm."""
    path = tmp_path / "store.db"
    _make_db(path, trades=0, lots=False, wash=False)

    validity = validate_dataset(str(path))
    assert validity.empty is True
    assert validity.ok is True
    assert validity.failures == ()


def test_a_database_without_flex_tables_is_empty_not_broken(tmp_path):
    path = tmp_path / "store.db"
    sqlite3.connect(path).close()

    validity = validate_dataset(str(path))
    assert validity.empty is True
    assert validity.ok is True


def test_a_missing_file_reports_honestly_instead_of_raising(tmp_path):
    validity = validate_dataset(str(tmp_path / "nope.db"))
    assert validity.ok is False
    assert validity.failures  # says what happened rather than crashing startup


def test_a_damaged_file_fails_validation(tmp_path):
    """A damaged file must never pass — this is the risk the WAL-safe snapshot upload
    exists to prevent, so the check that would notice it has to be real.

    The dataset is grown past a single page first: a scribble into a small database's
    free space leaves `PRAGMA integrity_check` reporting `ok`, which made an earlier
    version of this test pass vacuously.

    Which branch catches it is deliberately not asserted, because it is not stable:
    this scribble makes SQLite *raise* `DatabaseError: database disk image is malformed`
    while running the pragma, rather than return a non-`ok` string from it. Both paths
    lead to the same verdict, and both are covered — asserting the mechanism here would
    pin behaviour that belongs to SQLite.
    """
    path = tmp_path / "store.db"
    _make_db(path, trades=2000)
    assert path.stat().st_size > 4096 * 8  # enough pages that the write lands on data
    with open(path, "r+b") as fh:
        fh.seek(4096 * 4)
        fh.write(b"\xde\xad\xbe\xef" * 1024)

    validity = validate_dataset(str(path))
    assert validity.ok is False
    assert validity.empty is False  # damaged is not "nothing here yet"


def test_summary_line_names_the_first_failure(good_db):
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-0', 'flex', -100.0, '2026-08-04')")
    conn.commit()
    conn.close()

    line = validate_dataset(good_db).summary
    assert "execution_key" in line


# ── dataset_fingerprint — the "did this pull change anything" decision ─────────


def test_fingerprint_is_stable_when_nothing_changes(good_db):
    assert dataset_fingerprint(good_db) == dataset_fingerprint(good_db)


def test_fingerprint_moves_when_a_trade_lands(good_db):
    before = dataset_fingerprint(good_db)
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-new', 'flex', -1.0, '2026-08-05')")
    conn.commit()
    conn.close()

    assert dataset_fingerprint(good_db) != before


def test_fingerprint_moves_when_a_live_row_is_merged_into_flex(good_db):
    """The 2026-08-05 case: row COUNT was unchanged (1,110 before and after) because
    the Flex statement merged onto the nine live rows via execution_key. A count-only
    fingerprint would have called that 'no change' and skipped the backup, losing the
    settled statement figures. The source mix has to be part of the fingerprint."""
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-live', 'live', -1.0, '2026-08-05')")
    conn.commit()
    conn.close()
    before = dataset_fingerprint(good_db)

    conn = sqlite3.connect(good_db)
    conn.execute("UPDATE flex_trade SET source='flex' WHERE execution_key='key-live'")
    conn.commit()
    conn.close()

    assert dataset_fingerprint(good_db) != before


def test_fingerprint_of_a_missing_file_is_not_an_error(tmp_path):
    """Used to gate an upload; it must never be the thing that breaks startup."""
    assert dataset_fingerprint(str(tmp_path / "nope.db")) is None


# ── "already updated today — do not check again" (user rule, 2026-08-05) ──────
#
# Flex is T+1 and the store is pulled once a day. Re-running the checks on a byte-identical
# dataset every time a session starts is work that cannot produce a new answer, and it puts
# an unqualified "integrity validated" on screen with no indication of *when* anything was
# actually established. So the verdict is cached with the fingerprint it was computed
# against, and reused for the rest of the day — the UI then states the time it was proven
# rather than implying it happened just now.

from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from claudia.flex_sync import last_import, validate_dataset_daily  # noqa: E402

_NOW = datetime(2026, 8, 5, 14, 30, tzinfo=UTC)


def _import_log(path, when: datetime, filename: str = "flex_U1675699_2026-08-05.xml") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS flex_import_log ("
        "id INTEGER PRIMARY KEY, filename TEXT, sha256 TEXT, trade_id_count INTEGER,"
        "raw_trade_count INTEGER, source TEXT, imported_at TEXT, verified_at TEXT)"
    )
    conn.execute(
        "INSERT INTO flex_import_log (filename, trade_id_count, raw_trade_count, source,"
        " imported_at, verified_at) VALUES (?, 105, 105, 'auto', ?, ?)",
        (filename, when.isoformat(), when.isoformat()),
    )
    conn.commit()
    conn.close()


def test_the_first_run_of_the_day_actually_validates(good_db):
    outcome = validate_dataset_daily(good_db, now=_NOW)
    assert outcome.reused is False
    assert outcome.validity.ok is True
    assert outcome.validated_at == _NOW


def test_the_second_run_reuses_the_verdict_without_rechecking(good_db):
    validate_dataset_daily(good_db, now=_NOW)
    with patch("claudia.flex_sync.validate_dataset") as never:
        outcome = validate_dataset_daily(good_db, now=_NOW + timedelta(hours=2))
    never.assert_not_called()          # the whole point: no work, not merely a fast path
    assert outcome.reused is True
    assert outcome.validity.ok is True
    assert outcome.validated_at == _NOW  # reports when it was PROVEN, not when it was asked


def test_a_changed_dataset_is_revalidated_the_same_day(good_db):
    validate_dataset_daily(good_db, now=_NOW)
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-new', 'flex', -1.0, '2026-08-05')")
    conn.commit()
    conn.close()

    outcome = validate_dataset_daily(good_db, now=_NOW + timedelta(minutes=1))
    assert outcome.reused is False  # a pull landed; the old verdict no longer describes it


def test_yesterdays_verdict_is_not_reused_today(good_db):
    validate_dataset_daily(good_db, now=_NOW - timedelta(days=1))
    outcome = validate_dataset_daily(good_db, now=_NOW)
    assert outcome.reused is False


def test_a_failed_verdict_is_never_reused(good_db):
    conn = sqlite3.connect(good_db)
    conn.execute("INSERT INTO flex_trade VALUES ('key-0', 'flex', -100.0, '2026-08-04')")
    conn.commit()
    conn.close()

    first = validate_dataset_daily(good_db, now=_NOW)
    assert first.validity.ok is False
    second = validate_dataset_daily(good_db, now=_NOW + timedelta(minutes=1))
    assert second.reused is False  # a failure must be re-measured, never cached forward


def test_an_unwritable_location_still_validates(good_db):
    """Caching is an optimisation. Losing it must cost speed, never the check."""
    with patch("claudia.flex_sync.Path.write_text", side_effect=OSError("read-only fs")):
        outcome = validate_dataset_daily(good_db, now=_NOW)
    assert outcome.validity.ok is True
    assert outcome.reused is False


def test_a_corrupt_cache_file_is_ignored_rather_than_trusted(good_db):
    validate_dataset_daily(good_db, now=_NOW)
    Path(f"{good_db}.validation.json").write_text("{not json")

    outcome = validate_dataset_daily(good_db, now=_NOW + timedelta(minutes=1))
    assert outcome.reused is False
    assert outcome.validity.ok is True


# ── last_import — when the store was actually updated ─────────────────────────


def test_last_import_reports_the_most_recent_pull(good_db):
    _import_log(good_db, datetime(2026, 8, 4, 17, 59, tzinfo=UTC), "older.xml")
    _import_log(good_db, datetime(2026, 8, 5, 12, 18, 1, tzinfo=UTC), "newest.xml")

    record = last_import(good_db)
    assert record is not None
    assert record.filename == "newest.xml"
    assert record.at == datetime(2026, 8, 5, 12, 18, 1, tzinfo=UTC)


def test_last_import_is_none_before_anything_was_ever_imported(good_db):
    assert last_import(good_db) is None


def test_last_import_survives_an_unparseable_timestamp(good_db):
    conn = sqlite3.connect(good_db)
    conn.execute(
        "CREATE TABLE flex_import_log (id INTEGER PRIMARY KEY, filename TEXT, sha256 TEXT,"
        " trade_id_count INTEGER, raw_trade_count INTEGER, source TEXT, imported_at TEXT,"
        " verified_at TEXT)"
    )
    conn.execute("INSERT INTO flex_import_log (filename, imported_at) VALUES ('x.xml', 'never')")
    conn.commit()
    conn.close()

    assert last_import(good_db) is None  # no time is better than a wrong time on screen


def test_an_unreadable_path_writes_no_sidecar_at_all(tmp_path, monkeypatch):
    """Regression, 2026-08-05: it used to write one anyway.

    `_write_record` fired even when the dataset could not be fingerprinted, so any caller
    with an unusable path left a file behind — and a unit test passing a `MagicMock`
    config wrote `<MagicMock name='mock._config.sqlite_path' id=…>.validation.json` into
    the repository root. Fourteen reached a commit. Such a record can never be reused
    (reuse requires a fingerprint match), so writing it was cost with no benefit.
    """
    monkeypatch.chdir(tmp_path)
    outcome = validate_dataset_daily(str(tmp_path / "does-not-exist.db"), now=_NOW)

    assert outcome.validity.ok is False   # still reports honestly
    assert outcome.reused is False
    assert list(tmp_path.glob("*.validation.json")) == []
    assert list(tmp_path.iterdir()) == []  # nothing dropped anywhere


def test_a_mock_shaped_path_leaves_the_working_directory_clean(tmp_path, monkeypatch):
    """The exact shape that littered the repo: a path that is not a path."""
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    validate_dataset_daily(MagicMock(), now=_NOW)

    assert list(tmp_path.iterdir()) == []
