from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from stsds import payment, scoring
from stsds.gates import GateFailure, sec47_clock
from stsds.ledger import GENESIS, Ledger, csv_safe
from stsds.security import AuthError
from stsds.users import UserDirectory, hash_token

from .conftest import TODAY, make_cls

LEASE_TEXT = (
    "TENANCY AGREEMENT made on 2 November 2026. The Tenant shall pay monthly rent of RM1,500.00 "
    "for a term of 12 months. Alpha Sdn Bhd and Beta Sdn Bhd confirm this agreement."
)


def _assess(c, text=LEASE_TEXT, threshold=0.95, today=TODAY, rules=None, sha=None):
    return scoring.assess(c, text, sec47_clock(c.execution_date, today), rules, sha, threshold)


def lease(rules, sha, **fin):
    c = make_cls(financials={"monthly_rental_rm": "1500.00", "lease_duration_months": 12, **fin})
    return c, rules, sha


# ---- scoring ------------------------------------------------------------------------------------
def test_clean_document_scores_high_and_auto(rules, rules_sha):
    c, r, s = lease(rules, rules_sha)
    a = _assess(c, rules=r, sha=s)
    assert a.decision == "AUTO" and a.score == 0.99
    assert a.components["grounding"] == 1.0 and a.reasons == []


def test_ungrounded_figure_pulls_score_below_threshold(rules, rules_sha):
    c, r, s = lease(rules, rules_sha, monthly_rental_rm="900.00")
    a = _assess(c, rules=r, sha=s)
    assert a.decision == "REVIEW" and a.components["grounding"] < 1.0
    assert any("monthly_rental_rm" in x for x in a.reasons)


def test_score_is_product_so_zero_component_zeroes_it(rules, rules_sha):
    c = make_cls(financials={"monthly_rental_rm": "1500.00"})  # lease term missing -> engine needs review
    a = _assess(c, rules=rules, sha=rules_sha)
    assert a.components["completeness"] == 0.0 and a.score == 0.0 and a.decision == "REVIEW"


def test_overdue_and_bad_party_and_tiny_text(rules, rules_sha):
    c, r, s = lease(rules, rules_sha)
    late = _assess(c, today=dt.date(2027, 1, 20), rules=r, sha=s)
    assert late.components["clock"] == 0.0 and late.score == 0.0
    bad = c.model_copy(update={"parties": [c.parties[0].model_copy(update={"tin": "bad"}), c.parties[1]]})
    assert _assess(bad, rules=r, sha=s).components["parties"] == 0.5
    assert _assess(bad.model_copy(update={"parties": []}), rules=r, sha=s).components["parties"] == 0.0
    assert _assess(c, text="RM1,500.00 2 November 2026 12 months", rules=r, sha=s).components["text_quality"] < 1.0


def test_jpph_and_threshold(rules, rules_sha):
    c, r, s = lease(rules, rules_sha)
    jp = c.model_copy(update={"requires_jpph_valuation": True})
    assert _assess(jp, rules=r, sha=s).components["completeness"] == 0.0
    assert _assess(c, threshold=0.995, rules=r, sha=s).decision == "REVIEW"


# ---- ledger -------------------------------------------------------------------------------------
def _row(led, i=0, **kw):
    return led.append(txn_id="a" * 32, stage=f"s{i}", outcome="OK", state="X", batch_id="b" * 32, **kw)


def test_ledger_chain_persists_across_reopen_and_detects_tamper(tmp_path):
    path = tmp_path / "l" / "ledger.jsonl"
    led = Ledger(path)
    first = _row(led, 0, amount_rm="72.00")
    assert first["seq"] == 0 and led.verify()
    _row(led, 1)
    again = Ledger(path)  # reopen: continues the chain
    assert _row(again, 2)["seq"] == 2 and again.verify()
    assert len(again.rows(batch_id="b" * 32)) == 3 and again.rows(txn_id="c" * 32) == []
    lines = path.read_text().splitlines()
    forged = json.loads(lines[1])
    forged["amount_rm"] = "1.00"
    path.write_text("\n".join([lines[0], json.dumps(forged), lines[2]]) + "\n")
    assert not Ledger(path).verify()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # deleted row
    assert not Ledger(path).verify()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_ledger_csv_neutralises_formula_injection(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    led.append(txn_id="a" * 32, stage="intake", outcome="OK", state="X", name="=HYPERLINK(\"http://evil\")", detail="+1")
    out = Ledger.to_csv(led.rows())
    assert "'=HYPERLINK" in out and "'+1" in out
    assert csv_safe("-2") == "'-2" and csv_safe(None) == "" and csv_safe("safe") == "safe"
    assert GENESIS == "0" * 64 and Ledger(tmp_path / "empty.jsonl").rows() == []


# ---- users --------------------------------------------------------------------------------------
TOKEN = "r" * 40


def _dir(**kw):
    return UserDirectory([{"name": "Aisha", "roles": ["reviewer"], "token_sha256": hash_token(TOKEN)}, *kw.get("extra", [])])


def test_user_directory_authenticates_by_token():
    d = _dir()
    who = d.authenticate(f"Bearer {TOKEN}")
    assert who.name == "Aisha" and who.has("reviewer") and not who.has("checker")
    for bad in (None, "", "Basic abc", "Bearer short", f"Bearer {'x' * 40}"):
        with pytest.raises(AuthError):
            d.authenticate(bad)


def test_user_directory_validation(tmp_path):
    ok = {"name": "A", "roles": ["reviewer"], "token_sha256": hash_token(TOKEN)}
    with pytest.raises(ValueError):
        UserDirectory([{**ok, "roles": ["admin"]}])
    with pytest.raises(ValueError):
        UserDirectory([{**ok, "name": "bad<name>"}])
    with pytest.raises(ValueError):
        UserDirectory([ok, {**ok, "name": "a", "token_sha256": hash_token("q" * 40)}])
    with pytest.raises(ValueError):
        UserDirectory([{**ok, "token_sha256": "short"}])
    f = tmp_path / "u.json"
    f.write_text(json.dumps({"users": [ok]}))
    assert UserDirectory.load(f).authenticate(f"Bearer {TOKEN}").name == "A"


# ---- payment provider adapter -------------------------------------------------------------------
def _client(monkeypatch, handler):
    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)


