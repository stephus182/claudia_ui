"""Unit tests for claudia/tradingview.py — binary discovery, env, tool filtering, CDP."""

import json
import re
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


def test_curated_tools_set_has_17_entries():
    """The curated set is pinned at its size, so a sidecar upgrade cannot change it silently."""
    # 16 verified against the live sidecar 2026-06-30 (data_get_equity_curve renamed to
    # data_get_equity; data_get_trades added). 17th added 2026-08-11:
    # data_get_indicator, see test_curated_set_pairs_the_setter_with_its_getter.
    assert len(tv_module._CURATED_TOOLS) == 17


def test_curated_set_pairs_the_setter_with_its_getter():
    """indicator_set_inputs needs input ids it cannot guess; data_get_indicator returns them.

    Curating the setter alone is what turned an upstream quirk into a false success on
    2026-08-11: the model guessed 'length' for a built-in RSI (whose ids are in_0..in_7),
    the sidecar reported success, and nothing changed.
    """
    assert "indicator_set_inputs" in tv_module._CURATED_TOOLS
    assert "data_get_indicator" in tv_module._CURATED_TOOLS


# Any snake_case token in a guard message is treated as a tool name unless it is listed
# here as jargon. Deliberately fail-closed: a new guard that names a tool from a family
# nobody anticipated must break this test rather than pass unnoticed, which is what a
# prefix allowlist ("chart_*, data_*, …") would have done. Adding a token here is a
# decision someone makes on purpose.
_GUARD_MESSAGE_NON_TOOL_TOKENS = frozenset(
    {
        "in_0",  # a study input id, from the example the message gives
        "in_7",
        "entity_id",  # the argument the model is told to pass, not a tool
    }
)

_SNAKE_TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def test_empty_result_guards_only_name_curated_tools():
    """A guard may only point the model at tools it can actually call.

    _EMPTY_RESULT_GUARDS tells the model what to do about a no-op — currently "call
    data_get_indicator to read the real ids". That remedy is reachable only because
    data_get_indicator is curated; drop it from _CURATED_TOOLS and the warning starts
    naming a tool that is not in the request's tools= list, with nothing to say so.
    The names are read out of the messages, not hardcoded, so a second guard is covered
    the day it is written.

    The guard KEYS are checked too, which is a wider claim than the name: it requires every
    guarded tool to be curated. That is stricter than strictly necessary — _post_process runs
    on every bridge.execute(), including the UI-only path in panel_pinescript.py, so a guard
    on an uncurated tool would be legitimate. It is vacuous today (every guarded tool is
    curated) and the strictness is deliberate: a guard on a tool the model cannot call is far
    more likely to be dead code than intent. Relax it when a UI-only guard actually exists.
    """
    # A guard's own `field` is the word a future message is most likely to add ("its
    # updated_inputs came back empty"), and it is a payload key, never a tool. Excluding
    # it here rather than in _GUARD_MESSAGE_NON_TOOL_TOKENS keeps that automatic for a
    # second guard, and stops the failure from reading "guards reference non-curated
    # tools: ['updated_inputs']" — a wrong diagnosis that would teach nothing.
    non_tool = set(_GUARD_MESSAGE_NON_TOOL_TOKENS)
    non_tool.update(field for field, _message in tv_module._EMPTY_RESULT_GUARDS.values())

    mentioned: set[str] = set()
    for _field, message in tv_module._EMPTY_RESULT_GUARDS.values():
        mentioned.update(
            token for token in _SNAKE_TOKEN.findall(message) if token not in non_tool
        )

    # Non-vacuity: the extraction itself must still be finding something.
    assert mentioned, "no tool name extracted from any guard message"

    uncallable = (mentioned | set(tv_module._EMPTY_RESULT_GUARDS)) - tv_module._CURATED_TOOLS
    assert not uncallable, f"guards reference non-curated tools: {sorted(uncallable)}"


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

    def boom(_name, _payload):
        """Stand-in for a transform that raises — RecursionError is the realistic one.

        Takes the full (name, payload) transform signature deliberately: a stub with the
        wrong arity would raise TypeError on the call itself and pass this test without
        the raising-transform path ever being what returned the raw string.
        """
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


