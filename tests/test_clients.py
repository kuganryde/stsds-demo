"""Client master: registration before stamping, four-eyes activation, the preflight gate and auto-release."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stsds import demo
from stsds.api import create_app
from stsds.clients import (
    CLIENT_HOLD_CODES,
    ClientDirectory,
    ClientIn,
    ClientPatch,
    ClientStatus,
    client_gate,
    normalise_id,
    pending_from_holds,
)
from stsds.gates import GateFailure
from stsds.schemas import Classification
from stsds.users import UserDirectory, hash_token

NOW = dt.date(2026, 11, 10)
OPS, REV, CHK, CHK2 = "o" * 40, "r" * 40, "c" * 40, "d" * 40
ALPHA, OMEGA = demo.DEMO_PARTIES[0], demo.NEW_CLIENT_PARTIES[0]


def run(coro):
    return asyncio.run(coro)


def data(party=ALPHA, **kw) -> ClientIn:
    base = dict(
        legal_name=party.name, entity_type="SDN_BHD", id_type="BRN", id_number=party.id_number, tin=party.tin,
        relationship_manager="Aisha Rahman", authority_ref="EL-001", authority_valid_until=NOW + dt.timedelta(days=200))
    base.update(kw)
    return ClientIn(**base)


@pytest.fixture
def cd(tmp_path):
    return ClientDirectory(tmp_path / "c.sqlite3")


def active(cd, party=ALPHA, **kw) -> str:
    cid = cd.create(data(party, **kw), "Rita")
    cd.activate(cid, "Chen", NOW)
    return cid


# ---- validation ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"tin": "12345"}, {"tin": "C12"}, {"id_number": "!!"}, {"id_type": "NRIC", "id_number": "123"},
    {"approval_limit_rm": "0"}, {"legal_name": ""}, {"relationship_manager": ""},
])
def test_invalid_client_data_is_rejected(kw):
    with pytest.raises(ValidationError):
        data(**kw)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        ClientIn(**{**data().model_dump(), "email": "x@example.com"})  # contact details are not stored here


def test_normalise_id_ignores_case_spaces_and_hyphens():
    assert normalise_id("2020-0101 2345") == normalise_id("202001012345") == "202001012345"
    assert normalise_id("ab-12") == "AB12"


# ---- lifecycle ----------------------------------------------------------------------------------------
def test_a_new_client_is_a_draft_and_ids_are_unique(cd):
    cid = cd.create(data(), "Rita")
    got = cd.get(cid)
    assert got.status is ClientStatus.DRAFT and got.prepared_by == "Rita"
    with pytest.raises(ValueError, match="duplicate"):
        cd.create(data(id_number="2020-0101-2345"), "Rita")  # same ID written differently
    assert cd.get("zz") is None and cd.get("0" * 32) is None


def test_the_preparer_cannot_approve_their_own_record(cd):
    cid = cd.create(data(), "Rita")
    for who in ("Rita", " rita "):
        with pytest.raises(PermissionError):
            cd.activate(cid, who, NOW)
    assert cd.activate(cid, "Chen", NOW).status is ClientStatus.ACTIVE
    assert cd.get(cid).activated_by == "Chen"


def test_activation_needs_valid_unexpired_authority(cd):
    no_auth = cd.create(data(ALPHA, authority_ref=None), "Rita")
    with pytest.raises(ValueError, match="authority reference"):
        cd.activate(no_auth, "Chen", NOW)
    expired = cd.create(data(demo.DEMO_PARTIES[1], authority_valid_until=NOW - dt.timedelta(days=1)), "Rita")
    with pytest.raises(ValueError, match="expired"):
        cd.activate(expired, "Chen", NOW)
    with pytest.raises(KeyError):
        cd.activate("0" * 32, "Chen", NOW)


def test_material_edit_of_an_active_client_needs_a_new_approval(cd):
    cid = active(cd)
    res = cd.update(cid, ClientPatch(contact_name="New Contact", notes="n"), "Rita")
    assert res == {"status": "ACTIVE", "reapproval": False}  # contacts and notes are not material
    res = cd.update(cid, ClientPatch(tin="C1111111111"), "Sam")
    assert res == {"status": "DRAFT", "reapproval": True}
    got = cd.get(cid)
    assert got.prepared_by == "Sam" and got.activated_by is None
    with pytest.raises(PermissionError):
        cd.activate(cid, "Sam", NOW)  # the person who made the change cannot approve it
    assert cd.activate(cid, "Chen", NOW).status is ClientStatus.ACTIVE


def test_an_unchanged_material_value_does_not_reset_approval(cd):
    cid = active(cd)
    assert cd.update(cid, ClientPatch(tin=ALPHA.tin, legal_name=ALPHA.name), "Rita")["reapproval"] is False


def test_edits_are_validated_and_cannot_steal_another_clients_id(cd):
    a, b = active(cd), active(cd, demo.DEMO_PARTIES[1])
    with pytest.raises(ValueError, match="invalid"):
        cd.update(a, ClientPatch(tin="nope"), "Rita")
    with pytest.raises(ValueError, match="duplicate"):
        cd.update(a, ClientPatch(id_number=demo.DEMO_PARTIES[1].id_number), "Rita")
    assert cd.get(a).data.id_number == ALPHA.id_number and cd.get(b).status is ClientStatus.ACTIVE
    assert cd.update("0" * 32, ClientPatch(notes="x"), "Rita") is None
    assert cd.update("bad", ClientPatch(notes="x"), "Rita") is None


def test_suspend_and_four_eyes_reactivation(cd):
    cid = active(cd)
    assert cd.suspend(cid, "Engagement ended", "Chen") is True
    assert cd.suspend(cid, "again", "Chen") is False
    assert cd.get(cid).suspend_reason == "Engagement ended"
    with pytest.raises(PermissionError):
        cd.activate(cid, "Chen", NOW)  # the suspender cannot lift their own suspension
    assert cd.activate(cid, "Dana", NOW).status is ClientStatus.ACTIVE
    assert cd.get(cid).suspended_by is None
    assert cd.suspend("0" * 32, "x", "Chen") is False


def test_a_named_actor_is_always_required(cd):
    with pytest.raises(ValueError, match="named person"):
        cd.create(data(), "   ")


def test_every_change_is_attributed_in_the_history(cd):
    cid = cd.create(data(), "Rita")
    cd.activate(cid, "Chen", NOW)
    cd.update(cid, ClientPatch(tin="C2222222222"), "Rita")
    cd.suspend(cid, "stop", "Chen")
    h = cd.history(cid)
    assert [x["actor"] for x in h] == ["Rita", "Chen", "Rita", "Chen"]
    assert "created" in h[0]["change"] and h[1]["change"] == {"activated": True}
    assert h[2]["change"]["reapproval"] is True and cd.history("bad") == []


def test_find_filters_by_status_and_text(cd):
    active(cd)
    cd.create(data(demo.DEMO_PARTIES[1]), "Rita")
    assert len(cd.find()) == 2 and len(cd.find("active")) == 1 and len(cd.find(q="beta")) == 1
    assert cd.find(q="nothing") == []


# ---- the gate -----------------------------------------------------------------------------------------
def test_gate_blocks_when_no_party_is_a_client(cd):
    with pytest.raises(GateFailure) as e:
        client_gate(cd, demo.NEW_CLIENT_PARTIES, NOW)
    assert e.value.code == "CLIENT_NOT_REGISTERED" and "Demo Omega" in e.value.message and e.value.route == "REVIEW"


def test_gate_fails_closed_without_a_register(cd):
    with pytest.raises(GateFailure) as e:
        client_gate(None, demo.DEMO_PARTIES, NOW)
    assert e.value.code == "CLIENT_REGISTRY_UNAVAILABLE" and e.value.route == "HALT"


def test_gate_passes_for_one_active_client_and_an_unregistered_counterparty(cd):
    cid = active(cd, default_contract_owner="Aisha")
    m = client_gate(cd, demo.DEMO_PARTIES, NOW)  # Beta is not a client, Alpha is
    assert m.primary_id == cid and m.client_ids == [cid] and m.default_contract_owner == "Aisha"
    assert m.limit_exceeded_for == [] and m.notes == []


def test_gate_codes_for_draft_suspended_expired_and_mismatch(cd):
    cid = cd.create(data(), "Rita")
    with pytest.raises(GateFailure) as e:
        client_gate(cd, demo.DEMO_PARTIES, NOW)
    assert e.value.code == "CLIENT_NOT_ACTIVE"
    cd.activate(cid, "Chen", NOW)
    with pytest.raises(GateFailure) as e:
        client_gate(cd, demo.DEMO_PARTIES, NOW + dt.timedelta(days=300))  # authority ran out
    assert e.value.code == "CLIENT_AUTHORITY_EXPIRED"
    wrong = [ALPHA.model_copy(update={"tin": "C9999999999"}), demo.DEMO_PARTIES[1]]
    with pytest.raises(GateFailure) as e:
        client_gate(cd, wrong, NOW)
    assert e.value.code == "CLIENT_DETAILS_MISMATCH"
    cd.suspend(cid, "ended", "Chen")
    with pytest.raises(GateFailure) as e:
        client_gate(cd, demo.DEMO_PARTIES, NOW)
    assert e.value.code == "CLIENT_SUSPENDED" and "ended" in e.value.message


def test_one_blocked_client_blocks_even_if_the_other_party_is_fine(cd):
    active(cd)
    cd.create(data(demo.DEMO_PARTIES[1]), "Rita")  # Beta is still a draft
    with pytest.raises(GateFailure) as e:
        client_gate(cd, demo.DEMO_PARTIES, NOW)
    assert e.value.code == "CLIENT_NOT_ACTIVE"


def test_gate_matches_ids_written_differently_and_notes_name_differences(cd):
    active(cd)
    same = ALPHA.model_copy(update={"id_number": "2020-01-012345", "name": "DEMO ALPHA SDN. BHD."})
    assert client_gate(cd, [same], NOW).notes == []  # punctuation and case are not a difference
    other = same.model_copy(update={"name": "Demo Alpha Holdings Sdn Bhd"})
    assert any("differs" in n for n in client_gate(cd, [other], NOW).notes)


def test_approval_limit_is_reported_when_reached(cd):
    active(cd, approval_limit_rm=Decimal("100"))
    assert client_gate(cd, demo.DEMO_PARTIES, NOW, Decimal("99.99")).limit_exceeded_for == []
    assert client_gate(cd, demo.DEMO_PARTIES, NOW, Decimal("100")).limit_exceeded_for == [ALPHA.name]


# ---- the pipeline -------------------------------------------------------------------------------------
@pytest.fixture
def kit(tmp_path):
    return demo.Kit(tmp_path, today=NOW, require_registered_client=True)


def b64(kind, days=2):
    return base64.b64encode(demo.make_pdf(demo.sample_text(kind, NOW - dt.timedelta(days=days)))).decode()


def test_an_unregistered_client_is_held_and_cannot_be_approved_through(kit):
    res = run(kit.pipeline.run_to_parity(b64("newclient"), "t"))
    ctx = kit.pipeline.store.load(res.txn_id)
    assert res.state == "REVIEW" and res.halt["code"] == "CLIENT_NOT_REGISTERED"
    assert ctx.review.resolvable is False  # the fix is to register the client, not to edit the extraction
    with pytest.raises(GateFailure) as e:
        kit.pipeline.review_resolve(res.txn_id, "Rita", "approve", attest=True)
    assert e.value.code == "NOT_RESOLVABLE"


def test_a_registered_client_goes_straight_through(kit):
    active(kit.pipeline.clients)
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    ctx = kit.pipeline.store.load(res.txn_id)
    assert res.state == "PARITY_OK" and ctx.client_name == ALPHA.name


def test_registering_and_approving_the_client_releases_the_held_document(kit):
    res = run(kit.pipeline.run_to_parity(b64("newclient"), "t"))
    pend = pending_from_holds([kit.pipeline.store.load(res.txn_id)])
    assert {p["name"] for p in pend} == {"Demo Omega Sdn Bhd", "Demo Delta Sdn Bhd"} and pend[0]["documents"] == 1
    assert kit.pipeline.release_client_holds("Chen") == []  # nothing registered yet
    cid = kit.pipeline.clients.create(data(OMEGA, default_contract_owner="Omar"), "Rita")
    assert kit.pipeline.release_client_holds("Chen") == []  # a draft is not enough
    kit.pipeline.clients.activate(cid, "Chen", NOW)
    assert kit.pipeline.release_client_holds("Chen") == [res.txn_id]
    out = run(kit.orchestrator.advance(res.txn_id))
    ctx = kit.pipeline.store.load(res.txn_id)
    assert out.state != "REVIEW" and ctx.client_name == OMEGA.name and ctx.client_owner == "Omar"


def test_release_only_touches_client_holds(kit):
    low = run(kit.pipeline.run_to_parity(b64("blurry"), "t"))  # held for low confidence, not for a client
    active(kit.pipeline.clients)
    assert kit.pipeline.store.load(low.txn_id).state == "REVIEW"
    assert kit.pipeline.release_client_holds("Chen") == []
    assert kit.pipeline.store.load(low.txn_id).state == "REVIEW"


def test_suspending_a_client_holds_new_documents_until_a_different_person_reactivates(kit):
    cid = active(kit.pipeline.clients)
    kit.pipeline.clients.suspend(cid, "Engagement ended", "Chen")
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    assert res.halt["code"] == "CLIENT_SUSPENDED"
    kit.pipeline.clients.activate(cid, "Dana", NOW)
    assert kit.pipeline.release_client_holds("Dana") == [res.txn_id]


def test_a_client_approval_limit_forces_a_second_approver(kit):
    active(kit.pipeline.clients, approval_limit_rm=Decimal("5"))
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    assert kit.pipeline.store.load(res.txn_id).needs_second_approval is True


def test_a_tin_that_disagrees_with_the_client_record_is_fixed_by_correcting_the_extraction(kit):
    active(kit.pipeline.clients)
    # The demo classifier always extracts Alpha's real TIN, so make the client record disagree with it.
    cid = kit.pipeline.clients.find(q="alpha")[0].id
    kit.pipeline.clients.update(cid, ClientPatch(tin="C1111111111"), "Rita")
    kit.pipeline.clients.activate(cid, "Chen", NOW)
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    assert res.halt["code"] == "CLIENT_DETAILS_MISMATCH"
    ctx = kit.pipeline.store.load(res.txn_id)
    assert ctx.review.resolvable is True
    fixed = Classification.model_validate({
        **ctx.candidate.model_dump(mode="json"),
        "parties": [{**p.model_dump(mode="json"), "tin": "C1111111111"} if p.name == ALPHA.name else p.model_dump(mode="json")
                    for p in ctx.candidate.parties]})
    kit.pipeline.review_resolve(res.txn_id, "Rita", "approve", corrections=fixed, attest=True)
    out = run(kit.orchestrator.advance(res.txn_id))
    after = kit.pipeline.store.load(res.txn_id)
    # The correction cleared the client gate; because a person edited the extraction, a checker must now approve.
    assert after.state == "AWAITING_APPROVAL" and after.needs_second_approval is True
    assert out.halt["code"] == "MAKER_CHECKER"


def test_registration_is_not_enforced_unless_switched_on(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW)  # a pilot: nothing is blocked
    res = run(kit.pipeline.run_to_parity(b64("newclient"), "t"))
    assert res.state == "PARITY_OK" and kit.pipeline.store.load(res.txn_id).client_id is None
    active(kit.pipeline.clients)
    res2 = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    assert kit.pipeline.store.load(res2.txn_id).client_name == ALPHA.name  # still tagged when it matches


def test_enforcing_without_a_register_halts_instead_of_passing(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, require_registered_client=True)
    kit.pipeline.clients = None
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    assert res.state == "HALTED" and res.halt["code"] == "CLIENT_REGISTRY_UNAVAILABLE"


def test_the_register_shows_the_client_as_entity_and_its_default_owner(kit):
    active(kit.pipeline.clients, default_contract_owner="Aisha Rahman")
    res = run(kit.pipeline.run_to_parity(b64("lease"), "t"))
    from stsds.register import Register
    reg = Register(Path(kit.settings.staging_dir) / "r.sqlite3", kit.pipeline.rules.penalty)
    row = [e for e in reg.entries([kit.pipeline.store.load(res.txn_id)], NOW)][0]
    assert row.entity == ALPHA.name and row.contract_owner == "Aisha Rahman"


def test_codes_are_consistent():
    assert "CLIENT_DETAILS_MISMATCH" in CLIENT_HOLD_CODES
    from stsds.pipeline import RESOLVABLE_CODES
    assert {"CLIENT_DETAILS_MISMATCH"} == CLIENT_HOLD_CODES & RESOLVABLE_CODES  # only that one is reviewer-fixable


# ---- API ----------------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    kit = demo.Kit(tmp_path, engine_token=OPS, today=NOW, require_registered_client=True)
    users = UserDirectory([
        {"name": "Rita", "roles": ["reviewer"], "token_sha256": hash_token(REV)},
        {"name": "Chen", "roles": ["checker"], "token_sha256": hash_token(CHK)},
        {"name": "Dana", "roles": ["checker"], "token_sha256": hash_token(CHK2)},
    ])
    return TestClient(create_app(kit.pipeline, kit.settings, users)), kit


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


def body(party=ALPHA, **kw):
    return {**data(party, **kw).model_dump(mode="json")}


def test_client_routes_need_a_login(api):
    c, _ = api
    assert c.get("/v1/clients").status_code == 401
    assert c.post("/v1/clients", json=body()).status_code == 401
    assert c.post("/v1/clients/" + "0" * 32 + "/activate").status_code == 401
    assert c.get("/v1/clients", headers=hdr("z" * 40)).status_code == 401


def test_prepare_then_approve_by_a_different_checker(api):
    c, _ = api
    made = c.post("/v1/clients", json=body(), headers=hdr(REV))  # a reviewer may prepare
    assert made.status_code == 200 and made.json()["status"] == "DRAFT"
    cid = made.json()["id"]
    assert c.post(f"/v1/clients/{cid}/activate", headers=hdr(REV)).status_code == 403  # but not approve
    assert c.post(f"/v1/clients/{cid}/activate", headers=hdr(OPS)).status_code == 403  # the service token never approves
    chk = c.post("/v1/clients", json=body(demo.DEMO_PARTIES[1]), headers=hdr(CHK)).json()["id"]
    assert c.post(f"/v1/clients/{chk}/activate", headers=hdr(CHK)).status_code == 403  # four-eyes
    ok = c.post(f"/v1/clients/{cid}/activate", headers=hdr(CHK))
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"
    got = c.get(f"/v1/clients/{cid}", headers=hdr(REV)).json()
    assert got["status"] == "ACTIVE" and got["activated_by"] == "Chen" and got["prepared_by"] == "Rita"
    assert c.post(f"/v1/clients/{chk}/activate", headers=hdr(CHK2)).status_code == 200


def test_validation_duplicates_and_bad_ids(api):
    c, _ = api
    assert c.post("/v1/clients", json={**body(), "tin": "x"}, headers=hdr(REV)).status_code == 422
    assert c.post("/v1/clients", json={**body(), "email": "a@b.c"}, headers=hdr(REV)).status_code == 422
    assert c.post("/v1/clients", json=body(), headers=hdr(REV)).status_code == 200
    assert c.post("/v1/clients", json=body(), headers=hdr(REV)).status_code == 409
    assert c.get("/v1/clients/not-an-id", headers=hdr(REV)).status_code == 400
    assert c.get("/v1/clients/" + "0" * 32, headers=hdr(REV)).status_code == 404
    assert c.patch("/v1/clients/" + "0" * 32, json={"notes": "x"}, headers=hdr(REV)).status_code == 404
    assert c.post("/v1/clients/" + "0" * 32 + "/activate", headers=hdr(CHK)).status_code == 404
    assert c.get("/v1/clients?status=bogus", headers=hdr(REV)).status_code == 422
    assert c.get("/v1/clients/not-an-id/history", headers=hdr(REV)).status_code == 400


def test_edit_reapproval_suspend_and_history(api):
    c, _ = api
    cid = c.post("/v1/clients", json=body(), headers=hdr(REV)).json()["id"]
    c.post(f"/v1/clients/{cid}/activate", headers=hdr(CHK))
    r = c.patch(f"/v1/clients/{cid}", json={"approval_limit_rm": "500.00"}, headers=hdr(REV))
    assert r.json() == {"status": "DRAFT", "reapproval": True}
    assert c.patch(f"/v1/clients/{cid}", json={"tin": "bad"}, headers=hdr(REV)).status_code == 422
    assert c.post(f"/v1/clients/{cid}/activate", headers=hdr(CHK2)).status_code == 200
    assert c.post(f"/v1/clients/{cid}/suspend", json={"reason": "ab"}, headers=hdr(CHK)).status_code == 422
    assert c.post(f"/v1/clients/{cid}/suspend", json={"reason": "Engagement ended"}, headers=hdr(REV)).status_code == 403
    assert c.post(f"/v1/clients/{cid}/suspend", json={"reason": "Engagement ended"}, headers=hdr(CHK)).status_code == 200
    assert c.post(f"/v1/clients/{cid}/suspend", json={"reason": "Engagement ended"}, headers=hdr(CHK)).status_code == 404
    assert c.post(f"/v1/clients/{cid}/activate", headers=hdr(CHK)).status_code == 403  # suspender cannot lift it
    hist = c.get(f"/v1/clients/{cid}/history", headers=hdr(REV)).json()["items"]
    assert [h["actor"] for h in hist][:3] == ["Rita", "Chen", "Rita"] and len(hist) >= 5
    listed = c.get("/v1/clients?status=suspended&q=alpha", headers=hdr(REV)).json()
    assert listed["total"] == 1 and listed["items"][0]["status"] == "SUSPENDED"


def test_onboarding_a_client_releases_the_held_batch_by_itself(api):
    c, kit = api
    docs = [{"name": "new.pdf", "pdf_base64": b64("newclient")}, {"name": "ok.pdf", "pdf_base64": b64("lease")}]
    active(kit.pipeline.clients)  # Alpha is already onboarded, Omega is not
    assert c.post("/v1/batches", json={"source": "t", "documents": docs, "auto_run": True}, headers=hdr(OPS)).status_code == 200

    def states():
        return {kit.pipeline.store.load(i).name: kit.pipeline.store.load(i) for i in kit.pipeline.store.list_ids()}

    for _ in range(60):
        s = states()
        if len(s) == 2 and s["new.pdf"].state == "REVIEW":
            break
        time.sleep(0.25)
    assert s["new.pdf"].review.code == "CLIENT_NOT_REGISTERED" and s["ok.pdf"].state != "REVIEW"
    pending = c.get("/v1/clients/pending", headers=hdr(REV)).json()["items"]
    assert pending[0]["documents"] == 1 and "Demo Omega Sdn Bhd" in {p["name"] for p in pending}
    queue = c.get("/v1/review/queue", headers=hdr(REV)).json()["items"]
    assert [i["code"] for i in queue] == ["CLIENT_NOT_REGISTERED"] and queue[0]["resolvable"] is False

    cid = c.post("/v1/clients", json=body(OMEGA), headers=hdr(REV)).json()["id"]
    assert states()["new.pdf"].state == "REVIEW"  # a draft releases nothing
    done = c.post(f"/v1/clients/{cid}/activate", headers=hdr(CHK))
    assert done.status_code == 200 and len(done.json()["released"]) == 1
    for _ in range(60):
        after = states()["new.pdf"]
        if after.state != "REVIEW":
            break
        time.sleep(0.25)
    assert after.state != "REVIEW" and after.client_name == OMEGA.name
    assert c.get("/v1/clients/pending", headers=hdr(REV)).json()["items"] == []


def test_console_has_the_clients_tab_and_stays_free_of_inline_script(api):
    c, _ = api
    html = c.get("/console").text
    assert 'data-tab="clients"' in html and 'id="tab-clients"' in html and "<script>" not in html
    js = c.get("/console/app.js").text
    assert "innerHTML" not in js and "/v1/clients" in js and "eval(" not in js
