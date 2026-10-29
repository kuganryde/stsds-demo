"""Deterministic Stamp Act 1949 duty engine.

Design rules (these are what make the arithmetic exact and auditable):
  * No floating point anywhere. Money is converted to integer sen; units are integer ceilings.
  * Fail closed. Any missing or ambiguous input raises NeedsReview instead of defaulting.
    (The blueprint's reference snippet defaulted a missing salary to 0, which silently treats the
    contract as exempt, and a missing lease term to 12 months, which silently under-assesses.)
  * "Per RM N or part thereof" is applied per tier with an integer ceiling, not as a raw percentage.
  * Rates come from the versioned, hashed rules file; the engine holds no statutory constants.
"""

from __future__ import annotations

from decimal import Decimal

from .rules import Rules
from .schemas import CalcResult, Classification, Item, SecurityKind, Step

ENGINE_VERSION = "1.0.0"
_SEN = 100


class NeedsReview(Exception):
    """Raised when the engine cannot compute duty with certainty. Routes to a human officer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sen(amount: Decimal, field: str) -> int:
    scaled = amount * _SEN
    if scaled != scaled.to_integral_value():
        raise NeedsReview("MONEY_PRECISION", f"{field} has more than two decimal places")
    return int(scaled)


def _units(amount_sen: int, unit_rm: int) -> int:
    """Integer ceiling of amount / unit (the 'or part thereof' rule)."""
    unit_sen = unit_rm * _SEN
    return -(-amount_sen // unit_sen)


def _rm(value: int) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _need(value: object, code: str, message: str) -> None:
    if value is None:
        raise NeedsReview(code, message)


def _item_4(c: Classification, r: Rules, steps: list[Step]) -> tuple[int, bool]:
    f = c.financials
    _need(f.monthly_salary_rm, "MISSING_WAGE", "Monthly wage not found in the contract")
    assert f.monthly_salary_rm is not None
    wage = _sen(f.monthly_salary_rm, "monthly_salary_rm")
    threshold = _sen(r.item_4.wage_exemption_monthly, "wage_exemption_monthly")
    if wage <= threshold:
        steps.append(
            Step(
                label="Item 4 employment: exempt",
                formula=f"wage {f.monthly_salary_rm} <= {r.item_4.wage_exemption_monthly}",
                amount_rm=_rm(0),
            )
        )
        return 0, True
    duty = _sen(r.item_4.duty, "item_4.duty") // _SEN
    steps.append(
        Step(
            label="Item 4 employment: fixed duty",
            formula=f"wage {f.monthly_salary_rm} > {r.item_4.wage_exemption_monthly}",
            amount_rm=_rm(duty),
        )
    )
    return duty, False


def _item_49(c: Classification, r: Rules, steps: list[Step]) -> tuple[int, bool]:
    f = c.financials
    _need(f.monthly_rental_rm, "MISSING_RENT", "Monthly rent not found in the contract")
    assert f.monthly_rental_rm is not None
    if f.lease_indefinite:
        rate = r.item_49.indefinite_rate
        term = "indefinite"
    else:
        _need(f.lease_duration_months, "MISSING_TERM", "Lease term not found in the contract")
        assert f.lease_duration_months is not None
        months = f.lease_duration_months
        rate = next(
            b.rate for b in r.item_49.brackets if b.max_months is None or months <= b.max_months
        )
        term = f"{months} months"
    annual_sen = _sen(f.monthly_rental_rm, "monthly_rental_rm") * 12
    units = _units(annual_sen, r.item_49.unit_rm)
    gross = units * rate
    floor = _sen(r.min_duty, "min_duty") // _SEN
    duty = max(gross, floor)
    steps.append(
        Step(
            label="Item 49 lease: duty on annual rent",
            formula=(
                f"ceil(({f.monthly_rental_rm} x 12) / {r.item_49.unit_rm}) = {units} units "
                f"x RM{rate} ({term}) = {gross}"
            ),
            amount_rm=_rm(gross),
        )
    )
    if gross < floor:
        steps.append(
            Step(label="Section 36CB minimum duty", formula=f"max({gross}, {floor})", amount_rm=_rm(floor))
        )
    return duty, False


def _item_22(c: Classification, r: Rules, steps: list[Step]) -> tuple[int, bool]:
    f = c.financials
    if f.security_kind != SecurityKind.SECURED_LOAN:
        raise NeedsReview("SECURITY_KIND", "Only secured loans are computed automatically")
    _need(f.consideration_or_loan_rm, "MISSING_PRINCIPAL", "Principal sum not found")
    assert f.consideration_or_loan_rm is not None
    units = _units(_sen(f.consideration_or_loan_rm, "principal"), r.item_22.secured_unit_rm)
    gross = units * r.item_22.secured_rate
    floor = _sen(r.min_duty, "min_duty") // _SEN
    steps.append(
        Step(
            label="Item 22 security: 0.5% ad valorem",
            formula=(
                f"ceil({f.consideration_or_loan_rm} / {r.item_22.secured_unit_rm}) = {units} units "
                f"x RM{r.item_22.secured_rate} = {gross}"
            ),
            amount_rm=_rm(gross),
        )
    )
    if gross < floor:
        steps.append(
            Step(label="Section 36CB minimum duty", formula=f"max({gross}, {floor})", amount_rm=_rm(floor))
        )
    return max(gross, floor), False


def _item_32(c: Classification, r: Rules, steps: list[Step]) -> tuple[int, bool]:
    f = c.financials
    _need(f.consideration_or_loan_rm, "MISSING_VALUE", "Consideration not found")
    assert f.consideration_or_loan_rm is not None
    value = f.consideration_or_loan_rm
    if f.market_value_rm is not None and f.market_value_rm > value:
        value = f.market_value_rm  # duty is charged on the greater of consideration and market value
    value_sen = _sen(value, "dutiable_value")
    unit = r.item_32.unit_rm
    if c.is_foreign_buyer_or_entity:
        _need(c.is_residential_property, "RESIDENTIAL_FLAG", "Residential status not determined")
        if c.is_residential_property:
            if c.execution_date < r.item_32.foreign_residential_effective_from:
                raise NeedsReview("RATE_WINDOW", "Foreign residential rate not effective on this date")
            rate = r.item_32.foreign_residential_rate
            label = "Item 32(ab) non-citizen residential"
        else:
            rate = r.item_32.foreign_other_rate
            label = "Item 32(a) non-citizen non-residential"
        units = _units(value_sen, unit)
        gross = units * rate
        steps.append(
            Step(label=label, formula=f"ceil({value} / {unit}) = {units} units x RM{rate} = {gross}", amount_rm=_rm(gross))
        )
    else:
        gross = 0
        prev = 0
        for tier in r.item_32.citizen_tiers:
            upper = _sen(tier.upto, "tier") if tier.upto is not None else None
            top = value_sen if upper is None else min(value_sen, upper)
            portion = top - prev
            if portion > 0:
                units = _units(portion, unit)
                part = units * tier.rate
                gross += part
                steps.append(
                    Step(
                        label=f"Item 32(a) citizen tier RM{tier.rate} per RM{unit}",
                        formula=f"ceil({Decimal(portion) / _SEN} / {unit}) = {units} units x RM{tier.rate} = {part}",
                        amount_rm=_rm(part),
                    )
                )
            if upper is None or value_sen <= upper:
                break
            prev = upper
    floor = _sen(r.min_duty, "min_duty") // _SEN
    if gross < floor:
        steps.append(
            Step(label="Section 36CB minimum duty", formula=f"max({gross}, {floor})", amount_rm=_rm(floor))
        )
    return max(gross, floor), False


_HANDLERS = {
    Item.ITEM_4_EMPLOYMENT: _item_4,
    Item.ITEM_49_LEASE: _item_49,
    Item.ITEM_22_SECURITY: _item_22,
    Item.ITEM_32A_MOT: _item_32,
}


def calculate(c: Classification, rules: Rules, rules_sha256: str) -> CalcResult:
    """Compute stamp duty for a classified instrument. Raises NeedsReview if not certain."""
    if c.execution_date < rules.effective_from:
        raise NeedsReview("RULE_WINDOW", "Instrument executed before the rule set's effective date")
    steps: list[Step] = []
    primary, exempt = _HANDLERS[c.first_schedule_item](c, rules, steps)
    copies = c.financials.duplicate_copies
    dup_each = _sen(rules.duplicate_copy_duty, "duplicate_copy_duty") // _SEN
    duplicate = 0 if exempt else copies * dup_each
    if duplicate:
        steps.append(
            Step(label="Duplicate copies", formula=f"{copies} x RM{dup_each}", amount_rm=_rm(duplicate))
        )
    total = primary + duplicate
    return CalcResult(
        item=c.first_schedule_item,
        exempt=exempt,
        primary_duty_rm=_rm(primary),
        duplicate_duty_rm=_rm(duplicate),
        total_duty_rm=_rm(total),
        steps=steps,
        rules_version=rules.version,
        rules_sha256=rules_sha256,
        engine_version=ENGINE_VERSION,
    )
