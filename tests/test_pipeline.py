from __future__ import annotations

import asyncio
import base64
from decimal import Decimal

import pytest

from stsds.audit import AuditRecord, verify_chain

from .conftest import FakeRpa, make_cls, make_pdf

LEASE_TEXT = "TENANCY AGREEMENT made on 2 November 2026. Monthly rent RM1,500.00 for a term of 12 months."
LOAN_TEXT = "FACILITY AGREEMENT dated 2 November 2026. Principal sum RM5,000,000.00 secured by a charge."


def lease_cls():
    return make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 12})


def loan_cls():
    return make_cls("ITEM_22_SECURITY", financials={"consideration_or_loan_rm": Decimal("5000000.00"), "security_kind": "SECURED_LOAN"})


def b64(text=LEASE_TEXT):
    return base64.b64encode(make_pdf(text)).decode()


def run(coro):
    return asyncio.run(coro)


def test_happy_path_end_to_end(build):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, s3, notifier, audit = build(lease_cls(), LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "docusign"))
    assert res.ok and res.state == "PARITY_OK"
    assert res.data["engine_total"] == "72.00"
    sub = run(pipe.submit(res.txn_id))
    assert sub.ok and sub.state == "SUBMITTED" and rpa.submits == 1
    done = run(pipe.complete(res.txn_id))
    assert done.ok and done.state == "VAULTED"
    assert {"stsds/%s/bundle.pdf" % res.txn_id, "stsds/%s/bnds.json" % res.txn_id} <= set(s3.objects)
    recs = audit.records(res.txn_id)
    assert verify_chain(recs) and [r.event for r in recs][-1] == "VAULTED"
    assert not notifier.sent


def test_parity_mismatch_halts_and_never_submits(build):
    rpa = FakeRpa(Decimal("72.01"))
    pipe, _, notifier, _ = build(lease_cls(), LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "docusign"))
    assert not res.ok and res.halt["code"] == "PARITY_MISMATCH"
    assert notifier.sent and notifier.sent[0][1] == "PARITY_MISMATCH"
    again = run(pipe.submit(res.txn_id))
    assert not again.ok and rpa.submits == 0


def test_missing_lhdn_figure_halts(build):
    pipe, *_ = build(lease_cls(), LEASE_TEXT, FakeRpa(None))
    res = run(pipe.run_to_parity(b64(), "x"))
    assert res.halt["code"] == "PARITY_MISSING"


def test_hallucinated_figure_never_reaches_rpa(build):
    rpa = FakeRpa(Decimal("72.00"))
    bad = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("900.00"), "lease_duration_months": 12})
    pipe, *_ = build(bad, LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "x"))
    assert res.halt["code"] == "UNGROUNDED_FIGURE" and rpa.drafts == 0


def test_low_confidence_routes_to_review(build):
    pipe, *_ = build(lease_cls().model_copy(update={"confidence": 0.5}), LEASE_TEXT, FakeRpa(Decimal("72")))
    res = run(pipe.run_to_parity(b64(), "x"))
    assert res.halt["code"] == "LOW_CONFIDENCE" and res.state == "REVIEW"


def test_bad_tin_blocks_before_rpa(build):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, *_ = build(lease_cls(), LEASE_TEXT, rpa, verifier_ok=False)
    res = run(pipe.run_to_parity(b64(), "x"))
    assert res.halt["code"] == "TIN_REJECTED" and rpa.drafts == 0


def test_jpph_and_phase2_blocked_until_2027(build):
    c = make_cls("ITEM_32A_MOT", financials={"consideration_or_loan_rm": Decimal("500000.00")})
    text = "MEMORANDUM OF TRANSFER dated 2 November 2026. Consideration RM500,000.00."
    pipe, *_ = build(c, text, FakeRpa(Decimal("9000")))
    assert run(pipe.run_to_parity(b64(text), "x")).halt["code"] == "PHASE_NOT_LIVE"


def test_overdue_instrument_routes_to_review(build):
    c = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 12}, execution_date="2026-09-01")
    text = LEASE_TEXT.replace("2 November", "1 September")
    pipe, *_ = build(c, text, FakeRpa(Decimal("72")))
    assert run(pipe.run_to_parity(b64(text), "x")).halt["code"] == "SEC47_OVERDUE"


def test_shadow_mode_never_submits(build):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, *_ = build(lease_cls(), LEASE_TEXT, rpa, mode="shadow")
    res = run(pipe.run_to_parity(b64(), "x"))
    assert res.ok
    assert run(pipe.submit(res.txn_id)).halt["code"] == "SHADOW_MODE" and rpa.submits == 0


