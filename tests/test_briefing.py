"""Startup briefing builders and renderer.

No test here touches the network — the builders take plain data by design, which is the
same property that makes them safe to call during session start.
"""

from __future__ import annotations

import dataclasses
from datetime import date

from claudia import briefing as br
from claudia.contract_identity import ContractIdentity


def test_closures_lists_only_exchanges_closed_today() -> None:
    """Only today's closures, labelled from the existing 20-entry map."""
    mkt = {
        "holidays_by_exchange": {
            "XTKS": ["2026-09-21", "2026-09-23"],
            "XMEX": ["2026-09-16"],
            "XNYS": ["2026-11-26"],
        }
    }
    out = br.build_closures(mkt, today=date(2026, 9, 21))
    assert isinstance(out, br.Ready)
    assert [c.code for c in out.items] == ["XTKS"]
    assert out.items[0].label == "TSE Tokyo"


def test_closures_ready_and_empty_when_nothing_is_closed() -> None:
    """Measured 2026-09-15: no tracked exchange was closed. That is a POSITIVE result
    and must be Ready(()), never Unavailable."""
    mkt = {"holidays_by_exchange": {"XTKS": ["2026-09-21"]}}
    out = br.build_closures(mkt, today=date(2026, 9, 15))
    assert out == br.Ready(items=())


def test_closures_unavailable_when_the_calendar_is_missing() -> None:
    """No calendar is a failed read, and must never read as "nothing closed"."""
    out = br.build_closures(None, today=date(2026, 9, 15))
    assert isinstance(out, br.Unavailable)
    assert "calendar" in out.reason.lower()


def test_closures_unavailable_when_the_calendar_lacks_its_key() -> None:
    """A dict that arrived but carries no holiday map is a failed read, not a quiet day."""
    out = br.build_closures({}, today=date(2026, 9, 15))
    assert isinstance(out, br.Unavailable)


def test_closures_skips_non_string_keys_without_crashing_on_the_sort() -> None:
    """A malformed key must not crash the whole builder via `sorted()`.

    The filter that drops non-string keys has to run BEFORE `sorted()`, not merely
    guard the loop body: `sorted()` compares every entry against every other entry up
    front, so a single `int`/`str` (or `None`/`str`) key pair anywhere in the dict raises
    `TypeError` before the first well-formed entry is even reached — an in-loop
    `isinstance` continue placed after the sort call is unreachable and protects
    nothing. This is the exact mixed-key shape that reproduced the crash.
    """
    mkt = {
        "holidays_by_exchange": {
            "XTKS": ["2026-09-21"],
            1: ["2026-09-21"],
            None: [],
        }
    }
    out = br.build_closures(mkt, today=date(2026, 9, 21))
    assert isinstance(out, br.Ready)
    assert [c.code for c in out.items] == ["XTKS"]


class _Row:
    """Minimal stand-in for dashboard_data.Position — only what the builder reads."""

    def __init__(
        self,
        symbol: str,
        description: str,
        quantity: float,
        expiry: str | None,
        asset_class: str = "FUT",
        conid: int = 0,
    ) -> None:
        """Build a row with only the fields `build_expiries` reads.

        `conid` defaults to 0 so every pre-existing call site in this file — none of
        which cares about contract identity — keeps working unmodified. THIS IS A
        LATENT TRAP, not a safe default in general: every `_Row()` built without an
        explicit `conid` shares the same `0`, so a test that builds two or more such
        rows AND passes a non-empty `identities` mapping (e.g. `identities={0: ...}`)
        would have that one identity silently resolve for every row, producing a
        false pass. It is inert today only because no test in this file does both at
        once. Any test exercising `identities` with more than one row must give each
        row its own distinct, explicit `conid`.
        """
        self.symbol = symbol
        self.description = description
        self.quantity = quantity
        self.expiry = expiry
        self.asset_class = asset_class
        self.conid = conid


