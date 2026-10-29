"""Transaction ledger: one row for every outcome of every document, in order, tamper-evident.

The per-transaction audit chain (audit.py) is the evidence trail for a single filing. The ledger is
the book of record across ALL filings: it answers "what happened to every document we received,
who touched it, and what did it cost" in one place, and is what finance and compliance export.

Rows are appended with fsync and chained by SHA-256 across the whole file, so a deleted or edited
row is detected by ``verify``. Writes raise on failure: the pipeline treats an unwritable ledger as
a reason to stop before any irreversible step, because an unlogged transaction is not acceptable.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import threading
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
COLUMNS = (
    "seq", "ts", "batch_id", "txn_id", "name", "stage", "outcome", "code", "state", "actor",
    "amount_rm", "detail", "hash",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _digest(prev: str, body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + payload).encode()).hexdigest()


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection in exported cells (names come from uploads)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._seq, self._prev = 0, GENESIS
        if self.path.exists():
            last = None
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = json.loads(line)
            if last:
                self._seq, self._prev = last["seq"] + 1, last["hash"]

    def append(
        self,
        *,
        txn_id: str,
        stage: str,
        outcome: str,
        state: str,
        batch_id: str | None = None,
        name: str = "",
        code: str = "",
        actor: str = "system",
        amount_rm: str | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            body: dict[str, Any] = {
                "seq": self._seq,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "batch_id": batch_id or "",
                "txn_id": txn_id,
                "name": name[:80],
                "stage": stage,
                "outcome": outcome,
                "code": code,
                "state": state,
                "actor": actor[:80],
                "amount_rm": amount_rm or "",
                "detail": detail[:300],
            }
            row = {**body, "hash": _digest(self._prev, body)}
            new_file = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if new_file:
                os.chmod(self.path, 0o600)
            self._seq, self._prev = self._seq + 1, row["hash"]
            return row

    def rows(self, *, batch_id: str | None = None, txn_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock, self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if batch_id and row["batch_id"] != batch_id:
                    continue
                if txn_id and row["txn_id"] != txn_id:
                    continue
                out.append(row)
        return out

    def verify(self) -> bool:
        prev = GENESIS
        for i, row in enumerate(self.rows()):
            body = {k: v for k, v in row.items() if k != "hash"}
            if row["seq"] != i or _digest(prev, body) != row["hash"]:
                return False
            prev = row["hash"]
        return True

    @staticmethod
    def to_csv(rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(COLUMNS)
        for r in rows:
            writer.writerow([csv_safe(r.get(c)) for c in COLUMNS])
        return buf.getvalue()
