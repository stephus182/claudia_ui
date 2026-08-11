"""Unit tests for claudia/tradingview.py — binary discovery, env, tool filtering, CDP."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import claudia.tradingview as tv_module
from claudia.tradingview import (
    TradingViewBridge,
    _find_tv_mcp_bin,
    check_cdp_running,
)

# ── _find_tv_mcp_bin — TRADINGVIEW_MCP_PATH env var ──────────────────────────

def test_find_bin_env_var_valid_js(tmp_path, monkeypatch):
    """An env-var path pointing at a real .js entry point is used as-is."""
    fake = tmp_path / "server.js"
    fake.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(fake)


def test_find_bin_env_var_missing_file_falls_through(tmp_path, monkeypatch):
    """An env var naming a file that does not exist falls through to the search."""
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(tmp_path / "nonexistent.js"))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


def test_find_bin_env_var_not_js_falls_through(tmp_path, monkeypatch):
    """An env var naming a non-.js file falls through to the search."""
    fake = tmp_path / "server.sh"
    fake.write_text("#!/bin/bash")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


# ── _find_tv_mcp_bin — shutil.which ──────────────────────────────────────────

def test_find_bin_uses_which_when_env_unset(tmp_path, monkeypatch):
    """With no env var, a binary on PATH is used."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with patch("claudia.tradingview.shutil.which", return_value="/usr/local/bin/tradingview-mcp"):
        result = _find_tv_mcp_bin()
    assert result == "/usr/local/bin/tradingview-mcp"


# ── _find_tv_mcp_bin — home-based paths ──────────────────────────────────────

def test_find_bin_js_src_in_home(tmp_path, monkeypatch):
    """A JS source checkout in the home directory is found."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    js_src = tmp_path / ".tradingview-mcp" / "src" / "server.js"
    js_src.parent.mkdir(parents=True)
    js_src.write_text("// js")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(js_src)


def test_find_bin_ts_build_in_home(tmp_path, monkeypatch):
    """A TypeScript build output in the home directory is found."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    ts_build = tmp_path / ".tradingview-mcp" / "build" / "index.js"
    ts_build.parent.mkdir(parents=True)
    ts_build.write_text("// ts bundle")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(ts_build)


def test_find_bin_prefers_js_src_over_ts_build(tmp_path, monkeypatch):
    """JS source wins over a build directory when both exist."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    js_src = tmp_path / ".tradingview-mcp" / "src" / "server.js"
    js_src.parent.mkdir(parents=True)
    js_src.write_text("// js")
    ts_build = tmp_path / ".tradingview-mcp" / "build" / "index.js"
    ts_build.parent.mkdir(parents=True)
    ts_build.write_text("// ts")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(js_src)


# ── _find_tv_mcp_bin — vendor fallback paths ─────────────────────────────────

def test_find_bin_vendor_js_requires_node_modules(tmp_path, monkeypatch):
    """A vendored copy without installed dependencies is not usable and is skipped."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    vendor_js = tmp_path / "vendor" / "tradingview-mcp" / "src" / "server.js"
    vendor_js.parent.mkdir(parents=True)
    vendor_js.write_text("// vendor js")
    # No node_modules → should NOT be selected
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


def test_find_bin_vendor_js_with_node_modules(tmp_path, monkeypatch):
    """A vendored copy with dependencies installed is used."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    vendor_base = tmp_path / "vendor" / "tradingview-mcp"
    vendor_js = vendor_base / "src" / "server.js"
    vendor_js.parent.mkdir(parents=True)
    vendor_js.write_text("// vendor js")
    (vendor_base / "node_modules").mkdir()
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(vendor_js)


def test_find_bin_vendor_bundle_fallback(tmp_path, monkeypatch):
    """Vendor legacy bundle (no node_modules required) is the last resort."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    vendor_bundle = tmp_path / "vendor" / "tradingview-mcp" / "index.js"
    vendor_bundle.parent.mkdir(parents=True)
    vendor_bundle.write_text("// legacy bundle")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(vendor_bundle)


def test_find_bin_returns_none_when_nothing_found(tmp_path, monkeypatch):
    """With no sidecar anywhere, None is returned — TradingView is optional."""
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


# ── check_cdp_running ─────────────────────────────────────────────────────────

def test_check_cdp_running_true_when_port_open():
    """An open CDP port means TradingView Desktop is reachable."""
    with patch("claudia.tradingview.socket.create_connection"):
        assert check_cdp_running() is True


def test_check_cdp_running_false_when_port_closed():
    """A closed CDP port means the desktop app is not reachable, even if the sidecar is up."""
    with patch("claudia.tradingview.socket.create_connection", side_effect=OSError):
        assert check_cdp_running() is False


# ── TradingViewBridge — tool filtering ───────────────────────────────────────