def test_expiries_reports_a_future_inside_the_horizon() -> None:
    """The real 2026-09-15 case: ES Sep expiring on the 18th, three days out."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918")]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ES"
    assert item.expiry == date(2026, 9, 18)
    assert item.days_left == 3


def test_expiries_excludes_a_future_beyond_the_horizon() -> None:
    """Dec is months away and would be permanent noise in the briefing. This does NOT pin
    the horizon_days boundary despite the name — Dec is months out, not one day past the
    cutoff. See test_expiries_excludes_a_contract_one_day_past_the_horizon for that."""
    rows = [_Row("ES", "ES       DEC2026", -1.0, "20261218")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_includes_a_contract_expiring_today() -> None:
    """Pins the lower boundary: days_left == 0 (expires TODAY) must be INCLUDED — this is
    exactly the day a roll warning matters most."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260915")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.days_left == 0


def test_expiries_includes_a_contract_exactly_at_the_horizon() -> None:
    """Pins the inclusive upper boundary: days_left == horizon_days (7) must be INCLUDED."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260922")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.days_left == 7


def test_expiries_excludes_a_contract_one_day_past_the_horizon() -> None:
    """Pins the off-by-one just past the boundary: days_left == horizon_days + 1 (8) must
    be EXCLUDED. This is the case test_expiries_excludes_a_future_beyond_the_horizon's
    December example does not actually exercise."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260923")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert out == br.Ready(items=())


