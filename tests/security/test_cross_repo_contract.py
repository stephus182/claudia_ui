"""CLA-SEC-012 — the boundary with ibkr_core_mcp is a security boundary, so it is pinned.

Audit 2026-09-13, findings C. The two packages divide one safety property between them:
ibkr_core_mcp owns the gates, the write endpoints and the capability registry; claudia_ui
owns the human click, the proposal lifecycle and every surface a person reads before
authorising. Neither repository's test suite reads the other's source, so the seam between
them is the one place where both can be green while the property is broken.

Three ways that happens, each with a test here:

1. **A symbol moves.** ClaudIA imports `price_text_safe` and `change_value_text` from
   `ibkr_core_mcp.order_confirm`, neither of which is in the core's `__all__`. A rename
   there leaves core CI green and breaks ClaudIA at import time, in a session. Since
   2026-09-14 the blocking lane resolves the core at the SHA in `core-ref.txt`, so this is
   caught by the informational forward-compatibility lane days before it is caught by an
   upgrade — which is the point of having two lanes rather than one floating `main`.

2. **An entry point changes shape.** ClaudIA calls exactly three gated methods, by name,
   with two keyword arguments. A signature change is a silent behaviour change at the one
   call site that reaches a live brokerage account.

3. **A documented count drifts.** ClaudIA's prose says how many tools the model can reach
   and what they are. The registry is the core's; every number here is computed from it, so
   a tool added there cannot leave a stale sentence here.

4. **A behaviour ClaudIA's own invariant rests on changes shape.** Two of them, added
   2026-09-14 after the core's OWASP recalibration, each pinned because a ClaudIA invariant
   would silently stop holding: the price formatter CLA-SEC-005 renders every human surface
   with, and the arity of the toolkit call the turn loop unpacks. Deliberately *not* pinned:
   the core's MCP transport bearer token (ClaudIA does not use that transport) and
   `redact_error` (ClaudIA has no call site for it — § 9 records that as a known limit, and
   pinning an API this repository does not use would be coupling for its own sake).

These assertions run against the *installed* core, so they check what this machine and CI
actually resolve rather than what a document claims.
"""

from __future__ import annotations

import inspect

import pytest

from tests.security.structural import package_sources, referenced_names

# What ClaudIA imports from the core, and from where. Every entry is a name this repository
# would fail to start without. Kept explicit rather than derived from the imports, so that
# adding one is a decision someone makes here and not a diff nobody reads.
IMPORTED_API: dict[str, tuple[str, ...]] = {
    "ibkr_core_mcp": (
        "ClaudeToolkit",
        "Config",
        "IBKRClient",
        "BrowserCookieAuth",
        "GDriveCache",
        "SQLiteStore",
    ),
    "ibkr_core_mcp.order_confirm": ("change_value_text", "price_text_safe"),
    "ibkr_core_mcp.streaming": ("IBKRWebSocket", "PnLUpdate", "TradeExecution"),
    "ibkr_core_mcp.gateway": ("GatewayManager",),
    "ibkr_core_mcp.auth": ("BrowserCookieAuth",),
}

# The three gated entry points, and the keyword arguments ClaudIA passes to each. The gates
# live inside these methods in the core; calling a different one, or the same one with a
# changed signature, is how an order write stops being gated the way this repository thinks.
GATED_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    "place_order_and_confirm": ("reply_log",),
    "modify_order_and_confirm": ("reply_log",),
    "cancel_order": ("order_details",),
}


@pytest.mark.parametrize("module_name", sorted(IMPORTED_API))
def test_every_imported_core_symbol_still_exists(module_name):
    """Import each one the way ClaudIA imports it, from the installed package."""
    import importlib

    module = importlib.import_module(module_name)
    missing = [name for name in IMPORTED_API[module_name] if not hasattr(module, name)]
    assert not missing, f"{module_name} no longer provides {missing}"


def test_the_three_gated_entry_points_keep_their_shape():
    """Name, presence, and the keyword arguments ClaudIA actually passes."""
    from ibkr_core_mcp import IBKRClient

    for method_name, keywords in GATED_ENTRY_POINTS.items():
        method = getattr(IBKRClient, method_name, None)
        assert method is not None, f"IBKRClient no longer has {method_name}"
        parameters = inspect.signature(method).parameters
        missing = [kw for kw in keywords if kw not in parameters]
        assert not missing, f"{method_name} no longer accepts {missing}"


