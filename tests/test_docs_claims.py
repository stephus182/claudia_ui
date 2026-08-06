"""Fail when a living document points at a repository path that does not exist.

## Why this exists

The 2026-08-05 documentation audit found three claims that were authoritative in tone,
checkable in seconds, and false:

1. `docs/startup-flow.md` promised that the Panel code "carries `# app.py:NNN` parity
   comments". It does not — ten parity comments, none with a line number.
2. `dashboard_data` called the ledger window "a calendar day" *and* recorded that the
   reset instant could not be observed in a single session. Both wrong; one watch settled
   it that evening.
3. A projected -252.60 figure was written as "Consequence measured". The measurement, when
   it arrived, read -2,810.47.

Only the **first shape** is machine-detectable: a document naming a repo artifact that is
not there. This test covers that shape and nothing else. **A green run here is not
evidence that the docs are accurate** — #2 and #3 are discipline, not tooling, and reading
this gate as coverage of the whole class would repeat the exact mistake it exists to
flag.

## Scope, and why it is drawn here

**Living docs only.** Dated files (`YYYY-MM-DD-*.md`) and `docs/plans/**` are point-in-time
records: `claudia/app.py` in a 2026-06-12 audit is an accurate statement about the
repository as it stood, and "fixing" it would rewrite history to look correct. That is the
convention `docs/README.md` states, and this test honours it rather than fighting it.

**Backticked paths only.** The backtick convention (CLAUDE.md → Pointers) means a literal
pointer rather than a `@import`; those are the paths a reader will actually try to open.

The audit's own checker flagged 18 paths of which only 4 were real defects. A gate that
failed on all 18 would be a gate nobody could keep green, so the exemptions below are
explicit and each one names why it is not a defect — see `_EXEMPT`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# A dated filename marks a point-in-time record, not a living claim.
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")

# Backticked repo-relative paths — the form a reader treats as "open this".
_BACKTICK_PATH = re.compile(
    r"`((?:docs|claudia|tests|scripts)/[A-Za-z0-9_./-]+\.(?:md|py|sh))`"
)

# Paths that are correct despite not existing. Each entry states why, because an
# unexplained exemption is how a real defect gets parked here later.
_EXEMPT = {
    # Illustrative example inside prose *about* the backtick convention itself.
    "docs/foo.md",
}

# `docs/plans/` is git-ignored by design: those paths are pointers into the local +
# Drive archive, not repo files (user rule 2026-07-24).
_EXEMPT_PREFIXES = ("docs/plans/",)


def _living_docs() -> list[Path]:
    """Tracked Markdown files that make *current* claims, from git rather than the disk.

    `git ls-files` deliberately, not `glob`: an untracked scratch file in `docs/` is not a
    claim this repository makes, and gating on it would fail builds for someone's local
    notes.
    """
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "docs", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [
        REPO / p
        for p in tracked
        if p.endswith(".md") and not _DATED.search(Path(p).name) and "/plans/" not in p
    ]


def _claimed_paths(doc: Path) -> set[str]:
    """Backticked repo paths in `doc`, minus the documented exemptions.

    Returns a set: the same path cited five times in one document is one claim, and
    reporting it five times would push a reader toward silencing the check.
    """
    return {
        target
        for target in _BACKTICK_PATH.findall(doc.read_text())
        if target not in _EXEMPT and not target.startswith(_EXEMPT_PREFIXES)
    }


def test_living_docs_are_discovered():
    """Guard the guard: an empty or tiny corpus would make every assertion below vacuous.

    If `git ls-files` changes shape or the dated-file filter over-matches, this test
    silently stops checking anything — a check weaker than the thing it checks is the
    failure mode recorded in `feedback-mocks-weaker-than-dependencies`.
    """
    docs = _living_docs()
    assert len(docs) >= 15, f"only {len(docs)} living docs discovered — filter too broad?"
    names = {d.name for d in docs}
    assert "startup-flow.md" in names
    assert "order-api-reference.md" in names


@pytest.mark.parametrize("doc", _living_docs(), ids=lambda d: d.relative_to(REPO).as_posix())
def test_living_doc_repo_paths_exist(doc: Path):
    """Every backticked repo path in a living doc must resolve to a real file.

    This is the check that would have caught `docs/startup-flow.md` pointing six phase
    sections at `claudia/app.py`, deleted at the Phase 11 cutover — the doc CLAUDE.md
    names for diagnosing startup failures, sending readers to a file that is not there.
    """
    missing = sorted(p for p in _claimed_paths(doc) if not (REPO / p).exists())
    assert not missing, (
        f"{doc.relative_to(REPO)} points at {len(missing)} path(s) that do not exist: "
        f"{missing}. Either fix the path, or — if the file was deliberately removed and "
        f"the sentence is about its removal — reword so it does not read as a pointer."
    )