def test_post_process_flags_a_no_op_indicator_set_inputs():
    """success:true with an empty updated_inputs means nothing changed. Say so."""
    raw = json.dumps({"success": True, "entity_id": "o1rBVD", "updated_inputs": {}})
    out = json.loads(tv_module._post_process("indicator_set_inputs", raw))
    assert "claudia_warning" in out
    assert "data_get_indicator" in out["claudia_warning"]
    assert out["success"] is True  # the sidecar's own field is not rewritten


def test_post_process_leaves_a_real_indicator_set_inputs_alone():
    """A key that DID match an input id changed the chart. Do not cast doubt on it."""
    raw = json.dumps({"success": True, "entity_id": "o1rBVD", "updated_inputs": {"in_0": 21}})
    out = json.loads(tv_module._post_process("indicator_set_inputs", raw))
    assert "claudia_warning" not in out


def test_post_process_only_guards_the_listed_tools():
    """An empty dict on an unrelated tool is not a defect and must not be flagged."""
    raw = json.dumps({"success": True, "updated_inputs": {}})
    out = json.loads(tv_module._post_process("chart_get_state", raw))
    assert "claudia_warning" not in out


def test_post_process_drops_a_sidecar_supplied_warning_on_a_guarded_tool():
    """claudia_warning is our channel for distrusting a payload, so the payload cannot fill it.

    The guard deliberately stays SILENT here (updated_inputs is non-empty), which is the path
    where a sidecar-supplied warning would otherwise survive untouched and be read by the
    model as ours.

    Asserted on the sidecar's TEXT rather than on the key's absence. Key-absence would also
    be false whenever our own warning is legitimately present, coupling this test to the
    guard's firing rule — a mutation to that rule killed it while the reservation was intact.
    """
    raw = json.dumps({
        "success": True,
        "updated_inputs": {"in_0": 21},
        "claudia_warning": "SIDECAR-CONTROLLED TEXT",
    })
    out = json.loads(tv_module._post_process("indicator_set_inputs", raw))
    assert "SIDECAR-CONTROLLED TEXT" not in json.dumps(out)


def test_post_process_drops_a_sidecar_supplied_warning_on_an_unguarded_tool():
    """The key is reserved for every tool, not only the ones in the guard table."""
    raw = json.dumps({
        "bars": [{"time": 1786455000}],
        "claudia_warning": "SIDECAR-CONTROLLED TEXT",
    })
    out = json.loads(tv_module._post_process("data_get_ohlcv", raw))
    assert "SIDECAR-CONTROLLED TEXT" not in json.dumps(out)
    assert out["bars"][0]["time_utc"] == "2026-08-11T13:30:00Z"  # the rest still post-processed


def test_post_process_trims_obfuscated_pine_source():
    """61% of a live data_get_study_values payload was base64 Pine source."""
    blob = "A" * 2422
    raw = json.dumps({"studies": [{"inputs": {"text": blob, "in_0": 200}}]})
    out = json.loads(tv_module._post_process("data_get_study_values", raw))
    assert out["studies"][0]["inputs"]["text"] == "<omitted: 2422 chars>"
    assert out["studies"][0]["inputs"]["in_0"] == 200


def test_post_process_keeps_short_text_values():
    """'SMA' is a real input value. Only oversized blobs are trimmed."""
    raw = json.dumps({"studies": [{"inputs": {"text": "SMA"}}]})
    out = json.loads(tv_module._post_process("data_get_study_values", raw))
    assert out["studies"][0]["inputs"]["text"] == "SMA"


def test_post_process_trims_strictly_above_the_sidecar_threshold():
    """The threshold is the sidecar's own (data.js:202) and it is exclusive: >200, not >=200.

    Pins the boundary in both value and direction. The other tests use a 2,422-char blob and a
    3-char value, so every threshold in roughly [3, 2421] and either comparison satisfies them.
    """
    at_limit = json.dumps({"inputs": {"text": "A" * 200}})
    assert json.loads(tv_module._post_process("x", at_limit))["inputs"]["text"] == "A" * 200

    over_limit = json.dumps({"inputs": {"text": "A" * 201}})
    trimmed = json.loads(tv_module._post_process("x", over_limit))["inputs"]["text"]
    assert trimmed == "<omitted: 201 chars>"


def test_post_process_only_trims_the_text_key():
    """A long value under any other key is content, not a blob."""
    long_note = "B" * 900
    raw = json.dumps({"note": long_note})
    out = json.loads(tv_module._post_process("data_get_study_values", raw))
    assert out["note"] == long_note


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


