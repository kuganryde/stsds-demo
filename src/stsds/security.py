"""Security primitives: webhook authentication, service tokens, PII masking, safe identifiers."""

from __future__ import annotations

import hashlib
import hmac
import re
import time


class AuthError(Exception):
    pass


def verify_webhook(
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
    now: float | None = None,
    tolerance_s: int = 300,
) -> None:
    """HMAC-SHA256 over '<timestamp>.<raw body>' with a replay window.

    Constant-time comparison; the timestamp is part of the signed payload so a captured request
    cannot be replayed after the tolerance window.
    """
    if not secret:
        raise AuthError("webhook secret is not configured")
    if not signature or not timestamp:
        raise AuthError("missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise AuthError("bad timestamp") from exc
    if abs((now if now is not None else time.time()) - ts) > tolerance_s:
        raise AuthError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
        raise AuthError("bad signature")


def sign_webhook(body: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify_bearer(header: str | None, token: str) -> None:
    """Service-to-service bearer check (n8n -> engine). Constant-time; empty token never matches."""
    if not token or len(token) < 32:
        raise AuthError("service token not configured or too short")
    if not header or not header.startswith("Bearer "):
        raise AuthError("missing bearer token")
    if not hmac.compare_digest(header.removeprefix("Bearer ").encode(), token.encode()):
        raise AuthError("bad bearer token")


_ID_RE = re.compile(r"[^A-Za-z0-9]")


def mask(value: str | None) -> str:
    """Keep the last four characters only. Used for every TIN/NRIC/BRN that reaches a log."""
    if not value:
        return ""
    clean = _ID_RE.sub("", value)
    return "*" * max(len(clean) - 4, 0) + clean[-4:]


_TXN_RE = re.compile(r"^[a-f0-9]{32}$")


def valid_txn_id(value: str) -> bool:
    """Transaction ids are 32 hex chars. Anything else never reaches the filesystem."""
    return bool(_TXN_RE.match(value))
