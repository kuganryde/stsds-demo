from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from stsds import demo
from stsds.engine import calculate
from stsds.schemas import Classification


def test_scenarios_produce_expected_outcomes():
    rows = {r[0][0]: r for r in asyncio.run(demo.run_scenarios())}
    assert rows["1"][1] == "VAULTED" and "duty RM72.00 = LHDN RM72.00" in rows["1"][2]
    assert rows["2"][1] == "VAULTED" and "RM25000.00" in rows["2"][2] and "checker approved" in rows["2"][2]
    assert rows["3"][1] == "VAULTED" and "RM0.00" in rows["3"][2]
    assert rows["4"][1] == "HALTED" and "PARITY_MISMATCH" in rows["4"][2]
    assert rows["5"][1] == "HALTED" and "UNGROUNDED_FIGURE" in rows["5"][2]
    assert rows["6"][1] == "HALTED" and "PHASE_NOT_LIVE" in rows["6"][2]


@pytest.mark.parametrize("kind", ["lease", "loan", "employment", "transfer"])
def test_heuristic_classifier_reads_samples(kind):
    import datetime as dt

    c = demo.HeuristicClassifier().classify(demo.sample_text(kind, dt.date(2026, 10, 27)))
    assert c.execution_date == dt.date(2026, 10, 27) and c.confidence == 0.99


def test_portal_and_engine_agree_independently(rules, rules_sha):
    """The simulated portal and the engine are separate implementations: they must agree."""
    cases = [
        ("SEWA_PAJAKAN", "ITEM_49_LEASE", {"monthly_rent": "1500.00", "lease_months": "37"},
         {"monthly_rental_rm": "1500.00", "lease_duration_months": 37}),
        ("SEKURITI", "ITEM_22_SECURITY", {"principal_or_consideration": "2000.01"},
         {"consideration_or_loan_rm": "2000.01", "security_kind": "SECURED_LOAN"}),
        ("PINDAH_MILIK_HARTANAH", "ITEM_32A_MOT", {"principal_or_consideration": "1000000.01"},
         {"consideration_or_loan_rm": "1000000.01"}),
    ]
    for module, item, pf, ef in cases:
        c = Classification.model_validate({
            "bnds_module": module, "first_schedule_item": item, "requires_jpph_valuation": False,
            "is_foreign_buyer_or_entity": False, "financials": ef,
            "parties": [demo.DEMO_PARTIES[0].model_dump()], "execution_date": "2026-11-02", "confidence": 0.99,
        })
        assert calculate(c, rules, rules_sha).total_duty_rm == demo.portal_total(module, pf).quantize(Decimal("0.01"))


def test_local_folder_s3_records_mode(tmp_path):
    s3 = demo.LocalFolderS3(tmp_path)
    s3.put_object(Key="a/b.pdf", Body=b"x", ObjectLockMode="COMPLIANCE")
    assert s3.head_object(Key="a/b.pdf")["ObjectLockMode"] == "COMPLIANCE"


def test_cli_scenarios_and_samples(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "DEMO MODE" in out and "PARITY_MISMATCH" in out
    assert demo.main(["sample"]) == 0
    assert len(list((tmp_path / "demo-samples").glob("*.pdf"))) == 5
