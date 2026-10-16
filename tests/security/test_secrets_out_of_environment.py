"""CLA-SEC-014 — a secret is read from the protected `.env` into the object that uses it and
never placed in a process environment (gap #103 with #100; core register F48; CWE-526).

Until 2026-10-16 `panel_app`, `order_flow` (five times), `gateway_preflight` and the core's
`Config` each ran `load_dotenv`, which copied the whole file — `ANTHROPIC_API_KEY`,
`IBKR_FLEX_TOKEN`, `FIRECRAWL_API_KEY` — into ClaudIA's environment, and `pgrep`, `osascript`,
`pkill`, `open -a`, `git`, the Gate 2 dialog and the docker commands all inherited it. The
mechanism is `claudia/env_file.py`; this file is what fails when it stops working.

The real-child test spawns `python` — never a command that could reach the desktop.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from claudia import env_file
from tests.security.structural import PACKAGE_DIR, SCRIPTS_DIR

_TESTS_DIR = Path(__file__).resolve().parents[1]
_SECRET_LINES = (
    "ANTHROPIC_API_KEY=sk-ant-file-value\n"
    "IBKR_FLEX_TOKEN=flex-file-value\n"
    "IBKR_GATEWAY_URL=https://example.invalid/v1/api\n"
)


def _fake_client(monkeypatch) -> MagicMock:
    """Replace `claudia.agent.AsyncAnthropic` with a recording fake, and return it."""
    fake = MagicMock()
    monkeypatch.setattr("claudia.agent.AsyncAnthropic", fake)
    return fake


def _build_agent() -> None:
    """A `ClaudIAAgent` from mocks — the shape `tests/test_agent.py::_make_agent` uses."""
    from claudia.agent import ClaudIAAgent

    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    for name in (
        "get_rendered_proposals",
        "get_completed_order_actions",
        "get_refused_order_actions",
    ):
        getattr(store, name).return_value = []
    toolkit = MagicMock()
    toolkit.tools = []
    ClaudIAAgent(
        toolkit=toolkit, store=store, context_loader=MagicMock(), session_id="s", sink=MagicMock()
    )


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    """A 0600 `.env` of our own, in place of the stand-in conftest points the loader at."""
    path = tmp_path / ".env"
    path.write_text(_SECRET_LINES, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(env_file, "ENV_FILE", path)
    monkeypatch.setattr(env_file, "_loaded", None)
    monkeypatch.delenv("IBKR_GATEWAY_URL", raising=False)
    return path


def test_an_inherited_secret_is_removed_with_a_warning_naming_the_variable(
    env_path, monkeypatch, caplog
):
    """Design test 5. Break: the `del` removed — red on the environment; the value logged —
    red on the last line."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-inherited-value")

    with caplog.at_level(logging.WARNING, logger="claudia.env_file"):
        env_file.load()

    assert "ANTHROPIC_API_KEY" not in os.environ
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ANTHROPIC_API_KEY" in w for w in warnings), warnings
    assert not any("sk-ant-inherited-value" in w for w in warnings)


