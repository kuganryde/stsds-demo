from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stsds.classifier import parse_classification
from stsds.gates import (
    GateFailure,
    check_pdf,
    clock_gate,
    confidence_gate,
    grounding_gate,
    jpph_gate,
    parity_gate,
    parse_rm,
    phase_gate,
    preflight_parties,
    sec47_clock,
)
from stsds.schemas import IdType, Party

from .conftest import FakeVerifier, make_cls, make_pdf


def code(fn, *a, **k):
    with pytest.raises(GateFailure) as e:
        fn(*a, **k)
    return e.value.code


# ---- FR-01 -------------------------------------------------------------------------------------
def test_pdf_ok():
    assert check_pdf(make_pdf("hello", pages=2)) == 2


@pytest.mark.parametrize(
    ("data", "expected"),
    [(b"", "PDF_EMPTY"), (b"not a pdf", "PDF_MAGIC"), (b"%PDF-1.4 garbage", "PDF_UNREADABLE")],
)
def test_pdf_rejects(data, expected):
    assert code(check_pdf, data) == expected


def test_pdf_size_ceiling():
    assert code(check_pdf, b"%PDF-" + b"0" * 50, max_bytes=10) == "PDF_TOO_LARGE"
    assert check_pdf(make_pdf("x"), max_bytes=20 * 1024 * 1024) == 1


@pytest.mark.parametrize(
    ("today", "state", "remaining"),
    [
        (dt.date(2026, 11, 2), "OK", 30),
        (dt.date(2026, 11, 25), "URGENT", 7),
        (dt.date(2026, 12, 2), "URGENT", 0),
        (dt.date(2026, 12, 3), "OVERDUE", -1),
    ],
)
def test_sec47_clock(today, state, remaining):
    c = sec47_clock(dt.date(2026, 11, 2), today)
    assert (c.state, c.days_remaining) == (state, remaining)


def test_clock_uses_receipt_date_for_foreign_signing():
    c = sec47_clock(dt.date(2026, 10, 1), dt.date(2026, 11, 10), received_in_malaysia=dt.date(2026, 11, 5))
    assert c.deadline == dt.date(2026, 12, 5) and c.state == "OK"


def test_overdue_routes_to_review():
    assert code(clock_gate, sec47_clock(dt.date(2026, 1, 1), dt.date(2026, 11, 1))) == "SEC47_OVERDUE"


# ---- FR-02 -------------------------------------------------------------------------------------
def test_confidence_floor():
    assert code(confidence_gate, make_cls(confidence=0.94)) == "LOW_CONFIDENCE"
    confidence_gate(make_cls(confidence=0.95))


LEASE_TEXT = "TENANCY AGREEMENT made on 2 November 2026. Monthly rent RM1,500.00 for a term of 12 months."


def lease(**kw):
    return make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 12, **kw})


def test_grounding_passes_when_figures_in_text():
    grounding_gate(lease(), LEASE_TEXT)


def test_grounding_rejects_hallucinated_rent():
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1200.00"), "lease_duration_months": 12})
    assert code(grounding_gate, c, LEASE_TEXT) == "UNGROUNDED_FIGURE"


def test_grounding_rejects_substring_match():
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 12})
    assert code(grounding_gate, c, "rent RM11,500.00 for 12 months on 2 November 2026") == "UNGROUNDED_FIGURE"


def test_grounding_rejects_wrong_term_and_wrong_date():
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 36})
    assert code(grounding_gate, c, LEASE_TEXT) == "UNGROUNDED_FIGURE"
    assert code(grounding_gate, lease(), LEASE_TEXT.replace("2 November", "3 November")) == "UNGROUNDED_FIGURE"


@pytest.mark.parametrize(
    "text",
    [
        "dated 2026-11-02, rent 1,500.00, 12 months",
        "dated 02/11/2026, rent RM 1500, one (1) year",
        "this 2nd day of November 2026 rent RM1,500 for twelve 12 months",
        "executed 2 Nov 2026; rent 1500.00 per month; term 1 year",
        "pada 2 November 2026 sewa RM1,500.00 selama 12 bulan",
    ],
)
def test_grounding_accepts_common_formats(text):
    grounding_gate(lease(), text)


