"""New instrument types (held until sign-off), late-penalty estimate, contract register, monitoring, API."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from stsds import demo, engine
from stsds.api import create_app
from stsds.gates import GateFailure, phase_gate
from stsds.register import (
    ManualIn,
    OverrideIn,
    Register,
    Status,
    alerts,
    monthly_report,
    penalty_estimate,
    summary,
    to_csv,
    unstamped,
)
from stsds.schemas import Classification, DocKind, Item
from stsds.users import UserDirectory, hash_token

NOW = dt.date(2026, 11, 10)
OPS, REV, CHK = "o" * 40, "r" * 40, "c" * 40


def run(coro):
    return asyncio.run(coro)


def cls(item, module, **fin):
    return Classification.model_validate({
        "bnds_module": module, "first_schedule_item": item, "requires_jpph_valuation": False,
        "is_foreign_buyer_or_entity": False, "financials": fin,
        "parties": [p.model_dump() for p in demo.DEMO_PARTIES], "execution_date": "2026-11-01", "confidence": 0.99})


# ---- new instruments: engine ---------------------------------------------------------------------
def test_nominal_agreement_is_ten_ringgit(rules, rules_sha):
    r = engine.calculate(cls("ITEM_4_GENERAL", "PERJANJIAN_AM"), rules, rules_sha)
    assert r.total_duty_rm == Decimal("10.00")


@pytest.mark.parametrize("amount,fixed,expected", [
    ("120000.00", True, "600.00"),     # 120 units x RM5
    ("120000.00", False, "1200.00"),   # 120 units x RM10
    ("1000.01", True, "10.00"),        # 2 units x RM5 = RM10
    ("1.00", True, "10.00"),           # 1 unit x RM5 = 5, lifted to the RM10 minimum
])
def test_loan_or_service_agreement_ad_valorem(rules, rules_sha, amount, fixed, expected):
    c = cls("ITEM_22_AGREEMENT", "PERJANJIAN_PINJAMAN", consideration_or_loan_rm=amount, fixed_tenure=fixed)
    assert engine.calculate(c, rules, rules_sha).total_duty_rm == Decimal(expected)


def test_agreement_needs_a_stated_tenure_and_sum(rules, rules_sha):
    with pytest.raises(engine.NeedsReview) as e:
        engine.calculate(cls("ITEM_22_AGREEMENT", "PERJANJIAN_PINJAMAN", consideration_or_loan_rm="5000.00"), rules, rules_sha)
    assert e.value.code == "MISSING_TENURE"
    with pytest.raises(engine.NeedsReview) as e2:
        engine.calculate(cls("ITEM_22_AGREEMENT", "PERJANJIAN_PINJAMAN", fixed_tenure=True), rules, rules_sha)
    assert e2.value.code == "MISSING_PRINCIPAL"


@pytest.mark.parametrize("consideration,nta,expected", [
    ("250000.00", "300000.00", "900.00"),   # NTA is higher
    ("400000.00", "300000.00", "1200.00"),  # consideration is higher
    ("333.33", "100.00", "10.00"),          # RM1 + part, lifted to the minimum
])
def test_share_transfer_uses_the_higher_value(rules, rules_sha, consideration, nta, expected):
    c = cls("ITEM_32_SHARES", "PEMINDAHAN_SAHAM", consideration_or_loan_rm=consideration, net_tangible_assets_rm=nta)
    assert engine.calculate(c, rules, rules_sha).total_duty_rm == Decimal(expected)


def test_share_transfer_without_nta_fails_closed(rules, rules_sha):
    with pytest.raises(engine.NeedsReview) as e:
        engine.calculate(cls("ITEM_32_SHARES", "PEMINDAHAN_SAHAM", consideration_or_loan_rm="1000.00"), rules, rules_sha)
    assert e.value.code == "MISSING_NTA"


def test_module_must_match_item():
    with pytest.raises(ValueError):
        cls("ITEM_32_SHARES", "PERJANJIAN_AM")


@pytest.mark.parametrize("item", ["ITEM_4_GENERAL", "ITEM_22_AGREEMENT", "ITEM_32_SHARES"])
def test_new_items_are_not_live_in_the_real_rules_file(rules, item):
    with pytest.raises(GateFailure) as e:
        phase_gate(Item(item), dt.date(2026, 11, 10), dt.date(2026, 11, 10), rules)
    assert e.value.code == "PHASE_NOT_LIVE"


@pytest.mark.parametrize("kind,duty", [("service", "600.00"), ("shareholders", "10.00"), ("intercompany", "10.00"),
                                       ("shares", "900.00"), ("loanu", "1000.00")])
def test_new_documents_run_end_to_end_in_the_simulated_world_and_match_the_portal(tmp_path, kind, duty):
    async def go():
        kit = demo.Kit(tmp_path, today=NOW)
        text = demo.sample_text(kind, NOW - dt.timedelta(days=2))
        b = await kit.batches.create("ops", "t", [("doc.pdf", demo.make_pdf(text))])
        await kit.batches.run(b.batch_id)
        return kit.pipeline.store.load(b.entries[0].txn_id)

    ctx = run(go())
    assert ctx.state == "COMPLETE", ctx.last_halt
    assert str(ctx.calc.total_duty_rm) == duty == str(ctx.lhdn_total)


def test_a_wrong_nta_figure_cannot_be_taxed_ungrounded(tmp_path):
    from stsds.gates import grounding_report
    c = cls("ITEM_32_SHARES", "PEMINDAHAN_SAHAM", consideration_or_loan_rm="250000.00", net_tangible_assets_rm="999999.00")
    missing, _ = grounding_report(c, "SHARE TRANSFER FORM 1 November 2026 consideration RM250,000.00 NTA RM300,000.00")
    assert "net_tangible_assets_rm" in missing


# ---- late-stamping penalty estimate --------------------------------------------------------------
DEADLINE = dt.date(2026, 10, 1)


def test_no_penalty_on_or_before_the_deadline(rules):
    assert penalty_estimate(Decimal("500"), DEADLINE, DEADLINE, rules.penalty) is None
    assert penalty_estimate(Decimal("500"), DEADLINE, DEADLINE - dt.timedelta(days=3), rules.penalty) is None


@pytest.mark.parametrize("deadline,as_of,duty,pct,amount", [
    (dt.date(2026, 10, 1), dt.date(2026, 10, 2), "100.00", 10, "50.00"),      # 10% = 10, below the RM50 floor
    (dt.date(2026, 10, 1), dt.date(2026, 10, 2), "1000.00", 10, "100.00"),    # 10% = 100, above the floor
    (dt.date(2026, 9, 1), dt.date(2026, 12, 1), "100.00", 10, "50.00"),       # exactly 3 months after: first band
    (dt.date(2026, 9, 1), dt.date(2026, 12, 2), "1000.00", 20, "200.00"),     # a day past 3 months: 20% of 1000
    (dt.date(2025, 8, 1), dt.date(2026, 1, 1), "100.00", 20, "100.00"),       # later band, RM100 floor
    (dt.date(2026, 11, 30), dt.date(2027, 2, 28), "100.00", 10, "50.00"),     # month-end handling (30 Nov + 3 months)
])
def test_penalty_bands_and_minimums(rules, deadline, as_of, duty, pct, amount):
    p = penalty_estimate(Decimal(duty), deadline, as_of, rules.penalty)
    assert p and p.percent == pct and p.amount_rm == Decimal(amount) and p.duty_known


def test_penalty_with_unknown_duty_is_the_minimum_and_says_so(rules):
    p = penalty_estimate(None, DEADLINE, DEADLINE + dt.timedelta(days=5), rules.penalty)
    assert p and p.amount_rm == Decimal("50.00") and not p.duty_known


# ---- register and monitoring ----------------------------------------------------------------------
@pytest.fixture
def reg(tmp_path, rules):
    return Register(tmp_path / "r.sqlite3", rules.penalty)


def manual(**kw):
    base = dict(name="Contract", entity="Alpha Sdn Bhd", doc_kind=DocKind.SERVICE, execution_date=NOW - dt.timedelta(days=10))
    base.update(kw)
    return ManualIn(**base)


def test_status_is_derived_from_dates_and_certificate(reg):
    reg.add_manual(manual(name="stamped", certificate_ref="C1", stamped_on=NOW - dt.timedelta(days=5)), "t")
    reg.add_manual(manual(name="open"), "t")
    reg.add_manual(manual(name="late", execution_date=NOW - dt.timedelta(days=60), duty_rm=Decimal("1000")), "t")
    reg.add_manual(manual(name="exempt", execution_date=NOW - dt.timedelta(days=60), not_required=True), "t")
    by = {e.name: e for e in reg.entries([], NOW)}
    assert by["stamped"].status is Status.STAMPED and by["stamped"].penalty is None
    assert by["open"].status is Status.UNSTAMPED and by["open"].days_to_deadline == 20
    assert by["late"].status is Status.OVERDUE and by["late"].penalty.amount_rm == Decimal("100.00")
    assert by["exempt"].status is Status.NOT_REQUIRED


def test_late_stamped_contract_records_the_penalty_incurred(reg):
    reg.add_manual(manual(execution_date=NOW - dt.timedelta(days=100), duty_rm=Decimal("2000"),
                          certificate_ref="C9", stamped_on=NOW - dt.timedelta(days=20)), "t")
    (e,) = reg.entries([], NOW)
    assert e.status is Status.STAMPED and e.penalty and e.penalty.amount_rm >= Decimal("50.00")


def test_alerts_cover_overdue_due_soon_and_expiry(reg):
    reg.add_manual(manual(name="o", execution_date=NOW - dt.timedelta(days=50)), "t")
    reg.add_manual(manual(name="s", execution_date=NOW - dt.timedelta(days=26)), "t")
    reg.add_manual(manual(name="e", certificate_ref="C", stamped_on=NOW, expiry_date=NOW + dt.timedelta(days=30)), "t")
    reg.add_manual(manual(name="x", certificate_ref="C", stamped_on=NOW, expiry_date=NOW - dt.timedelta(days=2)), "t")
    reg.add_manual(manual(name="far", certificate_ref="C", stamped_on=NOW, expiry_date=NOW + dt.timedelta(days=400)), "t")
    got = {a["name"]: a["level"] for a in alerts(reg.entries([], NOW), NOW)}
    assert got == {"o": "OVERDUE", "x": "EXPIRED", "s": "DUE_SOON", "e": "EXPIRING"}


def test_summary_and_monthly_report(reg):
    reg.add_manual(manual(name="a", certificate_ref="C", stamped_on=NOW - dt.timedelta(days=1), duty_rm=Decimal("100")), "t")
    reg.add_manual(manual(name="b", execution_date=NOW - dt.timedelta(days=45), duty_rm=Decimal("1000")), "t")
    entries = reg.entries([], NOW)
    s = summary(entries, NOW)
    assert s["by_status"] == {"STAMPED": 1, "OVERDUE": 1} and s["compliance_rate_pct"] == 50.0
    assert s["penalty_exposure_rm"] == Decimal("100.00") and s["duty_stamped_rm"] == Decimal("100")
    rep = monthly_report(entries, 2026, 11, NOW)
    assert [e.name for e in rep["stamped"]] == ["a"] and [e.name for e in unstamped(entries)] == ["b"]


def test_overrides_on_a_pipeline_entry_and_history(tmp_path, rules):
    async def go():
        kit = demo.Kit(tmp_path, today=NOW)
        text = demo.sample_text("service", NOW - dt.timedelta(days=2))
        b = await kit.batches.create("ops", "t", [("svc.pdf", demo.make_pdf(text))])
        await kit.batches.run(b.batch_id)
        return kit, b.entries[0].txn_id

    kit, txn = run(go())
    reg = Register(tmp_path / "x.sqlite3", rules.penalty)
    ctx = kit.pipeline.store.load(txn)
    (e,) = reg.entries([ctx], NOW)
    assert e.source == "pipeline" and e.status is Status.STAMPED and e.doc_kind is DocKind.SERVICE
    assert e.contract_owner == "Aisha Rahman" and e.duty_rm == Decimal("600.00") and e.certificate_ref
    assert reg.update(txn, OverrideIn(entity="Renamed Sdn Bhd", renewal_date=dt.date(2027, 1, 1)), "Checker A")
    (e2,) = reg.entries([ctx], NOW)
    assert e2.entity == "Renamed Sdn Bhd" and e2.renewal_date == dt.date(2027, 1, 1)
    hist = reg.history(txn)
    assert hist[0]["actor"] == "Checker A" and "updated" in hist[0]["change"]


def test_update_rejects_bad_ids_and_invalid_manual_edits(reg):
    assert not reg.update("../../etc/passwd", OverrideIn(notes="x"), "t")
    cid = reg.add_manual(manual(), "t")
    assert reg.update(cid, OverrideIn(entity="New"), "t")
    assert not reg.update(cid, OverrideIn(entity=None), "t")  # would blank a required field
    assert reg.entries([], NOW)[0].entity == "New"


GOOD_CSV = "name,entity,doc_kind,execution_date,duty_rm\nA,Alpha,SERVICE,2026-10-30,10.00\nB,Alpha,LOAN,2026-10-01,\n"


def test_csv_import_all_or_nothing(reg):
    assert reg.import_csv(GOOD_CSV, "t") == {"imported": 2, "errors": []}
    bad = GOOD_CSV + "C,Alpha,NOPE,2026-10-01,\n"
    res = reg.import_csv(bad, "t")
    assert res["imported"] == 0 and res["errors"] and len(reg.entries([], NOW)) == 2
    assert reg.import_csv("name,entity\n", "t")["imported"] == 0
    assert reg.import_csv("x" * 1_100_000, "t")["errors"] == ["file too large"]


def test_csv_export_neutralises_formula_injection(reg):
    reg.add_manual(manual(name="=HYPERLINK(\"http://evil\")", entity="+cmd"), "t")
    out = to_csv(reg.entries([], NOW))
    assert "'=HYPERLINK" in out and "'+cmd" in out


# ---- API ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, rules):
    kit = demo.Kit(tmp_path, engine_token=OPS, today=NOW)
    users = UserDirectory([
        {"name": "Rita", "roles": ["reviewer"], "token_sha256": hash_token(REV)},
        {"name": "Chen", "roles": ["checker"], "token_sha256": hash_token(CHK)},
    ])
    register = Register(tmp_path / "api.sqlite3", rules.penalty)
    return TestClient(create_app(kit.pipeline, kit.settings, users, register=register))


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


def test_register_requires_authentication_and_the_right_role(client):
    body = {"name": "n", "entity": "e", "doc_kind": "SERVICE", "execution_date": "2026-10-30"}
    assert client.get("/v1/register").status_code == 401
    assert client.get("/v1/register", headers=hdr("z" * 40)).status_code == 401
    assert client.get("/v1/register", headers=hdr(REV)).status_code == 200            # reviewers may read
    assert client.post("/v1/register", json=body, headers=hdr(REV)).status_code == 403  # but not write
    assert client.get("/v1/register.csv", headers=hdr(REV)).status_code == 403
    assert client.post("/v1/register", json=body, headers=hdr(CHK)).status_code == 200


def test_register_api_round_trip(client):
    body = {"name": "Lease X", "entity": "Alpha", "doc_kind": "TENANCY", "execution_date": "2026-09-01",
            "duty_rm": "480.00", "expiry_date": "2026-12-20"}
    cid = client.post("/v1/register", json=body, headers=hdr(CHK)).json()["id"]
    rows = client.get("/v1/register?status=overdue", headers=hdr(REV)).json()
    assert rows["total"] == 1 and rows["items"][0]["id"] == cid and rows["items"][0]["penalty"]["amount_rm"] == "50.00"
    assert client.get("/v1/register?q=nomatch", headers=hdr(REV)).json()["total"] == 0
    assert client.patch(f"/v1/register/{cid}", json={"notes": "chased"}, headers=hdr(CHK)).status_code == 200
    assert client.patch("/v1/register/" + "0" * 32, json={"notes": "x"}, headers=hdr(CHK)).status_code == 200  # override slot
    assert client.patch("/v1/register/not-an-id", json={"notes": "x"}, headers=hdr(CHK)).status_code == 404
    assert client.patch(f"/v1/register/{cid}", json={"bogus": 1}, headers=hdr(CHK)).status_code == 422
    assert [h["actor"] for h in client.get(f"/v1/register/{cid}/history", headers=hdr(REV)).json()["items"]] == ["Chen", "Chen"]
    s = client.get("/v1/monitoring/summary", headers=hdr(REV)).json()
    assert s["by_status"] == {"OVERDUE": 1} and Decimal(s["penalty_exposure_rm"]) == Decimal("50.00")
    kinds = {a["level"] for a in client.get("/v1/monitoring/alerts", headers=hdr(REV)).json()["items"]}
    assert kinds == {"OVERDUE", "EXPIRING"}
    assert len(client.get("/v1/monitoring/unstamped", headers=hdr(REV)).json()["items"]) == 1
    assert client.get("/v1/reports/monthly?month=2026-11", headers=hdr(REV)).json()["month"] == "2026-11"
    assert client.get("/v1/reports/monthly?month=2026-13", headers=hdr(REV)).status_code == 422
    assert client.get("/v1/reports/monthly?month=bad", headers=hdr(REV)).status_code == 422
    assert client.get("/v1/register.csv", headers=hdr(CHK)).text.startswith("name,entity")


def test_register_validation_and_import_api(client):
    bad = {"name": "n", "entity": "e", "doc_kind": "SERVICE", "execution_date": "not-a-date"}
    assert client.post("/v1/register", json=bad, headers=hdr(CHK)).status_code == 422
    extra = {"name": "n", "entity": "e", "doc_kind": "SERVICE", "execution_date": "2026-10-30", "admin": True}
    assert client.post("/v1/register", json=extra, headers=hdr(CHK)).status_code == 422
    ok = client.post("/v1/register/import", content=GOOD_CSV.encode(), headers=hdr(CHK))
    assert ok.status_code == 200 and ok.json()["imported"] == 2
    assert client.post("/v1/register/import", content=b"name,entity\nonly,two\n", headers=hdr(CHK)).status_code == 422
    assert client.post("/v1/register/import", content=b"\xff\xfe", headers=hdr(CHK)).status_code == 400
    assert client.post("/v1/register/import", content=GOOD_CSV.encode(), headers=hdr(REV)).status_code == 403
    assert client.post("/v1/register/import", content=b"x" * 1_100_000, headers=hdr(CHK)).status_code == 413


def test_register_lists_pipeline_documents_from_a_real_batch(client, tmp_path):
    docs = [{"name": "svc.pdf", "pdf_base64": base64.b64encode(
        demo.make_pdf(demo.sample_text("service", NOW - dt.timedelta(days=2)))).decode()}]
    made = client.post("/v1/batches", json={"source": "t", "documents": docs, "auto_run": True}, headers=hdr(OPS))
    assert made.status_code == 200
    import time
    for _ in range(40):
        rows = client.get("/v1/register", headers=hdr(CHK)).json()["items"]
        if rows and rows[0]["status"] == "STAMPED":
            break
        time.sleep(0.25)
    assert rows[0]["source"] == "pipeline" and rows[0]["doc_kind"] == "SERVICE" and rows[0]["duty_rm"] == "600.00"