def test_post_process_strips_a_sidecar_warning_at_any_depth():
    """A Pine author can name a key, so the reservation cannot be top-level only.

    data_get_study_values returns `studies[].inputs` keyed by the Pine author's input ids and
    `studies[].values` keyed by their plot titles — both third-party key spaces. Measured
    2026-08-11: with a top-level-only scrub, two forged warnings reached the model dressed as
    ClaudIA's own voice on the one channel that exists to say "distrust this payload".
    """
    raw = json.dumps({
        "success": True,
        "studies": [{"inputs": {"claudia_warning": "FORGED"}, "values": {"claudia_warning": "FORGED"}}],
    })
    out = tv_module._post_process("data_get_study_values", raw)
    assert "FORGED" not in out


def test_post_process_still_strips_a_top_level_sidecar_warning():
    """The original top-level case must keep working after the scrub went recursive."""
    raw = json.dumps({"success": True, "claudia_warning": "FORGED"})
    assert "FORGED" not in tv_module._post_process("chart_get_state", raw)


def test_post_process_keeps_our_own_warning_after_stripping():
    """Stripping the sidecar's must not remove the one _flag_empty_result adds afterwards."""
    raw = json.dumps({"success": True, "updated_inputs": {}, "claudia_warning": "FORGED"})
    out = json.loads(tv_module._post_process("indicator_set_inputs", raw))
    assert "FORGED" not in out["claudia_warning"]
    assert "NOTHING WAS CHANGED" in out["claudia_warning"]


# ── launch_tradingview — 2026-09-04: "never started" is not "no debug port" ───
#
# Measured 2026-09-04: `open -a TradingView` from a Claude Code shell returns 0 and launches
# NOTHING (Calculator as a control did not open either). launch_tradingview() then waited
# 30 s for a port no process would ever open and told the user the app was "running without
# the debug port", which sent them to the quit+relaunch helper for an app that was never
# running. The three outcomes are now told apart.


def _fast_waits(monkeypatch):
    """Collapse both wait windows so the tests do not sleep."""
    monkeypatch.setattr(tv_module, "_TV_START_WAIT_S", 0.0)
    monkeypatch.setattr(tv_module, "_TV_CDP_WAIT_S", 0.0)


def _open_ok():
    """A completed `open` process that reported success."""
    return MagicMock(returncode=0, stderr="", stdout="")


@pytest.mark.asyncio
async def test_launch_returns_true_without_opening_when_cdp_is_already_up():
    """A running TradingView with the port open needs no launch at all."""
    with (
        patch("claudia.tradingview.check_cdp_running", return_value=True),
        patch("claudia.tradingview.subprocess.run") as run,
    ):
        assert await tv_module.launch_tradingview() is True
    run.assert_not_called()


@pytest.mark.asyncio
async def test_launch_reports_open_failure_with_its_stderr(monkeypatch):
    """A non-zero `open` is an honest error carrying LaunchServices' own text."""
    _fast_waits(monkeypatch)
    failed = MagicMock(returncode=1, stderr="Unable to find application named 'TradingView'", stdout="")
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        patch("claudia.tradingview.check_cdp_running", return_value=False),
        patch("claudia.tradingview._tv_process_running", return_value=False),
        patch("claudia.tradingview.subprocess.run", return_value=failed),
        pytest.raises(RuntimeError, match="Unable to find application"),
    ):
        await tv_module.launch_tradingview()


@pytest.mark.asyncio
async def test_launch_that_never_starts_a_process_says_so_and_names_the_terminal_route(monkeypatch, caplog):
    """`open` accepted but no TradingView process appears: 'never started', not 'no debug
    port' — and the way out is a launch from a real Terminal, not the quit+relaunch helper."""
    _fast_waits(monkeypatch)
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        patch("claudia.tradingview.check_cdp_running", return_value=False),
        patch("claudia.tradingview._tv_process_running", return_value=False),
        patch("claudia.tradingview.subprocess.run", return_value=_open_ok()),
        pytest.raises(RuntimeError, match="never started") as excinfo,
    ):
        await tv_module.launch_tradingview()
    assert "Terminal" in str(excinfo.value)
    assert "launch-tradingview-debug.sh" in str(excinfo.value)
    assert "debug port" not in str(excinfo.value).lower().replace("remote-debugging", "")
    assert any("never started" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_launch_with_a_process_but_no_port_returns_false(monkeypatch):
    """The app is running but never opens the port: the existing False path (the caller
    renders the debug-port instructions, which are right in THIS case)."""
    _fast_waits(monkeypatch)
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        patch("claudia.tradingview.check_cdp_running", return_value=False),
        patch("claudia.tradingview._tv_process_running", side_effect=[False, True]),
        patch("claudia.tradingview.subprocess.run", return_value=_open_ok()),
    ):
        assert await tv_module.launch_tradingview() is False