def test_claudia_calls_no_gated_method_it_has_not_pinned():
    """The reverse direction: a fourth entry point added here must be pinned here too.

    Without this, `GATED_ENTRY_POINTS` documents the seam as of the day it was written and
    silently stops covering it — the failure mode the audit found in three other tests.
    """
    from ibkr_core_mcp import IBKRClient

    gated_names = {
        name
        for name in dir(IBKRClient)
        if name in {"place_order", "modify_order", "cancel_order", "reply_order"}
        or name.endswith("_and_confirm")
    }
    called = set()
    for _path, source in package_sources():
        called |= gated_names & referenced_names(source)
    assert called == set(GATED_ENTRY_POINTS), (
        f"ClaudIA calls {sorted(called)}; this test pins {sorted(GATED_ENTRY_POINTS)}"
    )


def test_the_capability_registry_is_the_source_of_the_tool_numbers():
    """Whatever ClaudIA says about the toolkit must be computed from the registry.

    The audit found `SECURITY.md` claiming "44 read-only ClaudeToolkit tools" while 20 of
    the 44 carried a capability other than READ_ONLY — four of them ungated IBKR account
    writes. The count was right and the adjective was false, and nothing could tell.
    """
    from ibkr_core_mcp.claude_tools import CAPABILITIES, TOOL_DEFINITIONS

    assert TOOL_DEFINITIONS, "the registry is empty"
    assert all(t.get("capabilities") for t in TOOL_DEFINITIONS), (
        "a tool declares no capability, so no honest sentence can be written about the set"
    )
    assert "ORDER_EXECUTION" not in CAPABILITIES, (
        "the forbidden capability gained a spelling — a tool could now declare it"
    )
    read_only = [str(t["name"]) for t in TOOL_DEFINITIONS if "READ_ONLY" in t["capabilities"]]
    assert len(read_only) < len(TOOL_DEFINITIONS), (
        "every tool now reads as read-only; re-check before writing that down anywhere"
    )


def test_claudia_exposes_the_whole_registry_to_the_model_and_knows_it():
    """ClaudIA hands the model every tool the registry defines, writers included.

    This is a deliberate position, not an oversight — the alert writers are useful and the
    order writers are not in the registry at all. It is recorded as a test so that the day
    it stops being deliberate, something says so.
    """
    from ibkr_core_mcp.claude_tools import TOOL_DEFINITIONS

    # `frozenset(...)`: the registry types `capabilities` as a bare Collection, so set
    # algebra on it is not statically valid even though every value is a frozenset.
    mutating = {
        str(t["name"])
        for t in TOOL_DEFINITIONS
        if frozenset(t["capabilities"])
        & {"ACCOUNT_STATE", "GOOGLE_DRIVE", "DATABASE", "SANDBOX_EXECUTION"}
    }
    assert mutating, "no tool mutates anything — the registry's meaning has changed"
    # `str(...)` for the same reason as the frozenset above: the registry's value type is a
    # union, so a name read out of it is not statically a `str`.
    account_writers = {
        str(t["name"]) for t in TOOL_DEFINITIONS if "ACCOUNT_STATE" in t["capabilities"]
    }
    assert account_writers == {
        "create_price_alert",
        "modify_price_alert",
        "delete_alert",
        "activate_alert",
    }, (
        "the set of ungated IBKR account writes the model can reach has changed: "
        f"{sorted(account_writers)}"
    )


# ── The supported core revision ──────────────────────────────────────────────────────────

# Set by the forward-compatibility CI lane, and by a developer who has overridden the PyPI
# copy with an editable checkout that has moved past the supported release. It disables one
# assertion — "the installed core is the supported release" — and nothing else: the import
# list, the gated entry points, the registry claims and the two pinned behaviours all still
# run, because those are exactly what that lane exists to check against an unreleased core.
UNPINNED_ENV_VAR = "CLAUDIA_CORE_UNPINNED"

CORE_DISTRIBUTION = "ibkr-core-mcp"


