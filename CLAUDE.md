# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python tool for authorized bulk xAI OAuth Device Code logins that produces credentials compatible with CLIProxyAPI (CPA). It requires Google Chrome or Chromium and uses DrissionPage for headed browser automation. The default serial, headed execution is intentional for Cloudflare/Turnstile reliability.

## Commands

```bash
# Create an environment and install runtime dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure local inputs (both files are gitignored)
cp config.example.yaml config.yaml
cp accounts.example.txt accounts.txt

# API mode: CPA must be running and configured with its management key
python run.py --config config.yaml

# Standalone mode: write CPA-compatible credentials locally
python run.py --mode standalone --accounts accounts.txt --out-dir output/auths

# Target one authorized account (the closest available focused integration smoke run)
python run.py --mode standalone --accounts accounts.txt --email user@example.com

# Process a bounded batch
python run.py --mode standalone --accounts accounts.txt --limit 5

# Equivalent module entry point
python -m xai_oauth_bulk --mode standalone --accounts accounts.txt --out-dir output/auths

# Run offline regression tests (or one test module)
python -m unittest discover -s tests -v
python -m unittest tests.test_cpa_precheck -v

# Basic offline syntax validation
python -m compileall -q xai_oauth_bulk run.py
```

There is no configured build system, linter, or type checker. OAuth runs are live browser integrations and require accounts the operator owns or is authorized to manage; use a one-account run rather than treating them as routine tests.

## Architecture

`run.py` and `python -m xai_oauth_bulk` both enter `xai_oauth_bulk.cli`. The CLI merges YAML configuration and command-line overrides through `config.load_config`, then resolves relative accounts/output paths and calls `runner.run_batch`. A nonzero exit status means at least one account failed.

`runner.py` is the batch coordinator. It parses and filters deduplicated account entries with `accounts.py`, selects API or standalone processing, optionally uses a thread pool, writes failures to `output/failed.jsonl`, and always closes each isolated browser in `finally`.

Two token-acquisition paths share the browser UI flow:

- **`api` mode (default):** when `skip_existing` is enabled, `runner.py` inventories CPA xAI filenames once via `cpa_client.py` and skips exact existing `xai-<email>.json` matches. It then asks CPA for device-login URLs and polls completion for the remaining accounts. CPA persists the credential in its configured auth directory; `.api-ok-*.txt` is only a local success marker.
- **`standalone` mode:** `oauth_device.py` requests a device code directly from xAI, polls its token endpoint on a background thread while the browser completes consent, then `schema.py` creates CPA's credential payload and `writer.py` atomically stores `xai-<email>.json` in `out_dir`.

`browser/isolation.py` creates a fresh Chromium profile and CDP port per account and removes the temporary profile after use. `browser/flow.py` drives the device-login screens; `browser/turnstile.py` waits for Turnstile and uses ordinary widget interaction only, so an operator may need to complete a checkbox manually in the headed browser.

## Configuration and outputs

`config.example.yaml` documents the supported configuration keys. Important defaults in `Config` are `mode: api`, `headless: false`, `workers: 1`, and `skip_existing: true`.

Account input accepts `email:password` or `email,password`; blank lines and comments are ignored. Main local outputs are `output/auths/xai-*.json` for standalone credentials, `output/auths/.api-ok-*.txt` for API-mode markers, and `output/failed.jsonl` for failures.
