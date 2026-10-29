"""End-to-end behaviour of batch intake, scoring, human review, payment, stamping and verification."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest

from stsds import demo
from stsds.gates import GateFailure
from stsds.rpa import PortalBill

NOW = dt.date(2026, 11, 10)
SIGNED = dt.date(2026, 11, 8)


def pdf(kind: str, rent: str | None = None) -> bytes:
    text = demo.sample_text(kind, SIGNED)
    if rent:
        text = text.replace("RM1,500.00", f"RM{Decimal(rent):,.2f}")
    return demo.make_pdf(text)


def run(coro):
    return asyncio.run(coro)


def kit_for(tmp_path, **kw):
    kw.setdefault("today", NOW)
    return demo.Kit(tmp_path, **kw)


def state(kit, txn):
    return kit.pipeline.store.load(txn).state


async def one(kit, data, name="doc.pdf"):
    batch = await kit.batches.create("ops", "test", [(name, data)])
    await kit.batches.run(batch.batch_id)
    return batch.entries[0].txn_id


# ---- batch -------------------------------------------------------------------------------------
def test_mixed_batch_each_document_handled_on_its_own(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        lease = pdf("lease")
        docs = [
            ("lease.pdf", lease), ("employment.pdf", pdf("employment")), ("loan.pdf", pdf("loan")),
            ("blurry.pdf", pdf("blurry")), ("lease-again.pdf", lease), ("bad.pdf", b"garbage"),
            ("worse.pdf", None),
        ]
        batch = await kit.batches.create("ops", "test", docs)
        summary = await kit.batches.run(batch.batch_id)
        return kit, summary

    kit, summary = run(go())
    by = {d["name"]: d for d in summary["documents"]}
    assert by["lease.pdf"]["state"] == "COMPLETE" and by["lease.pdf"]["duty_rm"] == "72.00"
    assert by["employment.pdf"]["state"] == "COMPLETE" and by["employment.pdf"]["duty_rm"] == "0.00"
    assert by["loan.pdf"]["state"] == "AWAITING_APPROVAL" and by["loan.pdf"]["duty_rm"] == "25000.00"
    assert by["blurry.pdf"]["state"] == "REVIEW" and by["blurry.pdf"]["code"] == "LOW_CONFIDENCE"
    assert by["blurry.pdf"]["score"] == 0.72
    assert by["lease-again.pdf"]["duplicate_in_batch"] is True
    assert by["bad.pdf"]["state"] == "REJECTED_AT_INTAKE" and by["bad.pdf"]["code"] == "PDF_MAGIC"
    assert by["worse.pdf"]["code"] == "PDF_BASE64"
    t = summary["totals"]
    assert (t["documents"], t["unique_documents"], t["complete"], t["waiting_for_people"], t["failed"]) == (7, 6, 2, 2, 2)
    assert kit.portal.submits == 2  # only the two clean documents were filed; the duplicate was not
    assert len(kit.payments.paid) == 1  # RM0 employment contract owes nothing
    assert kit.pipeline.ledger.verify()


def test_same_contract_exported_twice_is_not_filed_or_paid_twice(tmp_path):
    """Different file bytes, same instrument: the hash cannot catch this, the fingerprint does."""
    first, second = pdf("lease"), pdf("lease")
    assert first != second

    async def go():
        kit = kit_for(tmp_path)
        batch = await kit.batches.create("ops", "t", [("a.pdf", first), ("b.pdf", second)])
        summary = await kit.batches.run(batch.batch_id)
        return kit, summary, batch

    kit, summary, batch = run(go())
    by = {d["name"]: d for d in summary["documents"]}
    # Both copies race through the pipeline in parallel: whichever claims first is filed.
    states = sorted(d["state"] for d in by.values())
    assert states == ["COMPLETE", "REVIEW"]
    loser = next(e for e in batch.entries if by[e.name]["state"] == "REVIEW")
    assert by[loser.name]["code"] == "DUPLICATE_INSTRUMENT"
    assert kit.portal.submits == 1 and kit.payments.calls == 1
    with pytest.raises(GateFailure) as exc:  # and a reviewer cannot release it either
        kit.pipeline.review_resolve(loser.txn_id, "Aisha", "approve", attest=True)
    assert exc.value.code == "NOT_RESOLVABLE"


def test_rejected_copy_releases_the_instrument_claim(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        first = await one(kit, pdf("blurry"))  # held for review, claims nothing yet
        kit.pipeline.review_resolve(first, "Aisha", "reject")
        second = await one(kit, pdf("blurry"))
        return state(kit, first), state(kit, second)

    assert run(go()) == ("REJECTED", "REVIEW")  # the second copy is simply reviewed in its own right


def test_batch_limits(tmp_path):
    async def go():
        kit = kit_for(tmp_path, batch_max_docs=2)
        with pytest.raises(GateFailure) as e1:
            await kit.batches.create("ops", "t", [])
        with pytest.raises(GateFailure) as e2:
            await kit.batches.create("ops", "t", [("a", pdf("lease")), ("b", pdf("loan")), ("c", pdf("employment"))])
        with pytest.raises(GateFailure) as e3:
            kit.batches.get("0" * 32)
        with pytest.raises(GateFailure) as e4:
            kit.batches.get("../etc")
        return e1.value.code, e2.value.code, e3.value.code, e4.value.code

    assert run(go()) == ("BATCH_EMPTY", "BATCH_TOO_LARGE", "BATCH_UNKNOWN", "BATCH_ID")


def test_rerunning_a_batch_never_files_or_pays_twice(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        batch = await kit.batches.create("ops", "t", [("lease.pdf", pdf("lease"))])
        await kit.batches.run(batch.batch_id)
        await kit.batches.run(batch.batch_id)
        again = await kit.batches.create("ops", "t", [("lease.pdf", pdf("lease"))])  # same bytes, new batch
        await kit.batches.run(again.batch_id)
        return kit

    kit = run(go())
    assert kit.portal.submits == 1 and kit.payments.calls == 1


def test_only_one_portal_session_at_a_time(tmp_path):
    active = {"now": 0, "max": 0}

    async def go():
        kit = kit_for(tmp_path, portal_concurrency=1, batch_concurrency=5)
        original = kit.portal.draft

        async def slow_draft(*a, **k):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.02)
            try:
                return await original(*a, **k)
            finally:
                active["now"] -= 1

        kit.portal.draft = slow_draft
        docs = [(f"l{i}.pdf", pdf("lease", rent=str(1000 + i * 100))) for i in range(5)]
        batch = await kit.batches.create("ops", "t", docs)
        return await kit.batches.run(batch.batch_id)

    summary = run(go())
    assert summary["totals"]["complete"] == 5 and active["max"] == 1


def test_batch_payment_cap_holds_the_rest_for_an_officer(tmp_path):
    async def go():
        kit = kit_for(tmp_path, batch_payment_cap_rm=Decimal("100"))
        batch = await kit.batches.create("ops", "t", [("lease.pdf", pdf("lease")), ("loan.pdf", pdf("loan"))])
        return kit, await kit.batches.run(batch.batch_id)

    kit, summary = run(go())
    by = {d["name"]: d for d in summary["documents"]}
    assert by["lease.pdf"]["state"] == "COMPLETE"
    assert by["loan.pdf"]["state"] == "PARITY_OK" and by["loan.pdf"]["code"] == "BATCH_CAP"
    assert kit.portal.submits == 1


def test_shadow_mode_stops_at_draft(tmp_path):
    async def go():
        kit = kit_for(tmp_path, mode="shadow")
        txn = await one(kit, pdf("lease"))
        return kit, txn

    kit, txn = run(go())
    assert state(kit, txn) == "PARITY_OK" and kit.portal.submits == 0 and kit.payments.calls == 0


# ---- human in the loop ------------------------------------------------------------------------
def test_reviewer_approves_low_confidence_document_and_it_completes(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        txn = await one(kit, pdf("blurry"))
        assert state(kit, txn) == "REVIEW"
        kit.pipeline.review_resolve(txn, "Aisha", "approve", attest=True, note="checked scan")
        res = await kit.orchestrator.advance(txn)
        return kit, txn, res

    kit, txn, res = run(go())
    ctx = kit.pipeline.store.load(txn)
    assert res.state == "COMPLETE" and ctx.reviewed_by == "Aisha" and ctx.needs_second_approval is False
    approved = [r for r in kit.pipeline.audit.records(txn) if r.event == "REVIEW_APPROVED"][0]
    assert approved.data["reviewer"] == "Aisha" and approved.data["changed_fields"] == {}
    assert ctx.verification and all(v in {"PASS", "SKIPPED"} for v in ctx.verification.values())


def test_reviewer_correction_needs_a_different_checker(tmp_path):
    async def go():
        kit = kit_for(tmp_path, classifier=demo.LyingClassifier(demo.HeuristicClassifier()))
        txn = await one(kit, pdf("lease"))
        ctx = kit.pipeline.store.load(txn)
        assert ctx.state == "REVIEW" and ctx.review.code == "UNGROUNDED_FIGURE" and ctx.review.resolvable
        fixed = ctx.candidate.model_copy(
            update={"financials": ctx.candidate.financials.model_copy(update={"monthly_rental_rm": Decimal("1500.00")})}
        )
        kit.pipeline.review_resolve(txn, "Aisha", "approve", corrections=fixed, attest=True)
        stopped = await kit.orchestrator.advance(txn)
        same = await kit.orchestrator.advance(txn, approved_by="aisha")  # same person, different case
        other = await kit.orchestrator.advance(txn, approved_by="Daniel")
        return kit, txn, stopped, same, other

    kit, txn, stopped, same, other = run(go())
    assert stopped.state == "AWAITING_APPROVAL" and stopped.halt["code"] == "MAKER_CHECKER"
    assert same.state == "AWAITING_APPROVAL" and same.halt["code"] == "FOUR_EYES"
    assert other.state == "COMPLETE"
    ctx = kit.pipeline.store.load(txn)
    assert ctx.approved_by == "Daniel" and ctx.reviewed_by == "Aisha"
    approved = [r for r in kit.pipeline.audit.records(txn) if r.event == "REVIEW_APPROVED"][0]
    assert approved.data["changed_fields"]["financials.monthly_rental_rm"][1] == '"1500.00"'
    assert approved.data["second_approval_required"] is True
    assert ctx.calc.total_duty_rm == Decimal("72.00")  # recomputed from the corrected value, not the lie


def test_review_rules(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        blurry = await one(kit, pdf("blurry"))
        p = kit.pipeline
        with pytest.raises(GateFailure) as no_attest:
            p.review_resolve(blurry, "Aisha", "approve", attest=False)
        with pytest.raises(GateFailure) as no_name:
            p.review_resolve(blurry, "  ", "approve", attest=True)
        with pytest.raises(GateFailure) as not_review:
            p.review_resolve(await one(kit, pdf("lease")), "Aisha", "approve", attest=True)
        rejected = p.review_resolve(blurry, "Aisha", "reject", note="not ours")
        again = await kit.orchestrator.advance(blurry)
        return no_attest.value.code, no_name.value.code, not_review.value.code, rejected.state, again.state

    assert run(go()) == ("ATTESTATION_REQUIRED", "REVIEWER_REQUIRED", "NOT_IN_REVIEW", "REJECTED", "REJECTED")


def test_unresolvable_holds_can_only_be_rejected(tmp_path):
    async def go():
        kit = kit_for(tmp_path)  # transfer duty is not live before 1 Jan 2027
        txn = await one(kit, pdf("transfer"))
        ctx = kit.pipeline.store.load(txn)
        with pytest.raises(GateFailure) as exc:
            kit.pipeline.review_resolve(txn, "Aisha", "approve", attest=True)
        closed = kit.pipeline.review_resolve(txn, "Aisha", "reject")
        return ctx.review, exc.value.code, closed.state

    review, code, closed = run(go())
    assert review.code == "PHASE_NOT_LIVE" and review.resolvable is False
    assert code == "NOT_RESOLVABLE" and closed == "REJECTED"


def test_human_cannot_approve_past_the_statutory_window(tmp_path):
    async def go():
        kit = kit_for(tmp_path, today=dt.date(2026, 11, 25))
        txn = await one(kit, demo.make_pdf(demo.sample_text("lease", dt.date(2026, 10, 1))))
        return kit.pipeline.store.load(txn).review

    review = run(go())
    assert review.code == "SEC47_OVERDUE" and review.resolvable is False


def test_work_queue_lists_lowest_score_first(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        batch = await kit.batches.create("ops", "t", [("loan.pdf", pdf("loan")), ("blurry.pdf", pdf("blurry"))])
        await kit.batches.run(batch.batch_id)
        return kit.pipeline.work_items(), kit.pipeline.review_detail(batch.entries[1].txn_id)

    items, detail = run(go())
    assert [i["state"] for i in items] == ["REVIEW", "AWAITING_APPROVAL"]
    assert items[0]["score"] == 0.72 and items[0]["resolvable"] is True
    assert detail["assessment"]["decision"] == "REVIEW" and "TENANCY" in detail["text"]
    assert detail["extraction"]["first_schedule_item"] == "ITEM_49_LEASE"


# ---- payment safety ---------------------------------------------------------------------------
def test_lost_reply_after_money_moved_is_reconciled_not_repaid(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        kit.payments.fail_next = "lost-reply"
        txn = await one(kit, pdf("lease"))
        parked = state(kit, txn)
        await kit.orchestrator.advance(txn)  # must not try again by itself
        calls_while_parked = kit.payments.calls
        res = await kit.pipeline.reconcile_payment(txn, "Daniel")
        final = await kit.orchestrator.advance(txn)
        return kit, txn, parked, calls_while_parked, res.state, final.state

    kit, txn, parked, calls, reconciled, final = run(go())
    assert parked == "PAYMENT_UNCERTAIN" and calls == 1 and reconciled == "PAID" and final == "COMPLETE"
    assert kit.payments.calls == 1 and len(kit.payments.paid) == 1
    assert kit.pipeline.store.load(txn).payment["source"] == "reconciled-by-Daniel"


def test_timeout_before_money_moved_retry_needs_a_named_approver(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        kit.payments.fail_next = "timeout-before"
        txn = await one(kit, pdf("lease"))
        parked = state(kit, txn)
        res = await kit.pipeline.reconcile_payment(txn, "Daniel")
        blocked = await kit.orchestrator.advance(txn)
        done = await kit.orchestrator.advance(txn, approved_by="Daniel")
        return kit, parked, res.state, blocked.halt["code"], done.state

    kit, parked, reconciled, blocked, done = run(go())
    assert (parked, reconciled, blocked, done) == ("PAYMENT_UNCERTAIN", "SUBMITTED", "PAYMENT_RETRY_NEEDS_APPROVAL", "COMPLETE")
    assert kit.payments.calls == 2 and len(kit.payments.paid) == 1


def test_definite_refusal_leaves_bill_unpaid_and_waits(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        kit.payments.fail_next = "reject"
        txn = await one(kit, pdf("lease"))
        first = state(kit, txn)
        blocked = await kit.orchestrator.advance(txn)
        done = await kit.orchestrator.advance(txn, approved_by="Daniel")
        return first, blocked.halt["code"], done.state

    assert run(go()) == ("SUBMITTED", "PAYMENT_RETRY_NEEDS_APPROVAL", "COMPLETE")


def test_already_paid_bill_is_never_paid_again(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        p = kit.pipeline
        res = p.ingest_bytes(pdf("lease"), "t")
        for stage in (p.classify, p.calculate, p.preflight):
            res = stage(res.txn_id)
        await p.draft(res.txn_id)
        await p.submit(res.txn_id)
        bill = p.store.load(res.txn_id).bill_no
        kit.payments.paid[bill] = ("EARLIER-REF", Decimal("72.00"))  # e.g. paid just before a crash
        paid = await p.pay(res.txn_id)
        return kit, paid

    kit, paid = run(go())
    assert paid.state == "PAID" and paid.data["already_paid"] is True and kit.payments.calls == 0


def test_unexpected_error_during_payment_parks_as_uncertain(tmp_path):
    async def go():
        kit = kit_for(tmp_path)

        async def boom(**kw):
            raise RuntimeError("socket exploded")

        kit.payments.pay = boom
        txn = await one(kit, pdf("lease"))
        await kit.orchestrator.advance(txn)
        return kit, txn

    kit, txn = run(go())
    ctx = kit.pipeline.store.load(txn)
    assert ctx.state == "PAYMENT_UNCERTAIN" and ctx.last_halt["code"] == "INTERNAL_ERROR_UNCERTAIN"


def test_unexpected_error_during_submit_is_never_retried(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        calls = {"n": 0}

        async def boom(draft_id):
            calls["n"] += 1
            raise RuntimeError("browser crashed after the click")

        kit.portal.submit = boom
        txn = await one(kit, pdf("lease"))
        await kit.orchestrator.advance(txn)
        await kit.orchestrator.advance(txn)
        return kit, txn, calls["n"]

    kit, txn, n = run(go())
    assert state(kit, txn) == "SUBMIT_UNCERTAIN" and n == 1


def test_submit_failure_tells_the_officer_to_check_mytax_first(tmp_path):
    async def go():
        kit = kit_for(tmp_path)

        async def fail(draft_id):
            raise GateFailure("RPA_SUBMIT", "Submit failed: timeout", "HALT")

        kit.portal.submit = fail
        txn = await one(kit, pdf("lease"))
        return kit.pipeline.store.load(txn)

    ctx = run(go())
    assert ctx.state == "HALTED" and "Check MyTax for an existing bill" in ctx.last_halt["message"]


def test_certificate_is_not_released_before_payment(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        p = kit.pipeline
        res = p.ingest_bytes(pdf("lease"), "t")
        for stage in (p.classify, p.calculate, p.preflight):
            res = stage(res.txn_id)
        await p.draft(res.txn_id)
        await p.submit(res.txn_id)
        early = await p.complete(res.txn_id)
        return early

    early = run(go())
    assert early.halt["code"] == "STAGE_ORDER"


def test_portal_refuses_certificate_for_unpaid_bill(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        d = await kit.portal.draft("SEWA_PAJAKAN", {"monthly_rent": "1500.00", "lease_months": "12"}, [])
        await kit.portal.submit(d.draft_id)
        with pytest.raises(GateFailure) as exc:
            await kit.portal.download_sijil(d.draft_id)
        return exc.value.code

    assert run(go()) == "SIJIL_NOT_AVAILABLE"


def test_payment_provider_refuses_wrong_amount(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        d = await kit.portal.draft("SEWA_PAJAKAN", {"monthly_rent": "1500.00", "lease_months": "12"}, [])
        sub = await kit.portal.submit(d.draft_id)
        with pytest.raises(GateFailure) as exc:
            await kit.payments.pay(idempotency_key="k", bill_no=sub.bill_no, virtual_account=None, amount_rm=Decimal("71.99"))
        return exc.value.code

    assert run(go()) == "PAYMENT_REJECTED"


# ---- verification and logging -----------------------------------------------------------------
def test_verification_failure_is_not_complete(tmp_path):
    async def go():
        kit = kit_for(tmp_path)

        async def not_stamped(bill_no):
            return PortalBill(False)

        kit.portal.bill_status = not_stamped
        txn = await one(kit, pdf("lease"))
        return kit.pipeline.store.load(txn)

    ctx = run(go())
    assert ctx.state == "VERIFY_FAILED" and ctx.verification["portal_shows_stamped"].startswith("FAIL")


def test_verification_passes_with_all_checks_recorded(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        return kit.pipeline.store.load(await one(kit, pdf("lease")))

    ctx = run(go())
    assert ctx.state == "COMPLETE"
    assert ctx.verification == {
        "audit_chain": "PASS", "certificate_hashed": "PASS", "vault_retention": "PASS", "amounts_agree": "PASS",
        "payment_matches_duty": "PASS", "payment_confirmed_by_provider": "PASS", "portal_shows_stamped": "PASS",
        "qr_verified": "PASS", "ledger_chain": "PASS",
    }


def test_every_stage_of_every_document_is_in_the_ledger(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        batch = await kit.batches.create("ops", "t", [("lease.pdf", pdf("lease")), ("bad.pdf", b"nope"), ("blurry.pdf", pdf("blurry"))])
        await kit.batches.run(batch.batch_id)
        return kit, batch

    kit, batch = run(go())
    rows = kit.pipeline.ledger.rows(batch_id=batch.batch_id)
    lease_stages = [r["stage"] for r in rows if r["name"] == "lease.pdf"]
    assert lease_stages == [
        "INGESTED", "CLASSIFIED", "CALCULATED", "PREFLIGHT_OK", "PARITY_OK", "submit", "SUBMITTED", "pay",
        "PAID", "VAULTED", "COMPLETE",
    ]
    assert [r["outcome"] for r in rows if r["name"] == "bad.pdf"] == ["REJECTED"]
    assert [r["outcome"] for r in rows if r["name"] == "blurry.pdf"][-1] == "REVIEW"
    assert all(r["batch_id"] == batch.batch_id for r in rows) and kit.pipeline.ledger.verify()
    assert any(r["amount_rm"] == "72.00" for r in rows)


def test_unwritable_ledger_stops_the_filing_before_anything_irreversible(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        original = kit.pipeline.ledger.append

        def broken(**kw):
            if kw["stage"] == "submit":
                raise OSError("disk full")
            return original(**kw)

        kit.pipeline.ledger.append = broken
        txn = await one(kit, pdf("lease"))
        return kit, kit.pipeline.store.load(txn)

    kit, ctx = run(go())
    assert kit.portal.submits == 0 and kit.payments.calls == 0
    assert ctx.last_halt["code"] == "INTERNAL_ERROR" and ctx.state == "HALTED"


def test_restart_resumes_from_persisted_state(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        txn = await one(kit, pdf("loan"))
        assert state(kit, txn) == "AWAITING_APPROVAL"
        fresh = demo.Orchestrator(kit.pipeline)  # as after a process restart
        waiting = await fresh.advance(txn)
        done = await fresh.advance(txn, approved_by="Daniel")
        return waiting.state, done.state

    assert run(go()) == ("AWAITING_APPROVAL", "COMPLETE")


def test_unknown_transaction_is_reported_not_raised(tmp_path):
    async def go():
        return await kit_for(tmp_path).orchestrator.advance("0" * 32)

    res = run(go())
    assert res.ok is False and res.halt["code"] == "TXN_UNKNOWN"


def test_reconcile_requires_an_uncertain_payment_and_stays_parked_if_provider_unsure(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        txn = await one(kit, pdf("lease"))
        with pytest.raises(GateFailure) as exc:
            await kit.pipeline.reconcile_payment(txn, "Daniel")
        return exc.value.code

    assert run(go()) == "NOT_UNCERTAIN"
