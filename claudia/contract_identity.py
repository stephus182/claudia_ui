"""One futures contract, named in IBKR's own terms (design 2026-09-10, gap #37).

IBKR gives the exchange local symbol only as an *output*: `GET /iserver/contract/{conid}/info`
returns `local_symbol "ESU6"`, `contract_month "202609"`, `maturity_date "20260918"`,
`company_name "E-mini S&P 500"`, `multiplier "50"`, `currency "USD"` (measured 2026-09-10 on
conid 649180671; `symbol=ESU6` on contract search is "No symbol found"). Nothing here is
derived from a ticker: every field is IBKR's string or a fixed re-spelling of one — `202609`
becomes `SEP26`, IBKR's own month token. A read that fails, or carries no local symbol,
yields None and the caller shows what it showed before, never a guessed name.

The dashboard polls every 15 s and a contract's identity never changes, so identities are
cached for the process, keyed by conid. Failures are not cached: the gateway may be back.

Source: https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-contracts/get-instrument-info.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

_MONTH_TOKENS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class ContractInfoSource(Protocol):
    """Whatever can answer `/iserver/contract/{conid}/info` — the one read this module makes."""

    def get_contract_info(self, conid: int) -> dict[str, Any]:
        """Contract metadata for one conid, as IBKR reports it."""
        ...


@dataclass(frozen=True)
class ContractIdentity:
    """A futures contract as IBKR names it. `month` is IBKR's `MMMYY` token, `expires` ISO."""

    conid: int
    local_symbol: str
    month: str | None
    expires: str | None
    name: str
    multiplier: float | None
    currency: str | None

    @property
    def label(self) -> str:
        """`ESU6 · SEP26 · expires 2026-09-18` — shorter when a part is unknown, never padded."""
        parts = [self.local_symbol]
        if self.month:
            parts.append(self.month)
        if self.expires:
            parts.append(f"expires {self.expires}")
        return " · ".join(parts)


def month_token(contract_month: Any) -> str | None:
    """`202609` → `SEP26`, IBKR's own month token; None for anything that is not `YYYYMM`."""
    text = str(contract_month or "")
    if len(text) != 6 or not text.isdigit():
        return None
    month = int(text[4:6])
    if not 1 <= month <= 12:
        return None
    return f"{_MONTH_TOKENS[month - 1]}{text[2:4]}"


def _iso_date(yyyymmdd: Any) -> str | None:
    """`20260918` → `2026-09-18`; None for anything else."""
    text = str(yyyymmdd or "")
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def parse_contract_info(conid: int, info: Any) -> ContractIdentity | None:
    """Type IBKR's contract-info payload; None when it carries no local symbol."""
    if not isinstance(info, dict):
        return None
    local_symbol = str(info.get("local_symbol") or "").strip()
    if not local_symbol:
        return None
    try:
        multiplier: float | None = float(info["multiplier"])
    except (KeyError, TypeError, ValueError):
        multiplier = None
    raw_ccy = info.get("currency")
    currency = (
        str(raw_ccy).strip().upper() if isinstance(raw_ccy, str) and raw_ccy.strip() else None
    )
    return ContractIdentity(
        conid=conid,
        local_symbol=local_symbol,
        month=month_token(info.get("contract_month")),
        expires=_iso_date(info.get("maturity_date")),
        name=str(info.get("company_name") or "").strip(),
        multiplier=multiplier,
        currency=currency,
    )


_CACHE: dict[int, ContractIdentity] = {}


def contract_identity(client: ContractInfoSource, conid: int) -> ContractIdentity | None:
    """The cached identity of `conid`, read once from IBKR; None on any failure, never raises."""
    cached = _CACHE.get(conid)
    if cached is not None:
        return cached
    try:
        info = client.get_contract_info(conid)
    except Exception as exc:
        log.warning("Contract identity unavailable for conid %s: %s", conid, exc)
        return None
    identity = parse_contract_info(conid, info)
    if identity is not None:
        _CACHE[conid] = identity
    return identity


def clear_cache() -> None:
    """Forget every cached identity (tests, or a process that wants a fresh read)."""
    _CACHE.clear()