def test_expiries_excludes_a_contract_that_expired_yesterday() -> None:
    """Pins the lower exclusion: days_left == -1 (expired YESTERDAY, still non-flat in the
    payload) must be EXCLUDED — a briefing reports what's ahead, not what already lapsed."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260914")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert out == br.Ready(items=())


def test_expiries_sorts_by_days_left_then_symbol() -> None:
    """Nearest deadline first; a tie on days_left breaks by symbol. Pins the actual sort
    (every other test yields at most one item, so the sort itself was never exercised
    before this): [NQ@5, ES@1, CL@3, ZB@1] -> [ES(1), ZB(1), CL(3), NQ(5)]."""
    rows = [
        _Row("NQ", "NQ       SEP2026", -1.0, "20260920"),  # days_left 5
        _Row("ES", "ES       SEP2026", -1.0, "20260916"),  # days_left 1
        _Row("CL", "CL       OCT2026", -1.0, "20260918"),  # days_left 3
        _Row("ZB", "ZB       SEP2026", -1.0, "20260916"),  # days_left 1, tie with ES
    ]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    assert [c.symbol for c in out.items] == ["ES", "ZB", "CL", "NQ"]
    assert [c.days_left for c in out.items] == [1, 1, 3, 5]


def test_expiries_excludes_a_flat_row() -> None:
    """IBKR keeps a closed contract in the payload at position 0.0 (measured 2026-09-15,
    both ES conids read 0.0 after the round trips). A flat book expires nothing."""
    rows = [_Row("ES", "ES       SEP2026", 0.0, "20260918")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_skips_a_non_finite_quantity() -> None:
    """ "Flat" means quantity == 0 exactly, not any falsy value. A NaN quantity is a
    distinct defect from being flat — it cannot arise from IBKR's real payload as far as
    observed, but a size the operator cannot act on must not render as a position, so it
    is skipped the same way an unparseable expiry is."""
    rows = [_Row("ES", "ES       SEP2026", float("nan"), "20260918")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_ignores_instruments_without_an_expiry() -> None:
    """A stock has no expiry at all, so it can never enter this section."""
    rows = [_Row("IGV", "IGV", 100.0, None, asset_class="STK")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_ignores_a_stock_even_with_a_non_empty_identities_mapping() -> None:
    """Belt-and-braces on the previous test: the claim "a stock row is unaffected by
    identities" currently holds only by call order — the expiry-parse filter runs
    before the identity lookup ever sees the row, since a row with no expiry is
    `continue`d out of the loop first. Make that explicit rather than incidental: a
    STK row with no expiry is still excluded even when a real, non-empty identities
    mapping is in play (the mapping just happens to key on a different conid, as a
    real futures-only identities map always would for a stock's conid)."""
    identity = ContractIdentity(
        conid=495512563,
        local_symbol="ESU6",
        month="SEP26",
        expires="2026-09-18",
        name="E-mini S&P 500",
        multiplier=50.0,
        currency=None,
    )
    rows = [_Row("IGV", "IGV", 100.0, None, asset_class="STK", conid=1)]
    out = br.build_expiries(rows, today=date(2026, 9, 15), identities={495512563: identity})
    assert out == br.Ready(items=())


def test_expiries_degraded_when_positions_have_not_been_polled() -> None:
    """The poller may not have published its first snapshot yet. That is not "nothing
    expiring" — it is "we have not looked"."""
    out = br.build_expiries(None, today=date(2026, 9, 15))
    assert isinstance(out, br.Degraded)
    assert "polled" in out.reason.lower()


def test_expiries_skips_an_unparseable_expiry_without_failing_the_section() -> None:
    """One bad row must not take the whole section down — the others are still true."""
    rows = [
        _Row("ES", "ES       SEP2026", -1.0, "not-a-date"),
        _Row("ES", "ES       SEP2026", -1.0, "20260918"),
    ]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    assert len(out.items) == 1


def test_expiries_is_not_keyed_on_sec_type() -> None:
    """Scope ruling 2026-09-15 is futures-first, but the DATA PATH must stay
    instrument-agnostic so options later are a predicate change, not a rewrite.
    An option row carrying an expiry must therefore be picked up by the builder."""
    rows = [_Row("SPY", "SPY 19SEP26 500 C", 1.0, "20260918", asset_class="OPT")]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    assert len(out.items) == 1


def test_expiries_names_a_future_using_its_identity_when_present() -> None:
    """Design 2026-09-15 (gap #37 applied to the briefing): a futures row whose conid
    resolves against the identities map renders IB's own local symbol and long name —
    `ESU6` / `E-mini S&P 500` — not the raw ticker and IBKR's padded `contractDesc`."""
    identity = ContractIdentity(
        conid=495512563,
        local_symbol="ESU6",
        month="SEP26",
        expires="2026-09-18",
        name="E-mini S&P 500",
        multiplier=50.0,
        currency=None,
    )
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918", conid=495512563)]
    out = br.build_expiries(rows, today=date(2026, 9, 15), identities={495512563: identity})
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ESU6"
    assert item.description == "E-mini S&P 500"


def test_expiries_falls_back_to_the_raw_row_when_no_identities_are_supplied() -> None:
    """`identities` omitted entirely (the pre-existing call shape) must still fall back to
    the row's own symbol/description — fail-soft, never a blanked or dropped row."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918", conid=495512563)]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ES"
    assert item.description == "ES       SEP2026"


def test_expiries_falls_back_per_field_when_the_identity_has_no_company_name() -> None:
    """Code review finding: `parse_contract_info` only guards `local_symbol` against
    being empty — `name` (IBKR's `company_name`) is not guaranteed non-blank, so a real,
    resolvable contract can carry a good `local_symbol` next to `name=""`. Falling back
    per IDENTITY (all-or-nothing) would render `ESU6 ()`; the fallback must be per
    FIELD, so `local_symbol` still resolves to `ESU6` while `description` falls back to
    the row's own `description` rather than rendering blank."""
    identity = ContractIdentity(
        conid=495512563,
        local_symbol="ESU6",
        month="SEP26",
        expires="2026-09-18",
        name="",
        multiplier=50.0,
        currency=None,
    )
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918", conid=495512563)]
    out = br.build_expiries(rows, today=date(2026, 9, 15), identities={495512563: identity})
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ESU6"
    assert item.description == "ES       SEP2026"


def test_expiries_falls_back_when_the_conid_is_absent_from_the_mapping() -> None:
    """The mapping exists and is non-empty, but carries no entry for THIS row's conid —
    an identity read that failed for this contract, or an identity for a different one.
    Must fall back exactly like a missing mapping, never render blank or a wrong name."""
    other = ContractIdentity(
        conid=999,
        local_symbol="NQZ6",
        month="DEC26",
        expires="2026-12-18",
        name="E-mini Nasdaq-100",
        multiplier=20.0,
        currency=None,
    )
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918", conid=495512563)]
    out = br.build_expiries(rows, today=date(2026, 9, 15), identities={999: other})
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ES"
    assert item.description == "ES       SEP2026"


def test_expiries_identities_none_behaves_exactly_like_an_empty_mapping() -> None:
    """`identities=None` and `identities={}` must be indistinguishable to the caller —
    the poller's first snapshot before any futures are on the book looks like the former,
    a healthy poll with only stocks open looks like the latter, and both mean the same
    thing to this builder: nothing to resolve against."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918", conid=495512563)]
    none_out = br.build_expiries(rows, today=date(2026, 9, 15), identities=None)
    empty_out = br.build_expiries(rows, today=date(2026, 9, 15), identities={})
    assert none_out == empty_out


def test_a_real_position_and_identity_reach_the_rendered_text_with_the_local_symbol() -> None:
    """End to end through REAL types, not hand-written doubles for either: a real
    `dashboard_data.Position` keyed by a real conid, resolved against a real
    `ContractIdentity`, through `positions_for_briefing` -> `build_expiries` ->
    `render_briefing`. Closes gap #37 on the briefing surface the same way it is already
    closed on Positions/Orders/proposal cards/Gate 2."""
    from datetime import UTC, datetime

    from claudia.dashboard_data import DashboardSnapshot, Position

    pos = Position(
        conid=495512563,
        symbol="ES",
        description="ES       SEP2026",
        asset_class="FUT",
        quantity=-1.0,
        average_cost=450000.0,
        market_price=4500.0,
        market_value=900000.0,
        unrealised_pnl=0.0,
        realised_pnl=0.0,
        currency="USD",
        expiry="20260918",
    )
    identity = ContractIdentity(
        conid=495512563,
        local_symbol="ESU6",
        month="SEP26",
        expires="2026-09-18",
        name="E-mini S&P 500",
        multiplier=50.0,
        currency=None,
    )
    snap = DashboardSnapshot(
        as_of=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        positions=(pos,),
        identities={495512563: identity},
    )

    positions = br.positions_for_briefing(snap)
    section = br.build_expiries(positions, today=date(2026, 9, 15), identities=snap.identities)
    text = br.render_briefing(
        br.Briefing(expiries=section, closures=br.Ready(items=())), escape=str
    )

    assert "ESU6" in text
    assert "E-mini S&P 500" in text
    assert "ES       SEP2026" not in text


def test_render_names_the_expiring_contract_and_the_closed_exchange() -> None:
    """The happy path names the contract, the date, the days left and the exchange."""
    b = br.Briefing(
        expiries=br.Ready(
            items=(
                br.ExpiringContract(
                    symbol="ES",
                    description="ES       SEP2026",
                    expiry=date(2026, 9, 18),
                    days_left=3,
                    quantity=-1.0,
                ),
            )
        ),
        closures=br.Ready(items=(br.ClosedExchange(code="XTKS", label="TSE Tokyo"),)),
    )
    text = br.render_briefing(b, escape=str)
    assert "ES" in text
    assert "2026-09-18" in text
    assert "3 days" in text
    assert "TSE Tokyo" in text


def test_render_formats_quantity_without_scientific_notation_or_rounding() -> None:
    """Code review 2026-09-15: `f'{-1234567.0:+g}'` renders `'-1.23457e+06'` — 6
    significant figures then scientific notation, unreadable and imprecise for a
    trader-facing number. The builder is deliberately instrument-agnostic (options/stocks
    are "a predicate change rather than a rewrite" per its docstring), and a six-figure
    share position is ordinary even though no futures test ever exercises more than one
    contract. Every quantity must render comma-grouped, in fixed-point, with its exact
    fraction kept when it has one.
    """
    cases = {
        -1.0: "-1",
        2.0: "+2",
        0.5: "+0.5",
        1000000.0: "+1,000,000",
        -1234567.0: "-1,234,567",
        1.5: "+1.5",
    }
    for quantity, expected in cases.items():
        b = br.Briefing(
            expiries=br.Ready(
                items=(
                    br.ExpiringContract(
                        symbol="ES",
                        description="ES       SEP2026",
                        expiry=date(2026, 9, 18),
                        days_left=3,
                        quantity=quantity,
                    ),
                )
            ),
            closures=br.Ready(items=()),
        )
        text = br.render_briefing(b, escape=str)
        assert f"— {expected} —" in text, f"quantity {quantity}: expected {expected!r} in {text!r}"


def test_render_singularises_one_day_left() -> None:
    """`f"{c.days_left} days"` renders "1 days" for a same-day-tomorrow expiry — a UI-feel
    defect the operator called out (code review 2026-09-15). days_left == 1 must read
    "1 day"."""
    b = br.Briefing(
        expiries=br.Ready(
            items=(
                br.ExpiringContract(
                    symbol="ES",
                    description="ES       SEP2026",
                    expiry=date(2026, 9, 16),
                    days_left=1,
                    quantity=-1.0,
                ),
            )
        ),
        closures=br.Ready(items=()),
    )
    text = br.render_briefing(b, escape=str)
    assert "1 day" in text
    assert "1 days" not in text


def test_render_says_plainly_when_there_is_genuinely_nothing() -> None:
    """A clean read that found nothing says so, and says nothing about availability."""
    b = br.Briefing(expiries=br.Ready(items=()), closures=br.Ready(items=()))
    text = br.render_briefing(b, escape=str)
    assert "No position expiring" in text
    assert "All tracked exchanges open" in text
    assert "unavailable" not in text.lower()


def test_ibkr_supplied_strings_are_escaped_not_rendered_as_markup() -> None:
    """The §10 canary. `symbol` and `description` come from IBKR, not from us, and this
    text lands in a Markdown chat message. Neither field has ever carried markup in
    practice — the control is that it would not matter if one did.
    """
    from claudia.panel_markdown import escape_markup

    b = br.Briefing(
        expiries=br.Ready(
            items=(
                br.ExpiringContract(
                    symbol="<img src=x onerror=alert(1)>",
                    description="ES <script>alert(1)</script>",
                    expiry=date(2026, 9, 18),
                    days_left=3,
                    quantity=-1.0,
                ),
            )
        ),
        closures=br.Ready(items=()),
    )
    text = br.render_briefing(b, escape=escape_markup)
    assert "<script>" not in text
    assert "<img" not in text
    assert "alert(1)" in text, "escaped, not deleted — the operator still sees the value"


def test_closure_labels_are_escaped_too() -> None:
    """Code review 2026-09-15: every label in play today traces to `EXCHANGE_LABELS` or
    its own-literal fallback, never to IBKR — but nothing pins that, and the cost of
    escaping a label unconditionally is zero. `escape` must reach `_render_closures`, not
    just `_render_expiries`.
    """
    from claudia.panel_markdown import escape_markup

    b = br.Briefing(
        expiries=br.Ready(items=()),
        closures=br.Ready(items=(br.ClosedExchange(code="XTKS", label="<b>TSE Tokyo</b>"),)),
    )
    text = br.render_briefing(b, escape=escape_markup)
    assert "<b>" not in text
    assert "TSE Tokyo" in text


def test_a_degraded_section_is_never_rendered_as_an_empty_one() -> None:
    """THE invariant, asserted over EVERY section on Briefing rather than one of them.

    Hard rule 2 (operator, 2026-09-15): a failed read must never read as "nothing today" —
    called the worst possible outcome. The check is structural, so a third section added
    later is covered without anyone remembering to extend this test.
    """
    empty = br.Briefing(expiries=br.Ready(items=()), closures=br.Ready(items=()))
    fields = dataclasses.fields(br.Briefing)
    assert fields, "Briefing has no sections — the invariant would be vacuous"

    # The positive sentence each section emits when it looked and genuinely found nothing.
    # A degraded section of that name must never produce its own sentence.
    positive = {"expiries": "No position expiring", "closures": "All tracked exchanges open"}
    assert set(positive) == {f.name for f in fields}, (
        "a section was added or renamed — give it its positive sentence here so the "
        "invariant keeps covering every section"
    )

    for field in fields:
        for bad in (br.Degraded(reason="poller asleep"), br.Unavailable(reason="no calendar")):
            b = dataclasses.replace(empty, **{field.name: bad})
            text = br.render_briefing(b, escape=str)
            assert "⚠" in text, f"{field.name}={type(bad).__name__} rendered no warning"
            assert bad.reason in text, f"{field.name}: the reason was dropped"
            assert positive[field.name] not in text, (
                f"{field.name}={type(bad).__name__} rendered as a positive empty result — "
                "this is hard rule 2 violated"
            )


class _Snap:
    """Stand-in for DashboardSnapshot — only the three fields BriefingSnapshot declares."""

    def __init__(self, positions: tuple[_Row, ...], error: str | None = None) -> None:
        """Build a snapshot double; `error` is what the guard keys on. `identities`
        defaults empty — none of these tests exercise contract naming, only the guard."""
        self.positions = positions
        self.error = error
        self.identities: dict[int, ContractIdentity] = {}


def test_guard_returns_none_when_the_poller_has_not_polled() -> None:
    """The poller serves empty_snapshot(error="Dashboard has not polled yet.") before its
    first poll — positions is () but nothing was read. Passing that () through would
    render "No position expiring", which is hard rule 2 violated in the wiring."""
    snap = _Snap(positions=(), error="Dashboard has not polled yet.")
    assert br.positions_for_briefing(snap) is None


def test_guard_returns_none_when_the_ibkr_half_failed() -> None:
    """A failed IBKR poll is not an empty book; positions must not be trusted."""
    snap = _Snap(positions=(), error="IBKR session not authenticated (401)")
    assert br.positions_for_briefing(snap) is None


def test_guard_returns_none_when_there_is_no_poller() -> None:
    """The very first session has no poller yet."""
    assert br.positions_for_briefing(None) is None


def test_guard_passes_positions_through_on_a_healthy_snapshot() -> None:
    """A clean poll that found nothing open is a POSITIVE result and must reach the
    builder as an empty sequence, not as None."""
    snap = _Snap(positions=(), error=None)
    assert br.positions_for_briefing(snap) == ()


def test_guard_returns_none_when_stale_positions_are_republished_alongside_an_error() -> None:
    """The realistic degraded shape is non-empty positions WITH an error, not empty ones.

    `DashboardPoller._publish_stale` (claudia/dashboard_poller.py) republishes the
    PREVIOUS snapshot's positions unaged when a mid-session poll fails, and attaches the
    new `error` to that same snapshot — it does not blank `positions` to `()`. So the
    runtime shape of "the account half just failed" is a snapshot carrying real, stale
    position rows next to a non-None `error`, not the empty-tuple case every other guard
    test here exercises.

    A guard written as `error is not None and not snapshot.positions` would pass every
    other test in this module — they all build `_Snap(positions=(), error=...)` — while
    silently falling through to `return snapshot.positions` whenever positions are
    non-empty, leaking stale rows into the briefing as if they were freshly polled. That
    is precisely the failure hard rule 2 exists to prevent: presenting unreliable data as
    reliable. This test pins the guard against that specific wrong shape by giving it a
    near-expiry row alongside the error, the same rows `_publish_stale` would carry
    forward from a healthy poll two ticks earlier.
    """
    stale_row = _Row(
        symbol="ESZ6",
        description="E-mini S&P 500",
        quantity=2.0,
        expiry="20260918",
    )
    snap = _Snap(positions=(stale_row,), error="IBKR read failed")

    assert br.positions_for_briefing(snap) is None

    text = br.render_briefing(
        br.Briefing(
            expiries=br.build_expiries(br.positions_for_briefing(snap), today=date(2026, 9, 15)),
            closures=br.Ready(items=()),
        ),
        escape=str,
    )
    assert "⚠" in text
    assert "ESZ6" not in text


def test_degraded_reason_from_the_guard_survives_into_the_render() -> None:
    """End to end for the rule: unpolled snapshot in, warning out — never "nothing"."""
    snap = _Snap(positions=(), error="Dashboard has not polled yet.")
    text = br.render_briefing(
        br.Briefing(
            expiries=br.build_expiries(br.positions_for_briefing(snap), today=date(2026, 9, 15)),
            closures=br.Ready(items=()),
        ),
        escape=str,
    )
    assert "⚠" in text
    assert "No position expiring" not in text


def test_the_real_snapshot_type_satisfies_the_guard_protocol() -> None:
    """The guard must accept the ACTUAL DashboardSnapshot, not only the double.

    DashboardSnapshot is a frozen dataclass; a Protocol declaring a plain mutable
    attribute would not be satisfied by it, and Task 6 is where that would blow up.
    Pinning it here means the failure lands in this task, not in the wiring.
    """
    from claudia.dashboard_data import empty_snapshot

    snap = empty_snapshot(error="Dashboard has not polled yet.")
    assert br.positions_for_briefing(snap) is None


def test_a_real_position_survives_the_whole_chain_to_the_rendered_text() -> None:
    """The positive path, pinned against the REAL frozen `Position`, not `_Row`.

    Every other positive-path test in this file (build_expiries, render_briefing) uses
    `_Row`, a plain mutable class that is structurally EASIER to satisfy than the real,
    frozen `dashboard_data.Position` — that gap is exactly how the `ExpiringRow` mismatch
    survived: every test stayed green on the double while the real chain would have
    failed mypy. `test_the_real_snapshot_type_satisfies_the_guard_protocol` above only
    exercises the real type on the empty/failure path (`positions == ()`), so it cannot
    catch a rename or type drift on `Position.symbol`/`.description`/`.quantity`/
    `.expiry` — a field that stopped matching `ExpiringRow` would leave every `positions=()`
    test green while a real, non-empty snapshot broke. This test closes that hole: a real
    `Position` in a real `DashboardSnapshot`, run through the full
    `positions_for_briefing` -> `build_expiries` -> `render_briefing` chain, asserting the
    contract actually appears in the rendered text.
    """
    from datetime import UTC, datetime

    from claudia.dashboard_data import DashboardSnapshot, Position

    pos = Position(
        conid=495512563,
        symbol="ESZ6",
        description="E-mini S&P 500",
        asset_class="FUT",
        quantity=2.0,
        average_cost=450000.0,
        market_price=4500.0,
        market_value=900000.0,
        unrealised_pnl=0.0,
        realised_pnl=0.0,
        currency="USD",
        expiry="20260918",
    )
    snap = DashboardSnapshot(as_of=datetime(2026, 9, 15, 12, 0, tzinfo=UTC), positions=(pos,))

    out = br.positions_for_briefing(snap)
    section = br.build_expiries(out, today=date(2026, 9, 15))
    text = br.render_briefing(
        br.Briefing(expiries=section, closures=br.Ready(items=())), escape=str
    )

    assert "**Expiring soon:**" in text
    assert "ESZ6" in text
    assert "2026-09-18" in text


# ── Weekend: the regular schedule comes first (operator 2026-09-26, gap #78) ───────────

_SATURDAY = date(2026, 9, 26)
_SUNDAY = date(2026, 9, 27)
_GLOBEX = {
    "futures": {
        "product_groups": {"equity_index": {"globex_hours_ct": "Sun 5:00 PM – Fri 4:00 PM"}}
    }
}


def test_a_saturday_is_a_weekend_closure_never_an_open_day() -> None:
    """2026-09-26, a Saturday: the section printed "All tracked exchanges open today",
    because a weekend is in no holiday list — the core builds the lists from weekdays.
    Operator: regular schedule first, holidays only subtract; no exchange opens on its
    weekend. So a weekend day is a `Weekend`, whatever the lists say."""
    mkt = {"holidays_by_exchange": {"XTKS": ["2026-09-21"]}, **_GLOBEX}
    out = br.build_closures(mkt, today=_SATURDAY)
    assert isinstance(out, br.Weekend)
    assert out.day == "Saturday"
    assert out.open_today == ()


def test_a_sunday_names_the_exchange_whose_regular_week_includes_it() -> None:
    """Tadawul trades Sun–Thu (exchange_calendars' XSAU calendar, the core's own source):
    on a Sunday it is the one tracked exchange with a session, and it is named."""
    out = br.build_closures({"holidays_by_exchange": {}}, today=_SUNDAY)
    assert isinstance(out, br.Weekend)
    assert out.day == "Sunday"
    assert out.open_today == ("Tadawul (Sun–Thu week)",)


def test_the_weekend_verdict_needs_no_calendar() -> None:
    """The weekly schedule is known from the date alone; an unreadable calendar costs
    the Globex hours line and nothing else — never an "unavailable" on a Saturday."""
    out = br.build_closures(None, today=_SATURDAY)
    assert isinstance(out, br.Weekend)
    assert out.globex_hours is None


def test_the_globex_hours_are_quoted_from_the_context_not_copied() -> None:
    """CME's schedule string reaches the briefing from the core's `_FUTURES_SCHEDULE`
    through the context; a context without it yields no hours, never a copy typed here."""
    with_hours = br.build_closures({"holidays_by_exchange": {}, **_GLOBEX}, today=_SATURDAY)
    without = br.build_closures({"holidays_by_exchange": {}}, today=_SATURDAY)
    assert isinstance(with_hours, br.Weekend) and isinstance(without, br.Weekend)
    assert with_hours.globex_hours == "Sun 5:00 PM – Fri 4:00 PM"
    assert without.globex_hours is None


def test_a_weekday_still_gets_the_holiday_verdict() -> None:
    """Regular schedule first, holidays second: on a weekday the lists decide as before."""
    out = br.build_closures(
        {"holidays_by_exchange": {"XTKS": ["2026-09-21"]}}, today=date(2026, 9, 21)
    )
    assert isinstance(out, br.Ready)
    assert [c.code for c in out.items] == ["XTKS"]


def test_a_saturday_renders_as_a_weekend_closure() -> None:
    """The sentence the operator reads on a Saturday: closed on the regular schedule,
    with CME's own hours quoted — and never the weekday "open today" sentence."""
    b = br.Briefing(
        expiries=br.Ready(items=()),
        closures=br.Weekend(
            day="Saturday", open_today=(), globex_hours="Sun 5:00 PM – Fri 4:00 PM"
        ),
    )
    text = br.render_briefing(b, escape=str)
    assert "Saturday — weekend" in text
    assert "every tracked exchange closed" in text
    assert "Sun 5:00 PM – Fri 4:00 PM CT" in text
    assert "open today" not in text


def test_a_sunday_renders_its_exception_and_the_globex_reopen() -> None:
    """Sunday is a session day for Tadawul and the evening Globex open; both are said."""
    b = br.Briefing(
        expiries=br.Ready(items=()),
        closures=br.Weekend(
            day="Sunday",
            open_today=("Tadawul (Sun–Thu week)",),
            globex_hours="Sun 5:00 PM – Fri 4:00 PM",
        ),
    )
    text = br.render_briefing(b, escape=str)
    assert "Sunday — weekend" in text
    assert "except Tadawul (Sun–Thu week)" in text
    assert "reopens this evening" in text
    assert "open today" not in text


def test_a_weekend_without_hours_says_nothing_about_globex_rather_than_guessing() -> None:
    """No schedule string in hand → no hour on screen (a clock claim is never invented)."""
    b = br.Briefing(
        expiries=br.Ready(items=()),
        closures=br.Weekend(day="Saturday", open_today=(), globex_hours=None),
    )
    text = br.render_briefing(b, escape=str)
    assert "Saturday — weekend" in text
    assert "PM" not in text and "Globex" not in text
