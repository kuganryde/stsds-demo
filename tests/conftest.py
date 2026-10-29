from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from stsds.audit import MemoryAuditStore
from stsds.gates import GateFailure
from stsds.notify import NullNotifier
from stsds.pipeline import Pipeline, StagingStore
from stsds.rpa import DraftResult, SubmitResult
from stsds.rules import Signoff, load_rules
from stsds.schemas import Classification, Party
from stsds.settings import Settings
from stsds.vault import Vault

RULES_PATH = Path(__file__).resolve().parents[1] / "rules" / "stamp_rules.json"
TODAY = dt.date(2026, 11, 10)


@pytest.fixture(scope="session")
def rules_pair():
    return load_rules(RULES_PATH)


@pytest.fixture(scope="session")
def rules(rules_pair):
    return rules_pair[0]


@pytest.fixture(scope="session")
def rules_sha(rules_pair):
    return rules_pair[1]


def make_pdf(text: str, pages: int = 1) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    for _ in range(pages):
        y = 800
        for line in text.splitlines():
            pdf.drawString(40, y, line[:110])
            y -= 14
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


PARTIES = [
    Party(role="landlord", name="Alpha Sdn Bhd", tin="C1234567890", id_type="BRN", id_number="202001012345"),
    Party(role="tenant", name="Beta Sdn Bhd", tin="C9876543210", id_type="BRN", id_number="201901054321"),
]


def make_cls(item: str = "ITEM_49_LEASE", **overrides: Any) -> Classification:
    module = {
        "ITEM_4_EMPLOYMENT": "PENYETEMAN_AM",
        "ITEM_49_LEASE": "SEWA_PAJAKAN",
        "ITEM_22_SECURITY": "SEKURITI",
        "ITEM_32A_MOT": "PINDAH_MILIK_HARTANAH",
    }[item]
    fin = {k: v for k, v in overrides.pop("financials", {}).items()}
    data: dict[str, Any] = {
        "bnds_module": module,
        "first_schedule_item": item,
        "requires_jpph_valuation": False,
        "is_foreign_buyer_or_entity": False,
        "is_residential_property": None,
        "financials": fin,
        "parties": [p.model_dump() for p in PARTIES],
        "execution_date": dt.date(2026, 11, 2),
        "confidence": 0.99,
        "evidence": [],
    }
    data.update(overrides)
    return Classification.model_validate(data)


class FakeClassifier:
    def __init__(self, result: Classification) -> None:
        self.result = result

    def classify(self, text: str) -> Classification:
        return self.result


class FakeExtractor:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract(self, pdf: bytes) -> str:
        return self.text


class FakeVerifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def verify(self, party: Party) -> bool:
        return self.ok


class FakeRpa:
    def __init__(self, lhdn_total: Decimal | None, sijil: bytes | None = None) -> None:
        self.lhdn_total = lhdn_total
        self.sijil = sijil if sijil is not None else make_pdf("SIJIL SETEM")
        self.drafts = 0
        self.submits = 0
        self.fail_download = False

    async def draft(self, module, fields, parties):
        self.drafts += 1
        return DraftResult("D-001", self.lhdn_total, {})

    async def submit(self, draft_id):
        self.submits += 1
        return SubmitResult("BILL-123", "VA-999")

    async def download_sijil(self, draft_id):
        if self.fail_download:
            raise GateFailure("SIJIL_STREAM", "network dropped", "HALT")
        return self.sijil


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.lock_mode = "COMPLIANCE"

    def put_object(self, **kw):
        self.objects[kw["Key"]] = kw
        return {"VersionId": "v1"}

    def head_object(self, **kw):
        return {"ObjectLockMode": self.lock_mode}


def settings(tmp_path: Path, **kw: Any) -> Settings:
    base = dict(
        mode="live", engine_token="t" * 40, webhook_secret="w" * 40, rules_path=str(RULES_PATH),
        staging_dir=str(tmp_path / "stg"), ollama_url="http://x", ollama_model="m", qdrant_url=None,
        s3_bucket="b", s3_kms_key_id=None, database_url=None, telegram_bot_token=None,
        telegram_chat_id=None, tin_verify_url=None, tin_verify_token=None,
        rpa_storage_state="s", rpa_selectors_path="p",
        maker_checker_threshold_rm=Decimal("10000"), phase2_enabled=False, max_body_bytes=28 * 1024 * 1024,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture()
def build(tmp_path, rules_pair):
    def _build(cls: Classification, text: str, rpa: FakeRpa, *, verifier_ok=True, signed=True, **skw):
        rules, sha = rules_pair
        if signed:
            rules = rules.model_copy(
                update={"signoff": Signoff(by="Head of Tax", role="Head of Tax", date=dt.date(2026, 10, 30))}
            )
        s3 = FakeS3()
        notifier = NullNotifier()
        audit = MemoryAuditStore()
        s = settings(tmp_path, **skw)
        pipe = Pipeline(
            settings=s, rules=rules, rules_sha256=sha, extractor=FakeExtractor(text),
            classifier=FakeClassifier(cls), verifier=FakeVerifier(verifier_ok), rpa=rpa,
            vault=Vault(s3, "b", None, tmp_path / "cache"), audit=audit,
            store=StagingStore(tmp_path / "stg"), notifier=notifier, today=lambda: TODAY,
        )
        return pipe, s3, notifier, audit

    return _build