def _pay(prov):
    return asyncio.run(prov.pay(idempotency_key="k1", bill_no="B1", virtual_account="VA", amount_rm=Decimal("72.00")))


def test_provider_success_sends_idempotency_key(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["key"], seen["body"] = req.headers["Idempotency-Key"], json.loads(req.content)
        return httpx.Response(200, json={"reference": "R1", "amount_rm": "72.00", "status": "PAID"})

    _client(monkeypatch, handler)
    res = _pay(payment.HttpPaymentProvider("https://pay.example", "t" * 20))
    assert res.status == "PAID" and res.amount_rm == Decimal("72.00") and seen["key"] == "k1"
    assert seen["body"]["amount_rm"] == "72.00" and seen["body"]["bill_no"] == "B1"


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (lambda r: httpx.Response(503), "PAYMENT_UNCERTAIN"),
        (lambda r: httpx.Response(400), "PAYMENT_REJECTED"),
        (lambda r: httpx.Response(200, text="not json"), "PAYMENT_UNCERTAIN"),
        (lambda r: httpx.Response(200, json={"status": "PAID"}), "PAYMENT_UNCERTAIN"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")), "PAYMENT_UNCERTAIN"),
        (lambda r: httpx.Response(200, json={"reference": "R", "amount_rm": "abc", "status": "PAID"}), "PAYMENT_UNPARSEABLE"),
    ],
)
def test_provider_failure_classification(monkeypatch, handler, code):
    _client(monkeypatch, handler)
    with pytest.raises(GateFailure) as exc:
        _pay(payment.HttpPaymentProvider("https://pay.example", "t" * 20))
    assert exc.value.code == code


def test_provider_status_and_config(monkeypatch):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/PAIDBILL"):
            return httpx.Response(200, json={"status": "PAID", "amount_rm": "72.00", "reference": "R"})
        if req.url.path.endswith("/ODD"):
            return httpx.Response(200, json={"status": "PENDING"})
        if req.url.path.endswith("/BROKEN"):
            return httpx.Response(500)
        return httpx.Response(404)

    _client(monkeypatch, handler)
    prov = payment.HttpPaymentProvider("https://pay.example/", "t" * 20)
    assert asyncio.run(prov.status("PAIDBILL")).payment == "PAID"
    assert asyncio.run(prov.status("NOPE")).payment == "UNPAID"
    assert asyncio.run(prov.status("ODD")).payment == "UNKNOWN"
    with pytest.raises(GateFailure) as exc:
        asyncio.run(prov.status("BROKEN"))
    assert exc.value.code == "PAYMENT_STATUS_ERROR"
    with pytest.raises(GateFailure) as exc2:
        _pay(payment.HttpPaymentProvider(None, None))
    assert exc2.value.code == "PAYMENT_UNCONFIGURED"
    with pytest.raises(ValueError):
        payment.HttpPaymentProvider("http://insecure.example", "t")


def test_user_creation_cli_prints_token_once_and_only_a_hash_entry(capsys):
    from stsds import users

    assert users.main(["Aisha Rahman", "reviewer", "checker"]) == 0
    out = capsys.readouterr().out
    token = [ln.strip() for ln in out.splitlines() if len(ln.strip()) == 43][0]
    entry = json.loads(out[out.index("{"):])
    assert entry["token_sha256"] == hash_token(token) and entry["roles"] == ["checker", "reviewer"]
    assert token not in json.dumps(entry)
    assert UserDirectory([entry]).authenticate(f"Bearer {token}").name == "Aisha Rahman"
    with pytest.raises(ValueError):
        users.new_user_entry("bad<name>", ["reviewer"])