def _supported_core_ref() -> str:
    """The one release this repository is supported against, read from `core-ref.txt`."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "core-ref.txt"
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    values = [ln for ln in lines if ln and not ln.startswith("#")]
    assert len(values) == 1, f"core-ref.txt must name exactly one ref, found {values}"
    return values[0]


def _declared_core_specifier() -> str:
    """The version range `pyproject.toml` declares for the core dependency."""
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        metadata = tomllib.load(handle)
    for raw in metadata["project"]["dependencies"]:
        requirement = Requirement(raw)
        if canonicalize_name(requirement.name) == CORE_DISTRIBUTION:
            return str(requirement.specifier)
    raise AssertionError(
        f"pyproject.toml declares no {CORE_DISTRIBUTION} dependency — the core is resolved "
        "from PyPI since 2026-09-19 and must be a declared dependency, not a side install"
    )


def test_the_supported_core_revision_is_immutable_and_named_once():
    """CI resolves the core from this file; anything but an exact version is not a pin.

    Until 2026-09-14 both CI jobs checked out `ibkr_core_mcp` at floating `main`, so a green
    commit here was not reproducible and a push in the other repository could turn this one
    red with no commit in it. The fix was one file, and this test is what stops a later
    "just point it at main for now" from being invisible.

    Since 2026-09-19 the value is a **released version**, not a commit SHA, because the core
    is installed from PyPI and a version is what `pip` resolves. The immutability argument
    survives the change and is in fact stronger: PyPI refuses to re-upload a file for a
    version that already exists, where a git tag can be moved
    (https://docs.pypi.org/project-management/yanking-and-deleting/ — a release may be
    yanked or deleted, never silently replaced). A range, a floor or a pre-release is not a
    pin, so each is rejected here.
    """
    from packaging.version import InvalidVersion, Version

    ref = _supported_core_ref()
    try:
        version = Version(ref)
    except InvalidVersion:  # pragma: no cover - the assertion below reports it
        version = None
    assert version is not None, (
        f"core-ref.txt must hold one exact released version of {CORE_DISTRIBUTION}, not "
        f"{ref!r} — a branch, a commit SHA or a range is not what pip resolves"
    )
    assert not version.is_prerelease and not version.is_devrelease, (
        f"{ref!r} is a pre-release; the supported revision must be a final release"
    )
    assert str(version) == ref, (
        f"core-ref.txt holds {ref!r}, which normalises to {version!s} — write the normalised "
        "form, so a string comparison against the installed distribution cannot drift"
    )


def test_the_installed_core_is_the_supported_release():
    """The blocking lane must run against the version this repository claims support for.

    This is the assertion that replaces "checkout at the pinned SHA". The `test` job no
    longer checks the core out at all: it installs it from PyPI, and *this* is what proves
    the resolver landed on the supported release rather than on whatever the floor in
    `pyproject.toml` happened to admit that morning. Without it, a core release published
    between two pushes here would silently become the tested core.

    Skipped — this assertion only, never the contract checks above — when the installed core
    is deliberately not the supported release: the `forward-compat` lane, and a developer
    working on both repositories at once through the editable override. Both set
    `CLAUDIA_CORE_UNPINNED`. Note that an editable core *satisfies* this assertion for as long
    as the checkout's declared version equals the pin (both 2.0.1 on 2026-09-19); what the
    variable buys is that neither the lane nor the developer goes red on the day the core
    bumps its version, which is not a fact about this repository.
    """
    import os
    from importlib.metadata import version

    from packaging.version import Version

    if os.environ.get(UNPINNED_ENV_VAR):
        import pytest

        pytest.skip(f"{UNPINNED_ENV_VAR} is set: the installed core is deliberately unpinned")

    installed = version(CORE_DISTRIBUTION)
    supported = _supported_core_ref()
    # Compared as PEP 440 versions, not as strings: the pinned side is held in normalised
    # form by the test above, but the installed side is whatever the core's own metadata
    # says, and "2.1" and "2.1.0" are the same release to pip and different strings to us.
    assert Version(installed) == Version(supported), (
        f"the installed {CORE_DISTRIBUTION} is {installed}, the supported release is "
        f"{supported}. Either re-install (`pip install -e '.[dev]'`), or move the pin "
        f"deliberately: change core-ref.txt, run the whole gate line, and say in the commit "
        f"message what changed in the core and why the bump is safe. If the override is "
        f"intentional, set {UNPINNED_ENV_VAR}=1."
    )


def test_the_supported_release_satisfies_the_declared_dependency():
    """Two files now name the core, and they make different claims — hold them together.

    `pyproject.toml` declares the range this code is *compatible* with; `core-ref.txt` names
    the one release it is *tested* against. They are not the same statement and must not be
    collapsed into one, but a pin outside its own declared range is incoherent: the
    dependency would forbid installing the very version CI proves the repository against.
    """
    from packaging.specifiers import SpecifierSet

    supported = _supported_core_ref()
    specifier = SpecifierSet(_declared_core_specifier())
    assert supported in specifier, (
        f"core-ref.txt pins {supported}, which pyproject.toml's {CORE_DISTRIBUTION}"
        f"{specifier} excludes"
    )


def test_nothing_else_in_the_repository_names_a_core_revision():
    """One file owns the supported revision. A second copy is the thing that goes stale.

    Two spellings are forbidden outside `core-ref.txt`: a 40-character commit SHA (nothing
    here may pin the core to a commit any more — the artifact is a release), and an exact
    `==` pin of the distribution, which is what a CI install line would grow if someone
    typed the version instead of reading the file. `pyproject.toml`'s floor is deliberately
    not caught: a range is a compatibility claim, held against the pin by
    `test_the_supported_release_satisfies_the_declared_dependency`.

    Checked over the files a reader would expect to carry one: the workflows, the packaging
    metadata and the setup instructions.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    ref = _supported_core_ref()
    candidates = [
        *(root / ".github" / "workflows").glob("*.yml"),
        root / "pyproject.toml",
        root / "CLAUDE.md",
        root / "README.md",
    ]
    sha = re.compile(r"\b[0-9a-f]{40}\b")
    # `==` followed by a digit: a literal version typed into the file. `==$(…)`, the shell
    # substitution that reads core-ref.txt, is the required spelling and is not an offender.
    exact_pin = re.compile(r"ibkr[-_]core[-_]mcp\s*(?:\[[^\]]*\]\s*)?==\s*\d")
    offenders = {}
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        found = [
            name
            for name, pattern in (("a commit SHA", sha), ("an exact == pin", exact_pin))
            if pattern.search(text)
        ]
        if found:
            offenders[str(path.relative_to(root))] = found
    assert not offenders, (
        f"the core revision is named outside core-ref.txt: {offenders} — core-ref.txt "
        f"({ref}) is the only place that may name one, and CI reads it rather than "
        "repeating it"
    )


