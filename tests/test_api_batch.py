"""HTTP behaviour: roles, the review queue, approvals, batch intake and the reviewer console."""

from __future__ import annotations

import base64
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from stsds import demo
from stsds.api import create_app
from stsds.users import UserDirectory, hash_token

NOW = dt.date(2026, 11, 10)
SIGNED = dt.date(2026, 11, 8)
T_REVIEW, T_CHECK, T_SOLO, T_OPS = "r" * 40, "c" * 40, "s" * 40, "o" * 40


def pdf64(kind: str) -> str:
    return base64.b64encode(demo.make_pdf(demo.sample_text(kind, SIGNED))).decode()


@pytest.fixture()
def world(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=T_OPS)
    users = UserDirectory([
        {"name": "Aisha", "roles": ["reviewer"], "token_sha256": hash_token(T_REVIEW)},
        {"name": "Daniel", "roles": ["checker"], "token_sha256": hash_token(T_CHECK)},
        {"name": "Solo", "roles": ["reviewer", "checker"], "token_sha256": hash_token(T_SOLO)},
    ])
    client = TestClient(create_app(kit.pipeline, kit.settings, users))
    return kit, client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def submit_batch(client, docs, **extra):
    r = client.post("/v1/batches", headers=auth(T_OPS), json={"documents": docs, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def by_name(client, batch_id):
    r = client.get(f"/v1/batches/{batch_id}", headers=auth(T_OPS))
    assert r.status_code == 200
    return r.json(), {d["name"]: d for d in r.json()["documents"]}


def test_endpoints_require_the_right_role(world):
    _kit, c = world
    assert c.get("/v1/review/queue").status_code == 401
    assert c.get("/v1/review/queue", headers=auth("x" * 40)).status_code == 401
    assert c.get("/v1/review/queue", headers=auth(T_OPS)).status_code == 403  # service token is no reviewer
    assert c.post("/v1/batches", headers=auth(T_REVIEW), json={"documents": []}).status_code == 403
    assert c.post(f"/v1/review/{'a' * 32}/decision", headers=auth(T_CHECK), json={"decision": "reject"}).status_code == 403
    assert c.post(f"/v1/approvals/{'a' * 32}", headers=auth(T_REVIEW)).status_code == 403
    assert c.get("/v1/review/queue", headers=auth(T_REVIEW)).status_code == 200


def test_batch_end_to_end_with_review_and_approval_over_http(world):
    kit, c = world
    docs = [
        {"name": "lease.pdf", "pdf_base64": pdf64("lease")},
        {"name": "loan.pdf", "pdf_base64": pdf64("loan")},
        {"name": "blurry.pdf", "pdf_base64": pdf64("blurry")},
        {"name": "broken.pdf", "pdf_base64": "%%%not-base64%%%"},
        {"name": "text.pdf", "pdf_base64": base64.b64encode(b"hello").decode()},
    ]
    made = submit_batch(c, docs)
    assert made["accepted"] == 3 and made["rejected_at_intake"] == 2
    summary, docs_by = by_name(c, made["batch_id"])  # background run has finished
    assert docs_by["lease.pdf"]["state"] == "COMPLETE"
    assert docs_by["loan.pdf"]["state"] == "AWAITING_APPROVAL" and docs_by["blurry.pdf"]["state"] == "REVIEW"
    assert docs_by["broken.pdf"]["code"] == "PDF_BASE64" and docs_by["text.pdf"]["code"] == "PDF_MAGIC"
    assert summary["totals"]["complete"] == 1 and summary["totals"]["waiting_for_people"] == 2

    # reviewer sees the unsure document first and can read what the model extracted
    items = c.get("/v1/review/queue", headers=auth(T_REVIEW)).json()["items"]
    assert [i["name"] for i in items] == ["blurry.pdf", "loan.pdf"]
    txn = items[0]["txn_id"]
    detail = c.get(f"/v1/review/{txn}", headers=auth(T_REVIEW)).json()
    assert detail["assessment"]["score"] == 0.72 and "TENANCY" in detail["text"]
    doc = c.get(f"/v1/review/{txn}/document", headers=auth(T_REVIEW))
    assert doc.status_code == 200 and doc.content.startswith(b"%PDF") and doc.headers["content-security-policy"] == "sandbox"

    # approving needs the attestation; bad corrections are refused; then it runs to the end
    assert c.post(f"/v1/review/{txn}/decision", headers=auth(T_REVIEW), json={"decision": "approve"}).status_code == 409
    bad = c.post(f"/v1/review/{txn}/decision", headers=auth(T_REVIEW),
                 json={"decision": "approve", "attest": True, "corrections": {"nonsense": 1}})
    assert bad.status_code == 422
    ok = c.post(f"/v1/review/{txn}/decision", headers=auth(T_REVIEW), json={"decision": "approve", "attest": True})
    assert ok.status_code == 200 and ok.json()["state"] == "COMPLETE"
    assert kit.pipeline.store.load(txn).reviewed_by == "Aisha"  # identity comes from the token, not the body

    # the high-value loan needs a checker; a reviewer-only account cannot approve it
    loan = docs_by["loan.pdf"]["txn_id"]
    assert c.post(f"/v1/approvals/{loan}", headers=auth(T_REVIEW)).status_code == 403
    assert c.post(f"/v1/approvals/{txn}", headers=auth(T_CHECK)).status_code == 409  # not waiting any more
    done = c.post(f"/v1/approvals/{loan}", headers=auth(T_CHECK))
    assert done.status_code == 200 and done.json()["state"] == "COMPLETE"
    assert kit.pipeline.store.load(loan).approved_by == "Daniel"

    final, _ = by_name(c, made["batch_id"])
    assert final["totals"]["complete"] == 3 and final["totals"]["duty_completed_rm"] == "25360.00"


def test_four_eyes_is_enforced_at_the_endpoint(world):
    kit, c = world
    kit.pipeline.classifier = demo.LyingClassifier(demo.HeuristicClassifier())
    made = submit_batch(c, [{"name": "lease.pdf", "pdf_base64": pdf64("lease")}])
    _s, docs = by_name(c, made["batch_id"])
    txn = docs["lease.pdf"]["txn_id"]
    assert docs["lease.pdf"]["code"] == "UNGROUNDED_FIGURE"
    fixed = kit.pipeline.review_detail(txn)["extraction"]
    fixed["financials"]["monthly_rental_rm"] = "1500.00"
    r = c.post(f"/v1/review/{txn}/decision", headers=auth(T_SOLO),
               json={"decision": "approve", "attest": True, "corrections": fixed})
    assert r.json()["state"] == "AWAITING_APPROVAL"
    same = c.post(f"/v1/approvals/{txn}", headers=auth(T_SOLO)).json()
    assert same["state"] == "AWAITING_APPROVAL" and same["halt"]["code"] == "FOUR_EYES"
    other = c.post(f"/v1/approvals/{txn}", headers=auth(T_CHECK)).json()
    assert other["state"] == "COMPLETE"


def test_reject_and_unresolvable_via_http(world):
    kit, c = world
    made = submit_batch(c, [{"name": "t.pdf", "pdf_base64": pdf64("transfer")}])
    _s, docs = by_name(c, made["batch_id"])
    txn = docs["t.pdf"]["txn_id"]
    assert docs["t.pdf"]["code"] == "PHASE_NOT_LIVE"
    assert c.post(f"/v1/review/{txn}/decision", headers=auth(T_REVIEW),
                  json={"decision": "approve", "attest": True}).status_code == 409
    rej = c.post(f"/v1/review/{txn}/decision", headers=auth(T_REVIEW), json={"decision": "reject", "note": "wrong tenant"})
    assert rej.json()["state"] == "REJECTED"


def test_ids_are_validated_and_unknowns_are_404(world):
    _kit, c = world
    assert c.get("/v1/batches/not-a-hex-id", headers=auth(T_OPS)).status_code == 400
    assert c.get(f"/v1/batches/{'0' * 32}", headers=auth(T_OPS)).status_code == 404
    assert c.post(f"/v1/batches/{'0' * 32}/run", headers=auth(T_OPS)).status_code == 404
    assert c.get(f"/v1/review/{'0' * 32}", headers=auth(T_REVIEW)).status_code == 404
    assert c.get("/v1/review/..%2F..%2Fetc", headers=auth(T_REVIEW)).status_code in {400, 404}
    assert c.get(f"/v1/review/{'0' * 32}/document", headers=auth(T_REVIEW)).status_code == 404
    assert c.post("/v1/batches", headers=auth(T_OPS), json={"documents": []}).status_code == 422
    assert c.get("/v1/ledger?batch_id=zz", headers=auth(T_OPS)).status_code == 400


def test_manual_run_and_advance_are_idempotent(world):
    kit, c = world
    made = submit_batch(c, [{"name": "lease.pdf", "pdf_base64": pdf64("lease")}], auto_run=False)
    _s, docs = by_name(c, made["batch_id"])
    txn = docs["lease.pdf"]["txn_id"]
    assert docs["lease.pdf"]["state"] == "INGESTED" and kit.portal.submits == 0
    first = c.post(f"/v1/batches/{made['batch_id']}/run", headers=auth(T_OPS)).json()
    assert first["totals"]["complete"] == 1
    again = c.post(f"/v1/advance/{txn}", headers=auth(T_OPS)).json()
    assert again["state"] == "COMPLETE" and kit.portal.submits == 1 and kit.payments.calls == 1


def test_reports_and_ledger_exports_are_safe_and_complete(world):
    kit, c = world
    made = submit_batch(c, [{"name": '=cmd|" /C calc"!A0.pdf', "pdf_base64": pdf64("lease")}])
    csv_text = c.get(f"/v1/batches/{made['batch_id']}/report.csv", headers=auth(T_OPS)).text
    assert "'=cmd" in csv_text and "COMPLETE" in csv_text and csv_text.splitlines()[0].startswith("name,txn_id,state")
    led = c.get(f"/v1/ledger?batch_id={made['batch_id']}", headers=auth(T_OPS)).json()
    assert led["chain_valid"] is True and {r["stage"] for r in led["rows"]} >= {"INGESTED", "PAID", "COMPLETE"}
    exported = c.get(f"/v1/ledger.csv?batch_id={made['batch_id']}", headers=auth(T_CHECK)).text
    assert "'=cmd" in exported and exported.count("\n") == len(led["rows"]) + 1


def test_payment_reconcile_endpoint(world):
    kit, c = world
    kit.payments.fail_next = "lost-reply"
    made = submit_batch(c, [{"name": "lease.pdf", "pdf_base64": pdf64("lease")}])
    _s, docs = by_name(c, made["batch_id"])
    txn = docs["lease.pdf"]["txn_id"]
    assert docs["lease.pdf"]["state"] == "PAYMENT_UNCERTAIN"
    assert c.post(f"/v1/payments/{txn}/reconcile", headers=auth(T_REVIEW)).status_code == 403
    queue = c.get("/v1/review/queue", headers=auth(T_CHECK)).json()["items"]
    assert queue[0]["state"] == "PAYMENT_UNCERTAIN"
    done = c.post(f"/v1/payments/{txn}/reconcile", headers=auth(T_CHECK)).json()
    assert done["state"] == "COMPLETE" and kit.payments.calls == 1
    assert c.post(f"/v1/payments/{txn}/reconcile", headers=auth(T_CHECK)).status_code == 409


def test_console_is_served_with_a_strict_policy(world):
    _kit, c = world
    page = c.get("/console")
    csp = page.headers["content-security-policy"]
    assert page.status_code == 200 and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "<script>" not in page.text.replace('<script src="/console/app.js"></script>', "")
    assert page.headers["cache-control"] == "no-store" and page.headers["x-content-type-options"] == "nosniff"
    js = c.get("/console/app.js")
    assert js.status_code == 200 and "innerHTML" not in js.text and "localStorage" not in js.text
    assert "sessionStorage" not in js.text and "eval(" not in js.text
    assert c.get("/console/app.css").status_code == 200


def test_service_only_deployment_has_no_human_actions(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=T_OPS)
    c = TestClient(create_app(kit.pipeline, kit.settings))  # no user directory configured
    assert c.get("/v1/review/queue", headers=auth(T_OPS)).status_code == 403
    assert c.post(f"/v1/approvals/{'a' * 32}", headers=auth(T_OPS)).status_code == 403
