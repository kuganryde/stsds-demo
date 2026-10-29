"""Append-only, hash-chained audit trail (Section 3A mock-audit: any transaction retrievable fast).

Each record commits to the previous record's hash, so tampering or deletion is detectable by
``verify_chain``. The Postgres schema additionally blocks UPDATE and DELETE with a trigger.
No raw PII is written: identifiers are masked before they reach this layer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditRecord:
    txn_id: str
    seq: int
    ts: str
    event: str
    data: dict[str, Any]
    prev_hash: str
    hash: str


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(txn_id: str, seq: int, ts: str, event: str, data: dict[str, Any], prev: str) -> str:
    payload = _canonical({"t": txn_id, "s": seq, "ts": ts, "e": event, "d": data, "p": prev})
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditStore(Protocol):
    def append(self, txn_id: str, event: str, data: dict[str, Any]) -> AuditRecord: ...
    def records(self, txn_id: str) -> list[AuditRecord]: ...


def verify_chain(records: list[AuditRecord]) -> bool:
    prev = GENESIS
    for i, r in enumerate(records):
        if r.seq != i or r.prev_hash != prev:
            return False
        if compute_hash(r.txn_id, r.seq, r.ts, r.event, r.data, r.prev_hash) != r.hash:
            return False
        prev = r.hash
    return True


class MemoryAuditStore:
    def __init__(self) -> None:
        self._data: dict[str, list[AuditRecord]] = {}
        self._lock = threading.Lock()

    def append(self, txn_id: str, event: str, data: dict[str, Any]) -> AuditRecord:
        with self._lock:
            chain = self._data.setdefault(txn_id, [])
            prev = chain[-1].hash if chain else GENESIS
            ts = dt.datetime.now(dt.UTC).isoformat()
            rec = AuditRecord(
                txn_id, len(chain), ts, event, data, prev,
                compute_hash(txn_id, len(chain), ts, event, data, prev),
            )
            chain.append(rec)
            return rec

    def records(self, txn_id: str) -> list[AuditRecord]:
        return list(self._data.get(txn_id, []))


class PostgresAuditStore:
    """Production store. Table and immutability trigger are defined in db/schema.sql."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def append(self, txn_id: str, event: str, data: dict[str, Any]) -> AuditRecord:
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (txn_id,))
            row = conn.execute(
                "SELECT seq, hash FROM audit_log WHERE txn_id=%s ORDER BY seq DESC LIMIT 1", (txn_id,)
            ).fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (0, GENESIS)
            ts = dt.datetime.now(dt.UTC).isoformat()
            digest = compute_hash(txn_id, seq, ts, event, data, prev)
            conn.execute(
                "INSERT INTO audit_log (txn_id, seq, ts, event, data, prev_hash, hash) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)",
                (txn_id, seq, ts, event, _canonical(data), prev, digest),
            )
            return AuditRecord(txn_id, seq, ts, event, data, prev, digest)

    def records(self, txn_id: str) -> list[AuditRecord]:
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT txn_id, seq, ts, event, data, prev_hash, hash FROM audit_log "
                "WHERE txn_id=%s ORDER BY seq",
                (txn_id,),
            ).fetchall()
        return [AuditRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]
