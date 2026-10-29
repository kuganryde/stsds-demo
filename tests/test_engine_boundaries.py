"""Statutory boundary tests: exact brackets, floors, 'or part thereof' rounding, fail-closed cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from stsds.engine import NeedsReview, calculate

from .conftest import make_cls


def duty(rules, sha, item, **kw):
    over = {k: kw.pop(k) for k in list(kw) if k in {"is_foreign_buyer_or_entity", "is_residential_property"}}
    return calculate(make_cls(item, financials=kw, **over), rules, sha)


# ---- Item 4 employment -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("wage", "copies", "expected", "exempt"),
    [
        ("0.00", 0, "0.00", True),
        ("2999.99", 0, "0.00", True),
        ("3000.00", 0, "0.00", True),
        ("3000.00", 5, "0.00", True),  # exempt: duplicates also exempt
        ("3000.01", 0, "10.00", False),
        ("3000.01", 2, "30.00", False),
        ("12000.00", 1, "20.00", False),
    ],
)
def test_item_4(rules, rules_sha, wage, copies, expected, exempt):
    r = duty(rules, rules_sha, "ITEM_4_EMPLOYMENT", monthly_salary_rm=Decimal(wage), duplicate_copies=copies)
    assert str(r.total_duty_rm) == expected and r.exempt is exempt


def test_item_4_missing_wage_fails_closed(rules, rules_sha):
    with pytest.raises(NeedsReview) as e:
        duty(rules, rules_sha, "ITEM_4_EMPLOYMENT")
    assert e.value.code == "MISSING_WAGE"


# ---- Item 49 lease -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rent", "months", "expected"),
    [
        ("1500.00", 12, "72.00"),   # 18000/250 = 72 units x RM1
        ("1500.00", 13, "216.00"),  # >1yr: 72 x RM3
        ("1500.00", 36, "216.00"),  # boundary: not exceeding 3 years
        ("1500.00", 37, "360.00"),  # >3yr: 72 x RM5
        ("1500.00", 60, "360.00"),  # boundary: not exceeding 5 years
        ("1500.00", 61, "504.00"),  # >5yr: 72 x RM7
        ("1500.01", 12, "73.00"),   # part of RM250 rounds up: 18000.12/250 -> 73 units
        ("20.83", 12, "10.00"),     # 249.96/250 -> 1 unit x RM1 = RM1, floor RM10
        ("100.00", 12, "10.00"),    # 1200/250 -> 5 units = RM5, floor RM10
        ("250.00", 12, "12.00"),    # 3000/250 = 12 units
    ],
)
def test_item_49(rules, rules_sha, rent, months, expected):
    r = duty(rules, rules_sha, "ITEM_49_LEASE", monthly_rental_rm=Decimal(rent), lease_duration_months=months)
    assert str(r.total_duty_rm) == expected


def test_item_49_indefinite_uses_top_rate(rules, rules_sha):
    r = duty(rules, rules_sha, "ITEM_49_LEASE", monthly_rental_rm=Decimal("1500"), lease_indefinite=True)
    assert str(r.total_duty_rm) == "504.00"


def test_item_49_missing_term_fails_closed(rules, rules_sha):
    with pytest.raises(NeedsReview) as e:
        duty(rules, rules_sha, "ITEM_49_LEASE", monthly_rental_rm=Decimal("1500"))
    assert e.value.code == "MISSING_TERM"


def test_item_49_missing_rent_fails_closed(rules, rules_sha):
    with pytest.raises(NeedsReview) as e:
        duty(rules, rules_sha, "ITEM_49_LEASE", lease_duration_months=12)
    assert e.value.code == "MISSING_RENT"


def test_item_49_duplicates_added(rules, rules_sha):
    r = duty(rules, rules_sha, "ITEM_49_LEASE", monthly_rental_rm=Decimal("1500"), lease_duration_months=12, duplicate_copies=2)
    assert str(r.total_duty_rm) == "92.00"


# ---- Item 22 security --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ("1.00", "10.00"),         # 1 unit x RM5 = 5 -> floor 10
        ("2000.00", "10.00"),      # 2 units = 10
        ("2000.01", "15.00"),      # part of RM1,000 rounds up: 3 units
        ("1000000.00", "5000.00"),
        ("1000000.01", "5005.00"),
    ],
)
def test_item_22(rules, rules_sha, principal, expected):
    r = duty(rules, rules_sha, "ITEM_22_SECURITY", consideration_or_loan_rm=Decimal(principal), security_kind="SECURED_LOAN")
    assert str(r.total_duty_rm) == expected


def test_item_22_other_security_routes_to_review(rules, rules_sha):
    with pytest.raises(NeedsReview) as e:
        duty(rules, rules_sha, "ITEM_22_SECURITY", consideration_or_loan_rm=Decimal("5000"), security_kind="OTHER")
    assert e.value.code == "SECURITY_KIND"


# ---- Item 32 transfer --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100000.00", "1000.00"),
        ("100000.01", "1002.00"),   # next RM100 part at 2%: 1 unit x RM2
        ("500000.00", "9000.00"),   # 1000 + 8000
        ("500000.01", "9003.00"),   # + 1 unit x RM3
        ("1000000.00", "24000.00"),  # 1000 + 8000 + 15000
        ("1000000.01", "24004.00"),  # + 1 unit x RM4
        ("2500000.00", "84000.00"),   # 24000 + (1,500,000 / 100) x RM4 = 24000 + 60000
        ("500.00", "10.00"),         # 5 units = RM5 -> floor 10
        ("750000.00", "9000.00"),    # placeholder replaced below
    ],
)
def test_item_32_citizen(rules, rules_sha, value, expected):
    if value == "750000.00":
        expected = "16500.00"  # 1000 + 8000 + (250000/100)*3 = 16500
    r = duty(rules, rules_sha, "ITEM_32A_MOT", consideration_or_loan_rm=Decimal(value))
    assert str(r.total_duty_rm) == expected


def test_item_32_uses_greater_of_consideration_and_market_value(rules, rules_sha):
    r = duty(rules, rules_sha, "ITEM_32A_MOT", consideration_or_loan_rm=Decimal("100000"), market_value_rm=Decimal("200000"))
    assert str(r.total_duty_rm) == "3000.00"  # 1000 + (100000/100)*2


@pytest.mark.parametrize(("value", "expected"), [("500000.00", "40000.00"), ("500000.01", "40008.00")])
def test_item_32_foreign_residential(rules, rules_sha, value, expected):
    r = duty(
        rules, rules_sha, "ITEM_32A_MOT", consideration_or_loan_rm=Decimal(value),
        is_foreign_buyer_or_entity=True, is_residential_property=True,
    )
    assert str(r.total_duty_rm) == expected


def test_item_32_foreign_non_residential(rules, rules_sha):
    r = duty(
        rules, rules_sha, "ITEM_32A_MOT", consideration_or_loan_rm=Decimal("500000"),
        is_foreign_buyer_or_entity=True, is_residential_property=False,
    )
    assert str(r.total_duty_rm) == "20000.00"


def test_item_32_foreign_unknown_residential_fails_closed(rules, rules_sha):
    with pytest.raises(NeedsReview) as e:
        duty(rules, rules_sha, "ITEM_32A_MOT", consideration_or_loan_rm=Decimal("500000"), is_foreign_buyer_or_entity=True)
    assert e.value.code == "RESIDENTIAL_FLAG"


# ---- general -----------------------------------------------------------------------------------
def test_pre_effective_date_routes_to_review(rules, rules_sha):
    import datetime as dt

    c = make_cls("ITEM_4_EMPLOYMENT", financials={"monthly_salary_rm": Decimal("5000")}, execution_date=dt.date(2025, 12, 31))
    with pytest.raises(NeedsReview) as e:
        calculate(c, rules, rules_sha)
    assert e.value.code == "RULE_WINDOW"


def test_result_records_rule_provenance(rules, rules_sha):
    r = duty(rules, rules_sha, "ITEM_4_EMPLOYMENT", monthly_salary_rm=Decimal("5000"))
    assert r.rules_version == rules.version and r.rules_sha256 == rules_sha and r.steps


def test_float_noise_cannot_change_duty(rules, rules_sha):
    """0.1 + 0.2 style binary error must be impossible: money is integer sen end to end."""
    r = duty(rules, rules_sha, "ITEM_22_SECURITY", consideration_or_loan_rm=Decimal("3000.00"), security_kind="SECURED_LOAN")
    assert str(r.total_duty_rm) == "15.00"
