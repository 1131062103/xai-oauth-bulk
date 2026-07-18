"""Account file parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Account:
    email: str
    password: str


def parse_accounts_file(path: str | Path) -> list[Account]:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"accounts file not found: {p}")
    accounts: list[Account] = []
    seen: set[str] = set()
    with p.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            email, password = _split_line(line)
            if not email or password is None:
                raise ValueError(f"invalid account line {lineno}: {line!r}")
            key = email.lower()
            if key in seen:
                continue
            seen.add(key)
            accounts.append(Account(email=email, password=password))
    return accounts


def _split_line(line: str) -> tuple[str, str | None]:
    if ":" in line:
        email, password = line.split(":", 1)
        return email.strip(), password
    if "," in line:
        email, password = line.split(",", 1)
        return email.strip(), password
    return line.strip(), None


def filter_accounts(
    accounts: list[Account],
    *,
    email: str = "",
    offset: int = 0,
    limit: int = 0,
) -> list[Account]:
    out = list(accounts)
    if email:
        target = email.strip().lower()
        out = [a for a in out if a.email.lower() == target]
    if offset > 0:
        out = out[offset:]
    if limit and limit > 0:
        out = out[:limit]
    return out