def test_get_tools_returns_only_curated_subset():
    """Only the curated tools are offered to the model, not the sidecar's full surface."""
    bridge = TradingViewBridge()
    all_tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "quote_get", "description": "", "input_schema": {}},
        {"name": "some_unlisted_tool", "description": "", "input_schema": {}},
        {"name": "another_unlisted", "description": "", "input_schema": {}},
    ]
    bridge._tools = all_tools
    bridge._curated_tools = [t for t in all_tools if t["name"] in tv_module._CURATED_TOOLS]
    result = bridge.get_tools()
    names = [t["name"] for t in result]
    assert "chart_get_state" in names
    assert "quote_get" in names
    assert "some_unlisted_tool" not in names
    assert "another_unlisted" not in names


def test_get_all_tools_returns_everything():
    """The uncurated accessor still exposes the full set, for diagnostics."""
    bridge = TradingViewBridge()
    all_tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "some_unlisted_tool", "description": "", "input_schema": {}},
    ]
    bridge._tools = all_tools
    bridge._curated_tools = [all_tools[0]]
    assert len(bridge.get_all_tools()) == 2


def test_curated_tools_set_has_16_entries():
    # 16 tools verified against live sidecar 2026-06-30:
    # data_get_equity_curve renamed to data_get_equity; data_get_trades added.
    """The curated set is pinned at its size, so a sidecar upgrade cannot change it silently."""
    assert len(tv_module._CURATED_TOOLS) == 16


# ── TradingViewBridge — subprocess env allowlist ─────────────────────────────

@pytest.mark.asyncio
async def test_start_env_excludes_secrets(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY and other secrets must not reach the Node subprocess."""
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("GDRIVE_TOKEN_FILE", "/secret/token.json")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    captured_env = {}

    def fake_params(**kwargs):
        """Capture the environment the subprocess would have been given."""
        captured_env.update(kwargs.get("env", {}))
        return MagicMock()

    class FakeCM:
        """A stand-in for the sidecar's stdio context manager."""
        async def __aenter__(self):
            """Hand back a read/write pair, as the real stdio client does."""
            return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a):
            """Nothing to tear down for the stub."""
            pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    with patch("claudia.tradingview.StdioServerParameters", side_effect=fake_params), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        bridge = TradingViewBridge()
        await bridge.start()

    assert captured_env, "StdioServerParameters was never called — env not captured"
    assert "ANTHROPIC_API_KEY" not in captured_env
    assert "GDRIVE_TOKEN_FILE" not in captured_env
    assert "PATH" in captured_env
    assert "CHROME_REMOTE_DEBUG_PORT" in captured_env


@pytest.mark.asyncio
async def test_start_sets_every_cdp_port_name(tmp_path, monkeypatch):
    """A non-default CDP port must reach the sidecar under all three variable names.

    The sidecar renamed this variable: upstream reads TV_CDP_PORT / CDP_PORT, older
    builds — including the vendor/ fallback — read CHROME_REMOTE_DEBUG_PORT. Setting
    only one name reproduces security-audit-2026-06-12 M-1, where the override was
    silently ignored and the sidecar quietly used 9222.
    """
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setattr("claudia.tradingview._TV_DEBUG_PORT", 9333)

    captured_env: dict[str, str] = {}

    def fake_params(**kwargs):
        """Capture the environment the subprocess would have been given."""
        captured_env.update(kwargs.get("env", {}))
        return MagicMock()

    class FakeCM:
        """A stand-in for the sidecar's stdio context manager."""
        async def __aenter__(self):
            """Hand back a read/write pair, as the real stdio client does."""
            return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a):
            """Nothing to tear down for the stub."""
            pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    with patch("claudia.tradingview.StdioServerParameters", side_effect=fake_params), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        bridge = TradingViewBridge()
        await bridge.start()

    for name in ("CHROME_REMOTE_DEBUG_PORT", "TV_CDP_PORT", "CDP_PORT"):
        assert captured_env.get(name) == "9333", f"{name} did not carry the configured port"


# ── result post-processing at the execute() seam ──────────────────────────────

def test_post_process_annotates_epoch_seconds_with_utc_iso():
    """A bare epoch gets an unambiguous sibling; the original integer is untouched."""
    raw = json.dumps({"bars": [{"time": 1786455000, "close": 196.6749}]})
    out = json.loads(tv_module._post_process("data_get_ohlcv", raw))
    assert out["bars"][0]["time_utc"] == "2026-08-11T13:30:00Z"
    assert out["bars"][0]["time"] == 1786455000
    assert out["bars"][0]["close"] == 196.6749


def test_post_process_does_not_treat_a_bar_index_as_an_epoch():
    """data_get_trades returns time_index (a bar number). Annotating it would invent a date.

    Two cases, and only the second pins the exact-key match. The realistic 782 is rejected
    by the range check, so a prefix match on "time" would pass it too. The in-range value
    under the same `time`-prefixed key is what separates them: exact-key matching leaves it
    alone, a prefix match stamps a bogus date on a bar number.
    """
    raw = json.dumps({"trades": [{"time_index": 782, "price": 303.8}]})
    out = json.loads(tv_module._post_process("data_get_trades", raw))
    assert "time_index_utc" not in out["trades"][0]
    assert out["trades"][0]["time_index"] == 782

    in_range = json.dumps({"trades": [{"time_index": 1786455000, "price": 303.8}]})
    out = json.loads(tv_module._post_process("data_get_trades", in_range))
    assert "time_index_utc" not in out["trades"][0]
    assert out["trades"][0]["time_index"] == 1786455000