# The lanes that decide whether a change here is mergeable. Both resolve the core at the
# pinned release; neither may check its source out, and neither may switch the pin assertion
# off. `secret-scan` touches no Python and is not in scope.
BLOCKING_JOBS = ("test", "dependency-audit")
FORWARD_COMPAT_JOB = "forward-compat"


def _ci_jobs_without_comments() -> dict[str, str]:
    """`ci.yml`'s jobs, keyed by name, with every comment line removed.

    Comments are stripped because this file's comments *discuss* the mechanism they assert —
    a bare substring search found "CLAUDIA_CORE_UNPINNED" in the sentence explaining it and
    passed a mutation that had deleted the actual `env:` entry (measured 2026-09-19 while
    writing this test). What a workflow does is its non-comment lines.

    Split per job rather than into "blocking" and "the rest", for the same reason: a whole-
    half search let a job stop reading the pin while a sibling's mention kept the assertion
    green (the M9 mutation, same session).
    """
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]

    jobs: dict[str, list[str]] = {}
    current: str | None = None
    in_jobs = False
    for line in live:
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        header = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*", line)
        if header:
            current = header.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)

    found = {name: "\n".join(lines) for name, lines in jobs.items()}
    # Named here rather than at each call site, so a renamed or deleted job reports itself
    # instead of surfacing as a KeyError three frames away.
    expected = (*BLOCKING_JOBS, FORWARD_COMPAT_JOB)
    missing = [name for name in expected if name not in found]
    assert not missing, (
        f"ci.yml no longer defines {missing} (it has {sorted(found)}) — these job names are "
        "part of the cross-repo contract, because which lane blocks and which lane is "
        "informational is the whole two-lane design"
    )
    return found


def test_ci_reads_the_pin_rather_than_repeating_it():
    """The mechanism, not just the rule: the workflow must resolve the core from this file.

    A rule that no file may name the version is worth nothing if the install line stopped
    naming *any* version — the blocking lanes would then track the floor in `pyproject.toml`,
    not the pin, and a core release published between two pushes here would silently become
    the tested core. So this asserts the positive, per job: each blocking lane reads
    `core-ref.txt` where it resolves the core, and no blocking lane checks the core's source
    out any more.
    """
    import re

    jobs = _ci_jobs_without_comments()

    # One LINE that pins the distribution and reads the file, not two facts anywhere in the
    # job: `core-ref.txt` also appears in a cache key and `ibkr-core-mcp` in a `pip show`,
    # and a whole-job search passed a mutation that had deleted the install line outright
    # (M10, 2026-09-19).
    pinned_from_the_file = re.compile(r"ibkr[-_]core[-_]mcp(\[[^\]]*\])?\s*==")
    for name in BLOCKING_JOBS:
        resolves = [
            line
            for line in jobs[name].splitlines()
            if pinned_from_the_file.search(line) and "core-ref.txt" in line
        ]
        assert resolves, (
            f"the {name} job has no line that pins ibkr-core-mcp to the version in "
            "core-ref.txt; it is tracking the dependency floor, which is a compatibility "
            "range and not a pin"
        )
        body = jobs[name]
        assert "repository: stephus182/ibkr_core_mcp" not in body, (
            f"the {name} job still checks the core out of GitHub; since 2026-09-19 the "
            "supported core is installed from PyPI and only the forward-compatibility lane "
            "uses a checkout"
        )

    assert "repository: stephus182/ibkr_core_mcp" in jobs[FORWARD_COMPAT_JOB], (
        "the forward-compatibility lane no longer checks out core main — it cannot come "
        "from PyPI, because its whole subject is unreleased changes"
    )


