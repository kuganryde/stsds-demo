"""Versioned statutory rules, loaded from data (not code) and hashed for the audit trail.

Every calculation records ``rules_version`` and ``rules_sha256`` so a filing can be reproduced
exactly, and a rule change is a reviewed data change rather than a code change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bracket(_S):
    max_months: int | None
    rate: int


class Tier(_S):
    upto: Decimal | None
    rate: int


class Item4(_S):
    duty: Decimal
    wage_exemption_monthly: Decimal


class Item49(_S):
    unit_rm: int
    brackets: list[Bracket]
    indefinite_rate: int

    @model_validator(mode="after")
    def _ordered(self) -> Item49:
        bounds = [b.max_months for b in self.brackets]
        finite = [b for b in bounds if b is not None]
        if finite != sorted(finite) or bounds[-1] is not None or None in bounds[:-1]:
            raise ValueError("item_49 brackets must ascend and end with an open bracket")
        return self


class Item22(_S):
    secured_unit_rm: int
    secured_rate: int


class Item4General(_S):
    duty: Decimal


class Item22Agreement(_S):
    unit_rm: int
    fixed_tenure_rate: int
    no_tenure_rate: int


class Item32Shares(_S):
    unit_rm: int
    rate: int


class PenaltyBand(_S):
    within_months: int | None  # months after the 30-day deadline; None = the open last band
    min_rm: Decimal
    pct: int


class Penalty(_S):
    bands: list[PenaltyBand]
    basis: str
    source: str

    @model_validator(mode="after")
    def _ordered(self) -> Penalty:
        bounds = [b.within_months for b in self.bands]
        finite = [b for b in bounds if b is not None]
        if not bounds or finite != sorted(finite) or bounds[-1] is not None or None in bounds[:-1]:
            raise ValueError("penalty bands must ascend and end with an open band")
        return self


class Item32(_S):
    unit_rm: int
    citizen_tiers: list[Tier]
    foreign_residential_rate: int
    foreign_other_rate: int
    foreign_residential_effective_from: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> Item32:
        uppers = [t.upto for t in self.citizen_tiers]
        finite = [u for u in uppers if u is not None]
        if finite != sorted(finite) or uppers[-1] is not None or None in uppers[:-1]:
            raise ValueError("item_32 tiers must ascend and end with an open tier")
        return self


class Signoff(_S):
    by: str
    role: str
    date: dt.date


class Rules(_S):
    version: str
    effective_from: dt.date
    min_duty: Decimal
    duplicate_copy_duty: Decimal
    item_4: Item4
    item_49: Item49
    item_22: Item22
    item_32: Item32
    item_4_general: Item4General
    item_22_agreement: Item22Agreement
    item_32_shares: Item32Shares
    penalty: Penalty
    phase_start: dict[str, dt.date]
    signoff: Signoff | None = None
    notes: dict[str, str] = {}
    sources: list[dict[str, str]] = []


def load_rules(path: str | Path) -> tuple[Rules, str]:
    """Return (rules, sha256-of-file). Decimals are parsed from text, never via float."""
    raw = Path(path).read_bytes()
    data = json.loads(raw, parse_float=Decimal, parse_int=int)
    return Rules.model_validate(data), hashlib.sha256(raw).hexdigest()