def test_post_process_ignores_out_of_range_time_values():
    """A `time` field that cannot be an epoch is left alone rather than guessed at."""
    raw = json.dumps({"time": 782, "other": {"timestamp": 0}})
    out = json.loads(tv_module._post_process("quote_get", raw))
    assert "time_utc" not in out
    assert "timestamp_utc" not in out["other"]


def test_post_process_ignores_booleans_in_time_fields():
    """A bool in a `time` field must not become 1970-01-01.

    This pins the OUTCOME, not the mechanism, and the distinction is worth stating. bool is
    a subclass of int, so `True` reaches the numeric branch — but `True == 1`, still far
    below the (lowered) _EPOCH_MIN of 100_000_000, so it is the RANGE check that actually
    rejects it. Re-verified after the floor was lowered: deleting the explicit
    `not isinstance(value, bool)` guard leaves this test green. The guard is kept as
    defense-in-depth for a floor at or below 1, and is commented as such at the guard
    itself rather than only here.
    """
    raw = json.dumps({"time": True})
    out = json.loads(tv_module._post_process("quote_get", raw))
    assert "time_utc" not in out


def test_post_process_annotates_the_ohlcv_summary_period():
    """data_get_ohlcv summary=true puts real epochs under `from`/`to`, not `time`.

    Shape is the sidecar's own (src/core/data.js:170): a `period` object beside
    `last_5_bars`, whose `time` fields DO get annotated. Leaving period bare would imply
    those two are not dates. Two generic key names are safe here only because both guards
    apply — exact key match AND the range check.
    """
    raw = json.dumps({
        "bar_count": 500,
        "period": {"from": 1786368600, "to": 1786455000},
        "last_5_bars": [{"time": 1786455000, "close": 196.6749}],
    })
    out = json.loads(tv_module._post_process("data_get_ohlcv", raw))
    assert out["period"]["from_utc"] == "2026-08-10T13:30:00Z"
    assert out["period"]["to_utc"] == "2026-08-11T13:30:00Z"
    assert out["period"]["from"] == 1786368600
    assert out["last_5_bars"][0]["time_utc"] == "2026-08-11T13:30:00Z"
    assert "bar_count_utc" not in out


def test_post_process_does_not_clobber_an_existing_utc_sibling():
    """If the sidecar ever sends its own `<key>_utc`, ours must not overwrite it.

    Order-independent by construction: the guard consults the INPUT dict, so the result
    does not depend on whether the sidecar's sibling precedes or follows the epoch.
    """
    for payload in (
        {"time": 1786455000, "time_utc": "sidecar value"},
        {"time_utc": "sidecar value", "time": 1786455000},
    ):
        out = json.loads(tv_module._post_process("quote_get", json.dumps(payload)))
        assert out["time_utc"] == "sidecar value"


def test_post_process_returns_raw_when_a_transform_raises():
    """A transform that blows up must not turn a succeeded tool call into a failure."""
    raw = json.dumps({"time": 1786455000})

    def boom(_payload):
        """Stand-in for a transform that raises — RecursionError is the realistic one."""
        raise RecursionError("too deep")

    with patch("claudia.tradingview._TRANSFORMS", (boom,)):
        assert tv_module._post_process("data_get_ohlcv", raw) == raw


def test_post_process_passes_non_json_through_untouched():
    """execute() also returns plain error strings. A transform must never eat one."""
    msg = "TradingView is not connected."
    assert tv_module._post_process("tv_health_check", msg) == msg


def test_post_process_passes_a_top_level_array_through_untouched():
    """Valid JSON that is not an object is returned verbatim, not re-serialised.

    Not hypothetical: execute()'s `json.dumps(result.content)` fallback produces exactly a
    top-level array. Byte-identical is the assertion — dropping the non-dict branch would
    both reformat it and annotate the epoch inside.
    """
    raw = '[{"time": 1786455000}]'
    assert tv_module._post_process("data_get_ohlcv", raw) == raw


async def test_execute_routes_the_sidecar_result_through_post_process():
    """The transform is only worth anything if execute() actually applies it."""
    item = MagicMock()
    item.text = json.dumps({"bars": [{"time": 1786455000}]})
    fake_session = AsyncMock()
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[item]))

    bridge = TradingViewBridge()
    bridge._session = fake_session
    out = json.loads(await bridge.execute("data_get_ohlcv", {}))

    assert out["bars"][0]["time_utc"] == "2026-08-11T13:30:00Z"
