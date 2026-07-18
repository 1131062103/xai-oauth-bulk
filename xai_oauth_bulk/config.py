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
    cfg.workers = max(int(cfg.workers or 1), 1)
    return cfg
