"""CLA-SEC-003 — proposing is a declaration, and CLA-SEC-004 — its values are never repaired.

Audit 2026-09-13, weaknesses B-3 and the structural half of B-4. `propose_order`,
`propose_cancel` and `propose_modify` are the only order-shaped things the model can call.
They reach nothing: the handler checks, records and returns a string. That was true and
guarded by a test named `test_proposal_handlers_cannot_reach_execution` — which
substring-searches `proposal_tools.py`, the module that holds the JSON schemas, not
`agent.py`, which holds the handler. The name promised the guarantee; the body read the
wrong file.

The second rule is the one the order-parameter policy rests on: validation rejects whole
and never repairs. A `setdefault("tif", "DAY")` in the defect check would be a fabricated
order parameter, and the three behavioural tests that pin immutability each drive a single
payload — they cannot see a repair on a path they do not exercise.
"""

from __future__ import annotations

from tests.security.structural import (
    PACKAGE_DIR,
    called_names,
    dict_mutations,
    function_named,
    self_collaborators,
)

AGENT = (PACKAGE_DIR / "agent.py").read_text(encoding="utf-8")
RENDERER = (PACKAGE_DIR / "panel_order_flow.py").read_text(encoding="utf-8")

# Anything that leaves the process, blocks, or starts work elsewhere. `_record_proposal`'s
# whole contract is that none of this happens before a human has seen the proposal.
IO_CALL_NAMES = frozenset(
    {
        "IBKRClient",
        "execute",
        "get",  # requests.get / httpx.get — see the note in the test about dict.get
        "post",
        "delete",
        "put",
        "request",
        "urlopen",
        "connect",
        "create_connection",
        "to_thread",
        "create_task",
        "run",
        "Popen",
        "check_output",
        "open",
        "add_message",
        "add_decision",
    }
)

# `dict.get` is the single collision between reading a proposal and making an HTTP request.
# Both handlers read their inputs with `.get`, so it is excluded by name and the absence of
# an HTTP client is covered by the collaborator rule and by CLA-SEC-001's name rule instead.
_DICT_READ = frozenset({"get"})


def test_the_proposal_handler_touches_only_the_turn_it_is_recording():
    """`_record_proposal` reads the turn's tool set and writes the pending slot. That is all.

    Not "does no I/O in practice" — the collaborator list IS the contract. A store write, a
    sink call or a toolkit call would each appear here as a new name.
    """
    reached = self_collaborators(function_named(AGENT, "_record_proposal"))
    assert reached == {"_pending_proposal", "_called_tools_this_turn"}


def test_the_defect_check_reaches_nothing_at_all():
    """`_proposal_defect` is a pure function over the input dict: no `self`, no I/O."""
    fn = function_named(AGENT, "_proposal_defect")
    assert self_collaborators(fn) == set()
    assert not (called_names(fn) & (IO_CALL_NAMES - _DICT_READ))


def test_neither_proposal_function_performs_io():
    """Both handlers are free of every call that would leave the process."""
    offenders = {
        name: sorted(called_names(function_named(AGENT, name)) & (IO_CALL_NAMES - _DICT_READ))
        for name in ("_record_proposal", "_proposal_defect")
        if called_names(function_named(AGENT, name)) & (IO_CALL_NAMES - _DICT_READ)
    }
    assert not offenders, f"a proposal handler reaches out: {offenders}"


def test_no_proposal_function_writes_to_the_proposal():
    """Reject whole, never repair — including in the renderers that read it afterwards.

    `render_*_proposal` takes its own deepcopy (B-4) and must not write to either the copy
    or the caller's dict: a normalisation there would change what executes without changing
    what the card was built from.
    """
    offenders: dict[str, set[str]] = {}
    for source, names in ((AGENT, ("inputs",)), (RENDERER, ("proposal",))):
        for fn_name in (
            "_record_proposal",
            "_proposal_defect",
            "render_order_proposal",
            "render_cancel_proposal",
            "render_modify_proposal",
        ):
            try:
                fn = function_named(source, fn_name)
            except KeyError:
                continue
            writes = dict_mutations(fn, names) | dict_mutations(fn, ("inputs", "proposal"))
            if writes:
                offenders[fn_name] = writes
    assert not offenders, f"an order proposal was written to: {offenders}"


# ── Guards on the guards ─────────────────────────────────────────────────────────────────


def test_the_collaborator_probe_sees_a_store_write():
    snippet = (
        "class A:\n"
        "    def _record_proposal(self, name, inputs):\n"
        "        self._store.add_decision(session_id=self._session_id)\n"
        "        self._pending_proposal = (name, inputs)\n"
    )
    assert self_collaborators(function_named(snippet, "_record_proposal")) == {
        "_store",
        "_session_id",
        "_pending_proposal",
    }


def test_the_io_probe_sees_a_preview_call():
    snippet = (
        "class A:\n"
        "    def _record_proposal(self, name, inputs):\n"
        "        self._toolkit.execute('preview_order', inputs)\n"
    )
    fn = function_named(snippet, "_record_proposal")
    assert called_names(fn) & (IO_CALL_NAMES - _DICT_READ) == {"execute"}


def test_the_mutation_probe_sees_every_shape_of_repair():
    snippet = (
        "def _proposal_defect(kind, inputs):\n"
        "    inputs.setdefault('tif', 'DAY')\n"
        "    inputs['quantity'] = abs(inputs['quantity'])\n"
        "    inputs.update(sec_type='STK')\n"
        "    del inputs['reason']\n"
        "    return None\n"
    )
    assert dict_mutations(function_named(snippet, "_proposal_defect"), ("inputs",)) == {
        "inputs.setdefault()",
        "inputs[…] =",
        "inputs.update()",
        "del inputs[…]",
    }


def test_the_mutation_probe_ignores_a_read():
    snippet = "def f(inputs):\n    return inputs.get('symbol'), inputs['quantity']\n"
    assert not dict_mutations(function_named(snippet, "f"), ("inputs",))
