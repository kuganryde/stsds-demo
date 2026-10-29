"""Differential test: the engine must agree with an independent oracle on thousands of inputs.

The oracle is deliberately written differently (exact Fractions, closed-form tier maths) so a bug
would have to be made twice, in two different styles, to go unnoticed.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from stsds.engine import calculate

from .conftest import make_cls

money = st.integers(min_value=0, max_value=5_000_000_00).map(lambda sen: Decimal(sen) / 100)
positive = st.integers(min_value=1, max_value=5_000_000_00).map(lambda sen: Decimal(sen) / 100)


def ceil_frac(x: Fraction) -> int:
    return math.ceil(x)


def oracle_lease(rent: Decimal, months: int) -> int:
    rate = 1 if months <= 12 else 3 if months <= 36 else 5 if months <= 60 else 7
    annual = Fraction(rent) * 12
    return max(ceil_frac(annual / 250) * rate, 10)


def oracle_security(principal: Decimal) -> int:
    return max(ceil_frac(Fraction(principal) / 1000) * 5, 10)


def oracle_transfer(value: Decimal) -> int:
    v = Fraction(value)
    bands = [(Fraction(0), Fraction(100_000), 1), (Fraction(100_000), Fraction(500_000), 2),
             (Fraction(500_000), Fraction(1_000_000), 3), (Fraction(1_000_000), None, 4)]
    total = 0
    for lo, hi, rate in bands:
        if v <= lo:
            break
        top = v if hi is None else min(v, hi)
        total += ceil_frac((top - lo) / 100) * rate
    return max(total, 10)


@settings(max_examples=600, deadline=None)
@given(rent=positive, months=st.integers(min_value=1, max_value=240), copies=st.integers(0, 5))
def test_lease_matches_oracle(rules, rules_sha, rent, months, copies):
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": rent, "lease_duration_months": months, "duplicate_copies": copies})
    assert calculate(c, rules, rules_sha).total_duty_rm == Decimal(oracle_lease(rent, months) + 10 * copies)


@settings(max_examples=600, deadline=None)
@given(principal=positive)
def test_security_matches_oracle(rules, rules_sha, principal):
    c = make_cls("ITEM_22_SECURITY", financials={"consideration_or_loan_rm": principal, "security_kind": "SECURED_LOAN"})
    assert calculate(c, rules, rules_sha).total_duty_rm == Decimal(oracle_security(principal))


@settings(max_examples=600, deadline=None)
@given(value=positive)
def test_transfer_matches_oracle(rules, rules_sha, value):
    c = make_cls("ITEM_32A_MOT", financials={"consideration_or_loan_rm": value})
    assert calculate(c, rules, rules_sha).total_duty_rm == Decimal(oracle_transfer(value))


@settings(max_examples=300, deadline=None)
@given(value=positive)
def test_foreign_residential_is_eight_per_hundred_or_part(rules, rules_sha, value):
    c = make_cls("ITEM_32A_MOT", financials={"consideration_or_loan_rm": value}, is_foreign_buyer_or_entity=True, is_residential_property=True)
    expected = max(ceil_frac(Fraction(value) / 100) * 8, 10)
    assert calculate(c, rules, rules_sha).total_duty_rm == Decimal(expected)


@settings(max_examples=300, deadline=None)
@given(a=positive, b=positive)
def test_transfer_duty_is_monotonic(rules, rules_sha, a, b):
    lo, hi = sorted((a, b))
    f = lambda v: calculate(make_cls("ITEM_32A_MOT", financials={"consideration_or_loan_rm": v}), rules, rules_sha).total_duty_rm
    assert f(lo) <= f(hi)


@settings(max_examples=300, deadline=None)
@given(rent=money, months=st.integers(1, 240))
def test_minimum_duty_floor_always_holds(rules, rules_sha, rent, months):
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": rent, "lease_duration_months": months})
    assert calculate(c, rules, rules_sha).total_duty_rm >= Decimal("10.00")
