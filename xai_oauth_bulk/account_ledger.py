"""Protected local ledger for successfully registered account credentials.

Two complementary outputs:

- ``accounts.jsonl`` — structured records (email, password, name, status, oauth path)
- ``accounts-registered.txt`` — ``email:password`` lines for reuse with file mode

Credentials are written as soon as browser registration succeeds, not only after
OAuth completes, so a later authorization failure does not lose the account.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .registration import RegistrationProfile


def _atomic_write_text(dest: Path, data: str, *, mode: int = 0o600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}-", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, dest)
        try:
            os.chmod(dest, mode)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def append_account_ledger(
    path: str | Path,
    *,
    email: str,
    profile: RegistrationProfile,
    mode: str,
    oauth_path: str = "",
    status: str = "registered",
) -> Path:
    """Append one account record with restrictive file permissions.

    ``status`` is typically ``registered`` (browser sign-up done) or
    ``oauth_ok`` (device-code / CPA authorization also finished).
    """
    dest = Path(path).expanduser().resolve()
    record = {
        "email": email,
        "password": profile.password,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "registered_at": datetime.now(tz=timezone.utc).isoformat(),
        "mode": mode,
        "status": status,
        "oauth_path": oauth_path or "",
    }
    existing = dest.read_text(encoding="utf-8") if dest.exists() else ""
    data = existing + json.dumps(record, ensure_ascii=False) + "\n"
    _atomic_write_text(dest, data)
    return dest


def append_account_credentials_txt(
    path: str | Path,
    *,
    email: str,
    password: str,
) -> Path:
    """Append ``email:password`` for reuse with ``accounts.txt`` file mode."""
    dest = Path(path).expanduser().resolve()
    line = f"{email}:{password}\n"
    existing = dest.read_text(encoding="utf-8") if dest.exists() else ""
    # Avoid exact duplicate consecutive lines if the same run writes twice.
    if existing.endswith(line) or f"\n{line}" in f"\n{existing}":
        return dest
    _atomic_write_text(dest, existing + line)
    return dest


def default_credentials_txt_path(ledger_path: str | Path) -> Path:
    """Sibling ``accounts-registered.txt`` next to the JSONL ledger."""
    dest = Path(ledger_path).expanduser()
    return dest.with_name("accounts-registered.txt")


def save_registered_account(
    *,
    ledger_path: str | Path,
    email: str,
    profile: RegistrationProfile,
    mode: str,
    oauth_path: str = "",
    status: str = "registered",
    credentials_txt_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Persist credentials to both JSONL ledger and email:password export."""
    jsonl = append_account_ledger(
        ledger_path,
        email=email,
        profile=profile,
        mode=mode,
        oauth_path=oauth_path,
        status=status,
    )
    txt_path = Path(credentials_txt_path) if credentials_txt_path else default_credentials_txt_path(ledger_path)
    txt = append_account_credentials_txt(txt_path, email=email, password=profile.password)
    return jsonl, txt
