"""CLI entry for xai-oauth-bulk."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config, validate_config
from .runner import run_batch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xai-oauth-bulk",
        description="Bulk xAI OAuth login for CLIProxyAPI (api | standalone)",
    )
    p.add_argument("--config", default="", help="Path to config.yaml")
    p.add_argument("--mode", choices=["api", "standalone"], default=None)
    p.add_argument("--accounts", dest="accounts_file", default=None)
    p.add_argument("--account-source", choices=["file", "register"], default=None)
    p.add_argument("--enable-registration", dest="registration_enabled", action="store_true", default=None)
    p.add_argument("--register-count", type=int, default=None)
    p.add_argument("--registration-timeout-sec", type=float, default=None)
    p.add_argument("--mailbox-provider", choices=["cloudflare", "duckmail", "yyds"], default=None)
    p.add_argument("--account-ledger", dest="account_ledger_path", default=None)
    p.add_argument("--out-dir", dest="out_dir", default=None)
    p.add_argument("--cpa-base-url", dest="cpa_base_url", default=None)
    p.add_argument("--cpa-management-key", dest="cpa_management_key", default=None)
    p.add_argument("--proxy", default=None)
    p.add_argument("--chrome-path", dest="chrome_path", default=None)
    p.add_argument("--headless", action="store_true", default=None)
    p.add_argument("--headed", action="store_true", default=False)
    p.add_argument("--browser-timeout-sec", dest="browser_timeout_sec", type=float, default=None)
    p.add_argument("--sleep", dest="sleep_between_sec", type=float, default=None)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--base-url", dest="base_url", default=None)
    p.add_argument("--email", dest="email_filter", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--offset", type=int, default=None)
    p.add_argument("--skip-existing", action="store_true", default=None)
    p.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Prefer running from tool directory so relative paths resolve as expected
    tool_root = Path(__file__).resolve().parents[1]
    if Path.cwd() != tool_root:
        # Keep cwd; resolve config relative to tool root if present
        pass

    overrides: dict = {}
    for key in (
        "mode",
        "accounts_file",
        "account_source",
        "registration_enabled",
        "register_count",
        "registration_timeout_sec",
        "mailbox_provider",
        "account_ledger_path",
        "out_dir",
        "cpa_base_url",
        "cpa_management_key",
        "proxy",
        "chrome_path",
        "browser_timeout_sec",
        "sleep_between_sec",
        "workers",
        "base_url",
        "email_filter",
        "limit",
        "offset",
        "skip_existing",
    ):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val

    if args.headed:
        overrides["headless"] = False
    elif args.headless is True:
        overrides["headless"] = True

    cfg_path = args.config or ""
    if not cfg_path:
        candidate = tool_root / "config.yaml"
        if candidate.is_file():
            cfg_path = str(candidate)
        else:
            example = tool_root / "config.example.yaml"
            if example.is_file() and not overrides.get("accounts_file"):
                # allow pure CLI without config when accounts provided
                pass

    try:
        cfg = load_config(cfg_path or None, **overrides)
        if cfg.account_source == "register" and args.accounts_file is not None:
            raise ValueError("--accounts is not supported with account_source=register")
        validate_config(cfg)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    # Resolve relative paths against the tool root when appropriate.
    if cfg.account_source == "file" and not Path(cfg.accounts_file).is_absolute():
        cand = tool_root / cfg.accounts_file
        if cand.is_file() or not (Path.cwd() / cfg.accounts_file).is_file():
            cfg.accounts_file = str(cand)
    if not Path(cfg.out_dir).is_absolute():
        cfg.out_dir = str(tool_root / cfg.out_dir)
    if not Path(cfg.fail_log).is_absolute():
        cfg.fail_log = str(tool_root / cfg.fail_log)
    if not Path(cfg.account_ledger_path).is_absolute():
        cfg.account_ledger_path = str(tool_root / cfg.account_ledger_path)

    results = run_batch(cfg)
    failed = [r for r in results if not r.ok]
    return 1 if failed else 0
