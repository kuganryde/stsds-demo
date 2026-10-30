"""Production wiring. ``uvicorn stsds.main:app``. Misconfiguration fails at startup, not at 2am."""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from fastapi import FastAPI

from .api import create_app
from .audit import MemoryAuditStore, PostgresAuditStore
from .classifier import OllamaClassifier
from .clients import ClientDirectory
from .gates import HttpTinVerifier
from .ledger import Ledger
from .notify import NullNotifier, TelegramNotifier
from .ocr import PdfTextExtractor
from .payment import HttpPaymentProvider
from .pipeline import Pipeline, StagingStore
from .qr import parse_hosts
from .rpa import PlaywrightMyTax, SelectorMap
from .rules import load_rules
from .settings import Settings
from .signing import HttpSignatureProvider
from .users import UserDirectory
from .vault import Vault

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}')


def build() -> tuple[Pipeline, Settings]:
    s = Settings.from_env()
    if s.mode == "live" and not (s.engine_token and s.webhook_secret and s.database_url and s.s3_bucket):
        raise RuntimeError("live mode requires engine token, webhook secret, database and vault bucket")
    if s.mode == "live" and not (s.users_path and s.payment_url and s.payment_token):
        raise RuntimeError("live mode requires a user directory (STSDS_USERS_FILE) and a payment provider")
    if s.mode == "live" and not parse_hosts(s.qr_verify_hosts):
        raise RuntimeError("live mode requires STSDS_QR_VERIFY_HOSTS (the host(s) the certificate QR may point to)")
    if s.mode == "live" and s.min_signatures < 1:
        raise RuntimeError("live mode requires STSDS_MIN_SIGNATURES of at least 1")
    rules, sha = load_rules(s.rules_path)
    staging = Path(s.staging_dir)
    pipeline = Pipeline(
        settings=s,
        rules=rules,
        rules_sha256=sha,
        extractor=PdfTextExtractor(),
        classifier=OllamaClassifier(s.ollama_url, s.ollama_model, s.qdrant_url),
        verifier=HttpTinVerifier(s.tin_verify_url, s.tin_verify_token),
        rpa=PlaywrightMyTax(SelectorMap.load(s.rpa_selectors_path), s.rpa_storage_state),
        vault=Vault(boto3.client("s3"), s.s3_bucket, s.s3_kms_key_id, staging / "sijil-cache"),
        audit=PostgresAuditStore(s.database_url) if s.database_url else MemoryAuditStore(),
        store=StagingStore(staging),
        payments=HttpPaymentProvider(s.payment_url, s.payment_token),
        ledger=Ledger(s.ledger_path),
        clients=ClientDirectory(staging / "clients.sqlite3"),
        notifier=(
            TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
            if s.telegram_bot_token and s.telegram_chat_id
            else NullNotifier()
        ),
    )
    return pipeline, s


def _app() -> FastAPI:
    pipeline, settings = build()
    users = UserDirectory.load(settings.users_path) if settings.users_path else None
    signer = HttpSignatureProvider(settings.signing_url, settings.signing_token) if settings.signing_url else None
    return create_app(pipeline, settings, users, signer)


app = _app()