def test_classifier_schema_rejects_extra_fields_and_bad_pairing():
    assert code(parse_classification, '{"foo": 1}') == "CLASSIFIER_SCHEMA"
    assert code(parse_classification, "not json") == "CLASSIFIER_SCHEMA"
    good = make_cls().model_dump_json()
    assert parse_classification(good).first_schedule_item.value == "ITEM_49_LEASE"
    bad = good.replace("SEWA_PAJAKAN", "SEKURITI")
    assert code(parse_classification, bad) == "CLASSIFIER_SCHEMA"


def test_classifier_parses_money_exactly():
    raw = make_cls("ITEM_22_SECURITY", financials={"consideration_or_loan_rm": Decimal("2000.10"), "security_kind": "SECURED_LOAN"}).model_dump_json()
    raw = raw.replace('"2000.10"', "2000.10")  # model emitted a JSON float
    assert parse_classification(raw).financials.consideration_or_loan_rm == Decimal("2000.10")


# ---- FR-03 -------------------------------------------------------------------------------------
def test_parties_ok():
    preflight_parties(make_cls().parties, FakeVerifier(True))


def test_parties_rejections():
    good = make_cls().parties[0]
    for bad, expected in [
        (good.model_copy(update={"tin": None}), "TIN_FORMAT"),
        (good.model_copy(update={"tin": "12"}), "TIN_FORMAT"),
        (good.model_copy(update={"id_type": IdType.NRIC, "id_number": "991399123456"}), "NRIC_INVALID"),
        (good.model_copy(update={"id_type": IdType.BRN, "id_number": "!"}), "BRN_INVALID"),
        (good.model_copy(update={"id_type": IdType.PASSPORT, "id_number": "!"}), "PASSPORT_INVALID"),
    ]:
        assert code(preflight_parties, [bad], FakeVerifier(True)) == expected
    assert code(preflight_parties, [good], FakeVerifier(False)) == "TIN_REJECTED"


def test_valid_nric_accepted():
    p = Party(role="tenant", name="Ali", tin="IG12345678090", id_type="NRIC", id_number="900101-14-5678")
    preflight_parties([p], FakeVerifier(True))


def test_unconfigured_verifier_fails_closed():
    from stsds.gates import HttpTinVerifier

    assert code(preflight_parties, make_cls().parties, HttpTinVerifier(None, None)) == "TIN_VERIFIER_UNCONFIGURED"


def test_jpph_gate():
    assert code(jpph_gate, make_cls("ITEM_32A_MOT", requires_jpph_valuation=True)) == "JPPH_VALUATION"


def test_phase_gate(rules):
    from stsds.schemas import Item

    phase_gate(Item.ITEM_49_LEASE, dt.date(2026, 11, 2), dt.date(2026, 11, 10), rules)
    assert code(phase_gate, Item.ITEM_32A_MOT, dt.date(2026, 11, 2), dt.date(2026, 11, 10), rules) == "PHASE_NOT_LIVE"
    phase_gate(Item.ITEM_32A_MOT, dt.date(2026, 11, 2), dt.date(2026, 11, 10), rules, phase2_enabled=True)
    phase_gate(Item.ITEM_32A_MOT, dt.date(2027, 1, 5), dt.date(2027, 1, 5), rules)


# ---- FR-04 -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "value"),
    [("RM 1,234.50", "1234.50"), ("72", "72.00"), ("RM72.5", "72.50"), ("MYR 10.00", "10.00"), (" 1,000 ", "1000.00")],
)
def test_parse_rm(text, value):
    assert parse_rm(text) == Decimal(value)


@pytest.mark.parametrize("text", ["", "RM", "abc", "1.234,50", "12.345", "RM 1,23"])
def test_parse_rm_rejects_junk(text):
    assert code(parse_rm, text) == "PARITY_UNPARSEABLE"


def test_parity_exact_match_passes():
    parity_gate(Decimal("72.00"), Decimal("72"))


@pytest.mark.parametrize("lhdn", ["72.01", "71.99", "0.00"])
def test_parity_any_sen_difference_halts(lhdn):
    assert code(parity_gate, Decimal("72.00"), Decimal(lhdn)) == "PARITY_MISMATCH"


def test_parity_missing_halts():
    assert code(parity_gate, Decimal("72.00"), None) == "PARITY_MISSING"
