"""People and roles for the human-in-the-loop steps.

Roles:  operator (submit batches, read reports)  ·  reviewer (resolve low-confidence documents)
        checker (approve high-value or human-edited filings).

Tokens are never stored: the directory file holds SHA-256 digests. Authentication compares in
constant time over every entry so response time does not reveal which user exists. The same person
may hold several roles, but the pipeline enforces four-eyes: the checker of a filing can never be
the reviewer who edited it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .security import AuthError

ROLES = frozenset({"operator", "reviewer", "checker"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,59}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    name: str
    roles: frozenset[str]

    def has(self, role: str) -> bool:
        return role in self.roles


class UserDirectory:
    def __init__(self, users: list[dict[str, object]]) -> None:
        self._entries: list[tuple[str, frozenset[str], str]] = []
        for u in users:
            name, roles, digest = str(u["name"]), frozenset(u["roles"]), str(u["token_sha256"])  # type: ignore[call-overload]
            if not _NAME_RE.match(name) or not roles or not roles <= ROLES or len(digest) != 64:
                raise ValueError(f"invalid user entry: {name!r}")
            self._entries.append((name, roles, digest))
        if len({n.lower() for n, _, _ in self._entries}) != len(self._entries):
            raise ValueError("duplicate user names")

    @classmethod
    def load(cls, path: str | Path) -> UserDirectory:
        return cls(json.loads(Path(path).read_text(encoding="utf-8"))["users"])

    def authenticate(self, header: str | None) -> Identity:
        if not header or not header.startswith("Bearer "):
            raise AuthError("missing bearer token")
        token = header.removeprefix("Bearer ")
        if len(token) < 32:
            raise AuthError("token too short")
        digest = hash_token(token)
        match: Identity | None = None
        for name, roles, stored in self._entries:  # no early exit: constant work per request
            if hmac.compare_digest(digest, stored):
                match = Identity(name, roles)
        if match is None:
            raise AuthError("unknown token")
        return match


def new_user_entry(name: str, roles: list[str]) -> tuple[str, dict[str, object]]:
    """Mint a token (shown once, never stored) and the directory entry that holds only its hash."""
    import secrets

    token = secrets.token_urlsafe(32)
    entry: dict[str, object] = {"name": name, "roles": sorted(roles), "token_sha256": hash_token(token)}
    UserDirectory([entry])  # validates name and roles before anything is printed
    return token, entry


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m stsds.users", description="Create a reviewer/checker/operator entry")
    ap.add_argument("name")
    ap.add_argument("roles", nargs="+", choices=sorted(ROLES))
    args = ap.parse_args(argv)
    token, entry = new_user_entry(args.name, args.roles)
    print("Give this token to the person ONCE, over a secure channel. It cannot be recovered:\n")
    print(f"  {token}\n")
    print("Add this entry to the users file (STSDS_USERS_FILE):\n")
    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
