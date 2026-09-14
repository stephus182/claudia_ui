"""CLA-SEC-010 — what the TradingView sidecar's *child process* actually receives.

`tests/test_tradingview.py::test_start_env_excludes_secrets` asserts the dictionary
ClaudIA builds, by patching `StdioServerParameters`. That is the input to the MCP library,
not the environment of the process — and the library does not pass it through. Read from
mcp 1.28.1, `mcp/client/stdio/__init__.py`::

    env=({**get_default_environment(), **server.env} if server.env is not None else …)

so the effective set is the library's `DEFAULT_INHERITED_ENV_VARS` *underneath* ours. Today
that adds `LOGNAME`, `SHELL` and `TERM` on this platform and nothing sensitive, which is why
the property has held while being asserted one layer too early. A release that widened that
list — or replaced the merge with `os.environ` — would leak every secret in `.env` to an
unsandboxed Node process and every existing test would stay green (audit 2026-09-13, § 3.10).

**No mock stands between the assertion and the kernel here.** The environment comes from
`tradingview._sidecar_env()`, the library composes it, `anyio.open_process` spawns a real
child, and the child reports what it was given. The one thing this file does not exercise is
`start`'s call to `_sidecar_env` — `test_the_bridge_builds_its_environment_from_the_declared_set`
pins that.

The floor of names ClaudIA does not control is **measured, not typed**: the same child is
spawned a second time with an empty `env`, which yields the library's defaults plus whatever
the child's own runtime sets for itself (`LC_CTYPE` and `__CF_USER_TEXT_ENCODING` appear for
a CPython child on macOS and are set by the child, not by us). Subtracting one run from the
other leaves exactly what ClaudIA contributed, with no hand-kept exemption list to go stale.
"""

from __future__ import annotations

import json
import sys

import pytest

from claudia import tradingview

# Every prefix `tests/conftest.py` scrubs from the pytest process, for the same reason: a
# hand-kept list of secret *names* was wrong in ibkr_core_mcp, and a prefix cannot be.
_SECRET_PREFIXES = (
    "ANTHROPIC_",
    "IBKR_",
    "GDRIVE_",
    "GOOGLE_",
    "FIRECRAWL_",
    "CRAWL4AI_",
)

_REPORTER = (
    "import json, os, sys;"
    "sys.stdout.write('');"
    "open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)));"
    "sys.stdin.read()"
)


async def _child_environment(env: dict[str, str], out_path) -> dict[str, str]:
    """Spawn a reporting child through the real MCP stdio client and read back its env."""
    import anyio
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-c", _REPORTER, str(out_path)], env=env
    )
    async with stdio_client(params):
        for _ in range(200):
            if out_path.exists() and out_path.read_text():
                break
            await anyio.sleep(0.05)
    assert out_path.exists() and out_path.read_text(), "the child never reported its environment"
    return dict(json.loads(out_path.read_text()))


@pytest.mark.asyncio
async def test_no_secret_reaches_the_sidecar_child_process(tmp_path, monkeypatch):
    """End to end: a secret in `os.environ` is not in the child's environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-the-sidecar")
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "flex-not-for-the-sidecar")
    monkeypatch.setenv("GDRIVE_TOKEN_FILE", "/secret/token.json")

    child_env = await _child_environment(tradingview._sidecar_env(), tmp_path / "env.json")

    leaked = [k for k in child_env if k.startswith(_SECRET_PREFIXES)]
    assert not leaked, f"the sidecar child was handed {leaked}"
    assert "sk-ant-not-for-the-sidecar" not in json.dumps(child_env), (
        "a secret reached the child under a name no prefix covers"
    )


@pytest.mark.asyncio
async def test_the_child_receives_the_declared_set_and_nothing_else_of_ours(tmp_path):
    """The names ClaudIA contributes, measured at the child, equal the ones it declares.

    The library's own floor is measured by a control spawn rather than listed here, so a
    release that widens `DEFAULT_INHERITED_ENV_VARS` shows up as a failure of the *first*
    test above if it carries a secret, and is simply absorbed here if it does not — which
    is the honest split: this test owns ClaudIA's contribution, that one owns the boundary.
    """
    declared = tradingview._sidecar_env()

    ours = await _child_environment(declared, tmp_path / "ours.json")
    floor = await _child_environment({}, tmp_path / "floor.json")

    contributed = {k: v for k, v in ours.items() if k not in floor or floor[k] != v}
    assert contributed == {k: v for k, v in declared.items() if floor.get(k) != v}, (
        f"the child's environment is not the one ClaudIA declared: "
        f"contributed={sorted(contributed)} declared={sorted(declared)}"
    )


def test_the_bridge_builds_its_environment_from_the_declared_set():
    """`start` must call `_sidecar_env` — otherwise the tests above guard a dead function."""
    import pathlib

    from tests.security.structural import called_names, function_named

    source = pathlib.Path(tradingview.__file__).read_text(encoding="utf-8")
    assert "_sidecar_env" in called_names(function_named(source, "start")), (
        "TradingViewBridge.start no longer builds its environment from _sidecar_env"
    )


def test_the_declared_set_names_no_secret(monkeypatch):
    """The allowlist is a list of *names*, so it can be checked without spawning anything."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert not [k for k in tradingview._sidecar_env() if k.startswith(_SECRET_PREFIXES)]