def test_only_the_forward_compatibility_lane_switches_the_pin_assertion_off():
    """The escape hatch must be reachable from exactly one lane, and that lane must use it.

    Both halves matter and each was mutated to prove it (2026-09-19). If the informational
    lane stops setting the variable, it goes red for the one reason it is designed to
    tolerate and its real signal is lost in the noise. If a blocking lane starts setting it,
    the assertion that proves CI ran against the supported release is switched off with
    nothing to say so — a green tick over an unchecked claim.
    """
    jobs = _ci_jobs_without_comments()

    assert f'{UNPINNED_ENV_VAR}: "1"' in jobs[FORWARD_COMPAT_JOB], (
        f"the forward-compatibility lane does not set {UNPINNED_ENV_VAR}, so the "
        "supported-release assertion will fail there for the reason that lane exists"
    )
    for name in BLOCKING_JOBS:
        assert UNPINNED_ENV_VAR not in jobs[name], (
            f"the {name} job sets {UNPINNED_ENV_VAR}, which switches off the one assertion "
            "that proves it ran against the supported release"
        )


def test_the_forward_compatibility_lane_proves_its_override_took():
    """That lane's every assertion is void if the editable install silently failed.

    It installs the core editable over the PyPI copy, then runs the seam tests. If the
    override does not take, those tests run against the *pinned release* and pass for the
    wrong reason — and the job carries `continue-on-error`, so nothing turns red and the
    only signal is a line in a log nobody opens.

    Version strings cannot close this: core `main` and the published release declared the
    same 2.0.1 on 2026-09-19, nine commits apart. Provenance can, which is what
    `claudia.install_check --require-editable` checks (PEP 610 `direct_url.json`).

    Until 2026-09-19 the step merely *printed* the import path. A printed claim is not a
    check, which is the same lesson this repository already wrote down about a green tick.
    """
    lane = _ci_jobs_without_comments()[FORWARD_COMPAT_JOB]

    assert "--require-editable" in lane, (
        "the forward-compatibility lane does not verify that its editable override took, so "
        "a silently failed install would leave it testing the released core and reporting "
        "forward compatibility"
    )


# ── Core behaviours a ClaudIA invariant rests on ─────────────────────────────────────────


def test_the_shared_price_formatter_still_renders_a_price_exactly():
    """CLA-SEC-005 is "what the human reads is what the click sends", and this is the "reads".

    `price_text_safe` lives in the core and renders every price on the proposal card, in the
    Gate 2 dialog and on the fill line. The defect it was written for was rounding: a 6E
    limit of 1.08455 read `1.08`, and two prices a full tick apart were indistinguishable on
    the surface a person authorises from. A core-side change back to two decimals would be
    green in that repository and would break this repository's strongest display claim with
    no commit here.
    """
    from ibkr_core_mcp.order_confirm import price_text_safe

    assert price_text_safe(1.08455) == "1.08455", "the formatter rounds again — re-read CLA-SEC-005"
    assert price_text_safe(0.5) == "0.50", "two decimals are the floor, not the ceiling"
    # Total by contract: the cancel card is built from `get_order_status`, so a string IBKR
    # sends that parses as no number must render as itself rather than take the card down.
    assert price_text_safe("Market") == "Market"


def test_the_toolkit_call_the_turn_loop_unpacks_keeps_its_arity():
    """`result_text, _ = self._toolkit.execute(name, inputs)` — a third return value is a 500.

    The dispatcher's one line into the core. It is not covered by the gated-entry-point test
    above, because `execute` is not an order write; it is covered here because unpacking is
    a contract and the turn loop has no other way to reach the toolkit.
    """
    from ibkr_core_mcp import ClaudeToolkit

    parameters = inspect.signature(ClaudeToolkit.execute).parameters
    assert [p for p in parameters if p != "self"][:2] == ["name", "inputs"], (
        f"ClaudeToolkit.execute's first two parameters changed: {list(parameters)}"
    )
    annotation = inspect.signature(ClaudeToolkit.execute).return_annotation
    assert "tuple" in str(annotation).lower(), (
        f"ClaudeToolkit.execute no longer returns a tuple ({annotation!r}); "
        "`result_text, _ = …` in agent._stream_turn unpacks one"
    )