def test_unsigned_rules_block_submit(build):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, *_ = build(lease_cls(), LEASE_TEXT, rpa, signed=False)
    res = run(pipe.run_to_parity(b64(), "x"))
    assert run(pipe.submit(res.txn_id)).halt["code"] == "RULES_UNSIGNED" and rpa.submits == 0


def test_maker_checker_above_threshold(build):
    rpa = FakeRpa(Decimal("25000.00"))
    pipe, *_ = build(loan_cls(), LOAN_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(LOAN_TEXT), "x"))
    assert res.ok and res.data["engine_total"] == "25000.00"
    pending = run(pipe.submit(res.txn_id))
    assert pending.state == "AWAITING_APPROVAL" and rpa.submits == 0
    # a blank approver never counts as approval
    blank = run(pipe.submit(res.txn_id, approved_by="   "))
    assert blank.state == "AWAITING_APPROVAL" and rpa.submits == 0
    # a transaction awaiting a checker resumes from the same state once approved
    approved = run(pipe.submit(res.txn_id, approved_by="CFO Delegate"))
    assert approved.ok and rpa.submits == 1


def test_ingest_is_idempotent(build):
    pipe, *_ = build(lease_cls(), LEASE_TEXT, FakeRpa(Decimal("72")))
    document = b64()  # identical bytes: same document delivered twice by the upstream system
    first = pipe.ingest(document, "x")
    second = pipe.ingest(document, "x")
    assert first.txn_id == second.txn_id and second.data.get("duplicate") is True


def test_stages_cannot_be_skipped(build):
    rpa = FakeRpa(Decimal("72"))
    pipe, *_ = build(lease_cls(), LEASE_TEXT, rpa)
    res = pipe.ingest(b64(), "x")
    assert run(pipe.draft(res.txn_id)).halt["code"] == "STAGE_ORDER"
    assert run(pipe.submit(res.txn_id)).halt["code"] == "STAGE_ORDER"
    assert rpa.drafts == 0 and rpa.submits == 0


def test_sijil_cached_before_vault_failure_and_kept(build, tmp_path):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, s3, *_ = build(lease_cls(), LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "x"))
    run(pipe.submit(res.txn_id))
    s3.lock_mode = "GOVERNANCE"  # simulate a misconfigured bucket
    out = run(pipe.complete(res.txn_id))
    assert out.halt["code"] == "VAULT_LOCK"
    assert (tmp_path / "cache" / f"{res.txn_id}.sijil.pdf").exists()  # one-time buffer survives


def test_sijil_download_failure_halts(build):
    rpa = FakeRpa(Decimal("72.00"))
    rpa.fail_download = True
    pipe, *_ = build(lease_cls(), LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "x"))
    run(pipe.submit(res.txn_id))
    assert run(pipe.complete(res.txn_id)).halt["code"] == "SIJIL_STREAM"


def test_audit_chain_detects_tampering(build):
    pipe, _, _, audit = build(lease_cls(), LEASE_TEXT, FakeRpa(Decimal("72.00")))
    res = run(pipe.run_to_parity(b64(), "x"))
    recs = audit.records(res.txn_id)
    assert verify_chain(recs)
    forged = list(recs)
    r = forged[1]
    forged[1] = AuditRecord(r.txn_id, r.seq, r.ts, r.event, {**r.data, "item": "FORGED"}, r.prev_hash, r.hash)
    assert not verify_chain(forged)
    assert not verify_chain(recs[1:])
    assert not verify_chain([recs[0], recs[2]])


def test_vault_retention_is_seven_years_compliance(build):
    rpa = FakeRpa(Decimal("72.00"))
    pipe, s3, *_ = build(lease_cls(), LEASE_TEXT, rpa)
    res = run(pipe.run_to_parity(b64(), "x"))
    run(pipe.submit(res.txn_id))
    done = run(pipe.complete(res.txn_id))
    obj = s3.objects[f"stsds/{res.txn_id}/bundle.pdf"]
    assert obj["ObjectLockMode"] == "COMPLIANCE"
    assert obj["ObjectLockRetainUntilDate"].year - __import__("datetime").datetime.now().year in (7, 6, 8)
    assert done.data["bundle_sha256"] and done.data["sijil_sha256"]


@pytest.mark.parametrize("bad", ["!!!notbase64", ""])
def test_ingest_rejects_garbage(build, bad):
    from stsds.gates import GateFailure

    pipe, *_ = build(lease_cls(), LEASE_TEXT, FakeRpa(Decimal("72")))
    with pytest.raises(GateFailure):
        pipe.ingest(bad, "x")
