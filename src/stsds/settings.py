"""Configuration from environment. Secrets are never defaulted; live mode needs explicit opt-in."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


def _secret(env: dict[str, str], name: str) -> str:
    """Read a secret from NAME, or from the file named by NAME_FILE (Docker/K8s secrets)."""
    path = env.get(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return env.get(name, "")


@dataclass(frozen=True)
class Settings:
    mode: str  # "shadow" (draft only) or "live" (may submit)
    engine_token: str
    webhook_secret: str
    rules_path: str
    staging_dir: str
    ollama_url: str
    ollama_model: str
    qdrant_url: str | None
    s3_bucket: str
    s3_kms_key_id: str | None
    database_url: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    tin_verify_url: str | None
    tin_verify_token: str | None
    rpa_storage_state: str
    rpa_selectors_path: str
    maker_checker_threshold_rm: Decimal
    phase2_enabled: bool
    max_body_bytes: int
    auto_threshold: float = 0.95
    batch_max_docs: int = 50
    batch_concurrency: int = 4
    portal_concurrency: int = 1
    batch_payment_cap_rm: Decimal = Decimal("250000")
    users_path: str | None = None
    ledger_path: str = "/var/lib/stsds/ledger/ledger.jsonl"
    payment_url: str | None = None
    payment_token: str | None = None
    min_signatures: int = 0  # 0 = do not require signatures at intake
    qr_verify_hosts: str = ""  # comma list of hosts the certificate QR may point to
    signing_url: str | None = None
    signing_token: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ) if env is None else env
        mode = e.get("STSDS_MODE", "shadow")
        if mode not in {"shadow", "live"}:
            raise ValueError("STSDS_MODE must be 'shadow' or 'live'")
        return cls(
            mode=mode,
            engine_token=_secret(e, "STSDS_ENGINE_TOKEN"),
            webhook_secret=_secret(e, "STSDS_WEBHOOK_SECRET"),
            rules_path=e.get("STSDS_RULES_PATH", "rules/stamp_rules.json"),
            staging_dir=e.get("STSDS_STAGING_DIR", "/var/lib/stsds/staging"),
            ollama_url=e.get("OLLAMA_URL", "http://ollama:11434"),
            ollama_model=e.get("OLLAMA_MODEL", "qwen2.5:14b"),
            qdrant_url=e.get("QDRANT_URL"),
            s3_bucket=e.get("STSDS_VAULT_BUCKET", ""),
            s3_kms_key_id=e.get("STSDS_VAULT_KMS_KEY_ID"),
            database_url=e.get("DATABASE_URL"),
            telegram_bot_token=_secret(e, "TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=e.get("TELEGRAM_CHAT_ID"),
            tin_verify_url=e.get("TIN_VERIFY_URL"),
            tin_verify_token=_secret(e, "TIN_VERIFY_TOKEN") or None,
            rpa_storage_state=e.get("RPA_STORAGE_STATE", "/run/secrets/mytax_storage_state.json"),
            rpa_selectors_path=e.get("RPA_SELECTORS_PATH", "rpa/selectors.json"),
            maker_checker_threshold_rm=Decimal(e.get("STSDS_MAKER_CHECKER_RM", "10000")),
            phase2_enabled=e.get("STSDS_ENABLE_PHASE2", "false").lower() == "true",
            max_body_bytes=int(e.get("STSDS_MAX_BODY_BYTES", str(28 * 1024 * 1024))),
            auto_threshold=float(e.get("STSDS_AUTO_THRESHOLD", "0.95")),
            batch_max_docs=int(e.get("STSDS_BATCH_MAX_DOCS", "50")),
            batch_concurrency=int(e.get("STSDS_BATCH_CONCURRENCY", "4")),
            portal_concurrency=int(e.get("STSDS_PORTAL_CONCURRENCY", "1")),
            batch_payment_cap_rm=Decimal(e.get("STSDS_BATCH_PAYMENT_CAP_RM", "250000")),
            users_path=e.get("STSDS_USERS_FILE"),
            ledger_path=e.get("STSDS_LEDGER_PATH", "/var/lib/stsds/ledger/ledger.jsonl"),
            payment_url=e.get("PAYMENT_URL"),
            payment_token=_secret(e, "PAYMENT_TOKEN") or None,
            min_signatures=int(e.get("STSDS_MIN_SIGNATURES", "0")),
            qr_verify_hosts=e.get("STSDS_QR_VERIFY_HOSTS", ""),
            signing_url=e.get("SIGNING_URL"),
            signing_token=_secret(e, "SIGNING_TOKEN") or None,
        )