def test_a_real_child_started_after_the_loader_sees_no_secret(env_path):
    """Design test 6, end to end: a child inheriting `os.environ` — no `env=` — reports what
    it was given. The setting is there (the loader ran); no secret is. Break: the loader
    exporting secrets (hand `secret_names=()` to the core) — red."""
    env_file.load()

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os, sys; sys.stdout.write(json.dumps(dict(os.environ)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    reported = json.loads(child.stdout)

    assert reported["IBKR_GATEWAY_URL"] == "https://example.invalid/v1/api"
    assert "ANTHROPIC_API_KEY" not in reported and "IBKR_FLEX_TOKEN" not in reported
    assert "sk-ant-file-value" not in child.stdout and "flex-file-value" not in child.stdout


def test_the_key_is_held_as_a_secret_and_unwrapped_only_for_the_client(env_path):
    key = env_file.anthropic_api_key()

    assert isinstance(key, SecretStr) and key.get_secret_value() == "sk-ant-file-value"
    assert "sk-ant-file-value" not in f"{key} {key!r}"


def test_a_later_load_dotenv_by_anyone_exports_no_secret(env_path, monkeypatch):
    """Open point 1: `crawl4ai/config.py` calls a bare `load_dotenv()` at import, and the
    venv sits inside the repository, so its search finds ClaudIA's `.env`. Once the loader has
    run, that call — any call — exports settings and never a secret.

    The genuine python-dotenv function is put back first (conftest keeps it as
    `_REAL_LOAD_DOTENV` after making the module's a no-op), so the named break is honest:
    with the replacement removed, the REAL loader runs and exports the secrets — red for
    the right reason, not for a no-op's silence.
    """
    import dotenv
    import dotenv.main

    from tests import conftest

    monkeypatch.setattr(dotenv, "load_dotenv", conftest._REAL_LOAD_DOTENV)
    monkeypatch.setattr(dotenv.main, "load_dotenv", conftest._REAL_LOAD_DOTENV)
    env_file.load()
    monkeypatch.delenv("IBKR_GATEWAY_URL", raising=False)

    # The third party's call, with the fixture file named. NEVER a bare `load_dotenv()` here:
    # under this test's own named break the genuine function would search upward from this
    # file and read the operator's real `.env` — it did, on 2026-10-16, and pytest printed
    # two real values. A test must be safe on a broken guard. The no-path case is held by
    # identity instead: the installed function reads `ENV_FILE`, which the tests above cover.
    dotenv.load_dotenv(env_path)

    assert dotenv.load_dotenv is env_file._load_dotenv_without_secrets
    assert dotenv.main.load_dotenv is env_file._load_dotenv_without_secrets
    assert os.environ["IBKR_GATEWAY_URL"] == "https://example.invalid/v1/api"
    assert "ANTHROPIC_API_KEY" not in os.environ and "IBKR_FLEX_TOKEN" not in os.environ


def test_the_real_env_file_cannot_be_opened_by_a_test(tmp_path, monkeypatch):
    """CLA-SEC-009: python-dotenv's default search walks up from any test file to the
    repository root, where the operator's `.env` is. On 2026-10-16 a mutation of the loader's
    replacement let a bare `load_dotenv()` in a test read that file, and the assertion's
    introspection printed two real values. The audit hook that refuses `~/.ibkr_core` now
    refuses any file event naming the real `.env`. Driven against a stand-in, as
    `test_no_real_data_io.py` drives the directory guard, so this test is safe on a broken
    guard: it opens nothing real. Break: the `_GUARDED_ENV_FILE` clause removed — red."""
    from tests import conftest

    stand_in = tmp_path / ".env"
    stand_in.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(conftest, "_GUARDED_ENV_FILE", stand_in.resolve())
    assert conftest._OPERATOR_GUARD["armed"]  # before anything real

    with pytest.raises(pytest.fail.Exception, match="reach the operator's data"):
        open(stand_in, encoding="utf-8").close()


@pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX")
def test_a_refused_file_is_reported_as_the_model_being_unreachable(env_path, caplog):
    """ClaudIA cannot reach the model at all then, and says so naming the file and the fix,
    on the terminal at ERROR. Break: `model_unreachable_reason` ignoring `refused` — red."""
    env_path.chmod(0o640)

    with caplog.at_level(logging.ERROR, logger="claudia.env_file"):
        reason = env_file.warn_if_model_unreachable()

    assert reason is not None and f"chmod 600 {env_path}" in reason
    assert any(
        "cannot reach the model" in r.getMessage() and reason in r.getMessage()
        for r in caplog.records
    )
    assert env_file.anthropic_api_key() is None


def test_building_an_agent_without_a_key_raises_setup_required(env_path, monkeypatch):
    """Open point 3: never the SDK's "set the environment variable" sentence. The error
    starts with `Setup required:` — `panel_app` shows such an error in the chat unlabelled
    (its convention for a missing `context.md`). Break: the agent passing `api_key=None`
    through to the SDK — red (the fake client is built, nothing raises)."""
    env_path.write_text("IBKR_GATEWAY_URL=https://example.invalid/v1/api\n", encoding="utf-8")
    fake = _fake_client(monkeypatch)

    with pytest.raises(
        RuntimeError, match=rf"^Setup required: ANTHROPIC_API_KEY is not set in {env_path}"
    ):
        _build_agent()

    fake.assert_not_called()


def test_a_file_without_the_key_is_reported_naming_the_variable_and_the_file(env_path):
    env_path.write_text("IBKR_GATEWAY_URL=https://example.invalid/v1/api\n", encoding="utf-8")

    reason = env_file.model_unreachable_reason()

    assert reason is not None and "ANTHROPIC_API_KEY" in reason and str(env_path) in reason


def test_a_file_with_the_key_reports_nothing(env_path):
    assert env_file.model_unreachable_reason() is None


def test_the_agent_builds_its_client_with_the_key_from_the_loader(env_path, monkeypatch):
    """The SDK reads no credential variable once `api_key=` is passed (anthropic 0.120.2,
    `_client.py`: "Explicit ctor args are total"). Break: the agent passing nothing — red."""
    fake = _fake_client(monkeypatch)

    _build_agent()

    assert fake.call_args.kwargs == {"api_key": "sk-ant-file-value"}


def test_building_the_core_config_exports_no_anthropic_key(env_path):
    """Found live 2026-10-16, twelve seconds after ClaudIA was serving: the core's
    `Config.from_env` read the file with the core's secret names only, so the toolkit's
    `Config` exported `ANTHROPIC_API_KEY` as a setting, and the next `gateway_url()` poll
    removed it again with a warning. `core_config()` hands ClaudIA's names through. Break:
    the names not passed — red."""
    env_file.load()

    cfg = env_file.core_config()

    assert "ANTHROPIC_API_KEY" not in os.environ
    assert cfg.flex_token.get_secret_value() == "flex-file-value"


# ── Structural (design test 7) ─────────────────────────────────────────────────────────


def _calls_and_names(path: Path) -> tuple[list[ast.Call], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return calls, names


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sources() -> list[Path]:
    return sorted(p for root in (PACKAGE_DIR, SCRIPTS_DIR, _TESTS_DIR) for p in root.rglob("*.py"))


_THE_LOADER = PACKAGE_DIR / "env_file.py"
_CONFTEST = _TESTS_DIR / "conftest.py"  # keeps `load_dotenv` a no-op for third parties
_THIS_FILE = Path(__file__).resolve()  # calls `dotenv.load_dotenv` on purpose, above


def test_no_dotenv_loader_outside_env_file():
    """`load_dotenv` and `dotenv_values` appear only in the loader (and conftest, which
    neutralises the first; and this file, which drives the replacement). A second reader is
    a second place a secret can be exported from. Break: a `load_dotenv` call anywhere
    else — red."""
    offenders = {
        str(path): sorted(used)
        for path in _sources()
        if path not in (_THE_LOADER, _CONFTEST, _THIS_FILE)
        and (used := _calls_and_names(path)[1] & {"load_dotenv", "dotenv_values"})
    }
    assert not offenders, offenders


def test_every_anthropic_client_is_built_with_an_explicit_api_key():
    """`AsyncAnthropic()` / `Anthropic()` with no `api_key=` reads `ANTHROPIC_API_KEY` from
    the environment. Break: drop the keyword in `agent.py` — red."""
    bare = {
        f"{path}:{call.lineno}"
        for path in _sources()
        for call in _calls_and_names(path)[0]
        if _callee(call) in ("AsyncAnthropic", "Anthropic")
        and not any(kw.arg == "api_key" for kw in call.keywords)
    }
    assert not bare, bare


def test_config_from_env_is_called_only_by_the_loader():
    """With no path the core searches upward from its own `config.py` — under the developer
    override that is the core repository's `.env`, not ClaudIA's — and the Flex token would
    silently read as "not configured". Every `Config` comes from `env_file.core_config()`.
    Break: a bare `Config.from_env()` in `order_flow.py` — red."""
    sites = {
        f"{path}:{call.lineno}"
        for path in _sources()
        if path != _THE_LOADER and not path.is_relative_to(_TESTS_DIR)
        for call in _calls_and_names(path)[0]
        if _callee(call) == "from_env"
    }
    assert not sites, sites


def test_the_structural_scans_see_each_shape(tmp_path):
    a = tmp_path / "a.py"
    a.write_text(
        "import anthropic\nfrom dotenv import load_dotenv\nanthropic.Anthropic()\nload_dotenv()\n"
    )
    calls, names = _calls_and_names(a)
    assert {"load_dotenv"} <= names
    assert [_callee(c) for c in calls] == ["Anthropic", "load_dotenv"]


def test_a_name_relative_to_a_directory_descriptor_is_not_judged_as_the_env_file(tmp_path):
    """pytest's own cleanup of old temp directories unlinks by bare name through a `dir_fd`.
    Resolved against the working directory — the repository root — a bare `.env` read as the
    operator's file, and the first run of the guard failed at session end on its own fixtures
    (2026-10-16). A path relative to a descriptor names no whole path, and is not judged.
    Break: the `fd_relative` clause removed — red (the refusal), and the run fails at its end."""
    stand_in = tmp_path / ".env"
    stand_in.write_text("X=1\n", encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        os.remove(".env", dir_fd=fd)
    finally:
        os.close(fd)

    assert not stand_in.exists()