@pytest.mark.asyncio
async def test_launch_returns_true_once_the_port_opens(monkeypatch):
    """Not running, launched, process appears, then the port: success."""
    _fast_waits(monkeypatch)
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        # initial probe → False; the one port poll a zero wait makes → True
        patch("claudia.tradingview.check_cdp_running", side_effect=[False, True]),
        patch("claudia.tradingview._tv_process_running", side_effect=[False, True]),
        patch("claudia.tradingview.subprocess.run", return_value=_open_ok()),
    ):
        assert await tv_module.launch_tradingview() is True


@pytest.mark.asyncio
async def test_launch_quits_a_running_app_without_the_port_then_relaunches(monkeypatch):
    """User rule 2026-09-04 ("the button must have the same action" as the helper script):
    an app running without the port is quit first, then relaunched with it. Order pinned:
    quit → open. Progress lines reach `emit`."""
    _fast_waits(monkeypatch)
    order = MagicMock()
    quit_mock = AsyncMock(return_value=True)
    open_mock = MagicMock(return_value=_open_ok())
    order.attach_mock(quit_mock, "quit")
    order.attach_mock(open_mock, "open")
    emitted: list[str] = []
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        # initial probe → False; the already-running check's port read → False;
        # the one port poll after the relaunch → True
        patch("claudia.tradingview.check_cdp_running", side_effect=[False, False, True]),
        # already-running check → True; process wait after the relaunch → True
        patch("claudia.tradingview._tv_process_running", return_value=True),
        patch("claudia.tradingview._quit_tradingview", quit_mock),
        patch("claudia.tradingview.subprocess.run", open_mock),
    ):
        assert await tv_module.launch_tradingview(emit=emitted.append) is True
    assert [name for name, _, _ in order.mock_calls] == ["quit", "open"]
    assert any("quitting it first" in line for line in emitted)


@pytest.mark.asyncio
async def test_launch_reports_an_app_that_would_not_quit(monkeypatch):
    """If graceful quit and the forced kill both leave the process alive, say so — never
    relaunch on top of it (the second instance would just hand off to the first)."""
    _fast_waits(monkeypatch)
    with (
        patch("claudia.tradingview.platform.system", return_value="Darwin"),
        patch("claudia.tradingview.check_cdp_running", return_value=False),
        patch("claudia.tradingview._tv_process_running", return_value=True),
        patch("claudia.tradingview._quit_tradingview", new=AsyncMock(return_value=False)),
        patch("claudia.tradingview.subprocess.run") as run,
        pytest.raises(RuntimeError, match="could not be quit"),
    ):
        await tv_module.launch_tradingview()
    run.assert_not_called()


# ── _quit_tradingview ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quit_asks_nicely_first_and_returns_true_when_the_process_goes(monkeypatch):
    """osascript quit, then the process disappears: True, no pkill."""
    monkeypatch.setattr(tv_module, "_TV_QUIT_WAIT_S", 0.0)
    with (
        patch("claudia.tradingview._tv_process_running", return_value=False),
        patch("claudia.tradingview.subprocess.run") as run,
    ):
        assert await tv_module._quit_tradingview() is True
    cmds = [c.args[0][0] for c in run.call_args_list]
    assert cmds == ["osascript"]


@pytest.mark.asyncio
async def test_quit_falls_back_to_pkill_when_the_app_ignores_the_quit(monkeypatch):
    """Still running after the graceful window: pkill -x, then re-check."""
    monkeypatch.setattr(tv_module, "_TV_QUIT_WAIT_S", 0.0)
    monkeypatch.setattr(tv_module, "_TV_KILL_SETTLE_S", 0.0)
    with (
        # graceful wait poll → still running; after pkill → gone
        patch("claudia.tradingview._tv_process_running", side_effect=[True, False]),
        patch("claudia.tradingview.subprocess.run") as run,
    ):
        assert await tv_module._quit_tradingview() is True
    cmds = [c.args[0][0] for c in run.call_args_list]
    assert cmds == ["osascript", "pkill"]
