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
