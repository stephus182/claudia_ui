# Changelog

All notable changes to `claudia_ui` are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

This file starts at the PyPI switch below; earlier history is in the git log and in the dated
documents under `docs/`.

---

## [Unreleased]

### Changed

- **`ibkr_core_mcp` is installed from PyPI, not from a sibling checkout.** It is now a declared
  dependency — `ibkr-core-mcp>=2.0.1,<3` in `pyproject.toml` — resolved from
  [pypi.org/project/ibkr-core-mcp](https://pypi.org/project/ibkr-core-mcp/) (first release 2.0.1,
  2026-09-19, published via Trusted Publishing). Until now nothing in this repository's metadata
  declared the one library the whole application is built on: `pip install claudia_ui` produced a
  tree that could not import it, and the version range that works was written only in prose.
  The core's own guidance for consumers asks for exactly this specifier
  (`ibkr_core_mcp/docs/consumers.md` § "2.0.1 — the package is on PyPI").
- **`core-ref.txt` names a released version (`2.0.1`) instead of a commit SHA.** The file's job
  is unchanged — one source of truth for the release this repository is *supported* against —
  but what CI installs is now a version, so a SHA there would have been a second definition that
  nothing resolves. The immutability argument survives: PyPI refuses a second upload of an
  existing filename, where a git tag can be moved. The commit stays in the file as provenance
  (v2.0.1 = `4a70f8b`), verified by unpacking the published wheel and comparing it with the tag
  tree — 34/34 payload files byte-identical, none missing. The supported library code is
  unchanged by the bump: `git diff d88169c 4a70f8b -- ibkr_core_mcp/` is empty.
- **CI: the blocking lanes install the pinned release; only `forward-compat` still checks the
  core out.** `test` and `dependency-audit` no longer clone `stephus182/ibkr_core_mcp`; each
  reads the version out of `core-ref.txt` and installs it from PyPI, `dependency-audit` with the
  `[scraper]` extra as before. `forward-compat` keeps its checkout of core `main` and its
  editable install deliberately — early warning on *unreleased* changes cannot come from PyPI —
  and sets `CLAUDIA_CORE_UNPINNED=1`.
- Developer docs (`CLAUDE.md`, `README.md`, `SECURITY.md`, `docs/security-architecture.md`)
  describe the PyPI pin plus forward-compat design. The editable install remains documented as
  the *override* for working on both repositories at once.

### Fixed

- **The forward-compatibility CI lane now proves its editable override took, instead of
  printing a path.** That lane installs `ibkr_core_mcp` from a checkout of `main` over the
  PyPI copy; if the install silently failed, every seam assertion in it ran against the
  *pinned release* and passed for the wrong reason — and because the job carries
  `continue-on-error`, nothing turned red. The step ran `print(ibkr_core_mcp.__file__)` and
  exited 0 regardless. It now runs `python -m claudia.install_check --require-editable`,
  which fails the step. A version comparison could not have closed this: core `main` and the
  published release both declared `2.0.1` on 2026-09-19, nine commits apart, so the question
  has to be about provenance (PEP 610 `direct_url.json`), not version.

### Added

- `claudia.install_check.core_install_origin()` — `editable` / `directory` / `index` /
  `unknown` for the installed core, read from PEP 610 metadata, with `python -m
  claudia.install_check [--require-editable]` as its entry point. The module had no entry
  point before, so that command exited 0 in silence, which read exactly like a clean report.
- `tests/security/test_cross_repo_contract.py` asserts that the **installed** distribution
  version equals the pin, that the pin satisfies the declared dependency range, that each
  blocking CI lane resolves the core from `core-ref.txt` rather than from the floor, and that
  only `forward-compat` sets `CLAUDIA_CORE_UNPINNED`. The skip that variable triggers is scoped
  to the single installed-version assertion; every other contract check still runs under it.
- `packaging` as a declared dev dependency — the contract test needs real PEP 440 specifier
  semantics, and it resolved until now only because `pytest` happens to require it.
