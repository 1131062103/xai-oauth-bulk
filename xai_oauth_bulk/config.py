"""Config loading for xai-oauth-bulk."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    mode: str = "api"
    cpa_base_url: str = "http://127.0.0.1:8317"
    cpa_management_key: str = ""
    accounts_file: str = "accounts.txt"
    out_dir: str = "output/auths"
    skip_existing: bool = True
    headless: bool = False
    proxy: str = ""
    chrome_path: str = ""
    browser_timeout_sec: float = 240.0
    sleep_between_sec: float = 3.0
    workers: int = 1
    base_url: str = "https://cli-chat-proxy.grok.com/v1"
    fail_log: str = "output/failed.jsonl"

    # Authorized account registration (disabled unless explicitly enabled).
    account_source: str = "file"
    registration_enabled: bool = False
    register_count: int = 0
    registration_timeout_sec: float = 300.0
    mailbox_provider: str = ""
    mailbox_poll_timeout_sec: float = 180.0
    mailbox_poll_interval_sec: float = 3.0
    mailbox_max_retries: int = 2
    account_ledger_path: str = "output/accounts.jsonl"

    # Cloudflare Temp Mail provider.
    cloudflare_api_base: str = ""
    cloudflare_api_key: str = ""
    cloudflare_auth_mode: str = "none"
    cloudflare_path_domains: str = "/api/domains"
    cloudflare_path_accounts: str = "/api/new_address"
    cloudflare_path_token: str = "/api/token"
    cloudflare_path_messages: str = "/api/mails"

    # DuckMail provider.
    duckmail_api_base: str = "https://api.duckmail.su"
    duckmail_api_key: str = ""

    # YYDS provider.
    yyds_api_base: str = ""
    yyds_api_key: str = ""
    yyds_jwt: str = ""
    yyds_preferred_domains: str = ""
    yyds_blocked_domains: str = ""

    # CLI overrides
    email_filter: str = ""
    limit: int = 0
    offset: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve_path(self, path: str, base: Path | None = None) -> Path:
        p = Path(path).expanduser()
        if p.is_absolute():
            return p
        root = base or Path.cwd()
        return (root / p).resolve()


def load_config(path: str | Path | None = None, **overrides: Any) -> Config:
    data: dict[str, Any] = {}
    if path:
        cfg_path = Path(path).expanduser().resolve()
        if cfg_path.is_file():
            with cfg_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"config must be a mapping: {cfg_path}")
            data.update(loaded)

    # CLI / env-style overrides (ignore None)
    for k, v in overrides.items():
        if v is None:
            continue
        data[k] = v

    known = {f.name for f in Config.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in known and k != "extra"}
    cfg = Config(**kwargs)
    cfg.extra = {k: v for k, v in data.items() if k not in known}
    cfg.mode = (cfg.mode or "api").strip().lower()
    if cfg.mode not in {"api", "standalone"}:
        raise ValueError(f"unsupported mode: {cfg.mode!r} (use api|standalone)")
    cfg.account_source = (cfg.account_source or "file").strip().lower()
    if cfg.account_source not in {"file", "register"}:
        raise ValueError("unsupported account_source (use file|register)")
    cfg.mailbox_provider = (cfg.mailbox_provider or "").strip().lower()
    cfg.workers = max(int(cfg.workers or 1), 1)
    cfg.register_count = max(int(cfg.register_count or 0), 0)
    cfg.mailbox_max_retries = max(int(cfg.mailbox_max_retries or 1), 1)
    return cfg


def validate_config(cfg: Config) -> None:
    """Reject unsupported registration combinations before external activity."""
    if cfg.account_source != "register":
        return
    if not cfg.registration_enabled:
        raise ValueError("registration requires registration_enabled: true")
    if not cfg.mailbox_provider:
        raise ValueError("registration requires mailbox_provider")
    if cfg.mailbox_provider not in {"cloudflare", "duckmail", "yyds"}:
        raise ValueError("unsupported mailbox_provider (use cloudflare|duckmail|yyds)")
    if cfg.workers != 1:
        raise ValueError("registration currently requires workers: 1")
    if cfg.email_filter:
        raise ValueError("--email is not supported with account_source=register")
    if cfg.offset:
        raise ValueError("--offset is not supported with account_source=register")
    if not (cfg.register_count or cfg.limit):
        raise ValueError("registration requires register_count or --limit greater than zero")
