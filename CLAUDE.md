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

# Explicitly opt into a one-account registration run (only for authorized provisioning)
python run.py --config config.yaml --account-source register --enable-registration --register-count 1 --headed

# Process a bounded batch
python run.py --mode standalone --accounts accounts.txt --limit 5

# Equivalent module entry point
python -m xai_oauth_bulk --mode standalone --accounts accounts.txt --out-dir output/auths

# Run offline regression tests (or focused registration modules)
python -m unittest discover -s tests -v
python -m unittest tests.test_cpa_precheck tests.test_mailbox tests.test_registration tests.test_registration_flow -v

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

`browser/isolation.py` creates a fresh Chromium profile and CDP port per account and removes the temporary profile after use. `browser/flow.py` drives device-login and authorized registration screens; `browser/turnstile.py` waits for Turnstile and uses ordinary widget interaction only, so an operator may need to complete a checkbox manually in the headed browser.

Registration has a separate, deliberately guarded dispatch in `runner.py`: `account_source: register`, `registration_enabled: true`, a supported mailbox provider, and a positive count are all required. It always runs serially (`workers: 1`) with dynamically provisioned mailbox addresses. `mailbox.py` supports Cloudflare-compatible, DuckMail, and YYDS providers, while `registration.py` builds a profile and `account_ledger.py` atomically records a successful account locally. Registration does not run the CPA duplicate precheck because no email exists to check before provisioning; normal file-based API batches still do.

## Configuration and outputs

`config.example.yaml` documents the supported configuration keys. Important defaults in `Config` are `mode: api`, `headless: false`, `workers: 1`, `skip_existing: true`, and `account_source: file`. Registration remains disabled until `registration_enabled: true`; it requires `account_source: register`, `register_count` (or `--limit`), and `mailbox_provider`. Provider endpoints and credentials are intentionally blank placeholders for Cloudflare-compatible, DuckMail, and YYDS services the operator is authorized to use.

Account input accepts `email:password` or `email,password`; blank lines and comments are ignored. Main local outputs are `output/auths/xai-*.json` for standalone credentials, `output/auths/.api-ok-*.txt` for API-mode markers, `output/failed.jsonl` for failures, `output/accounts.jsonl` for the registration ledger, and `output/accounts-registered.txt` (`email:password` lines for reuse). Credentials are written as soon as browser registration succeeds (`status=registered`); a second ledger line with `status=oauth_ok` is appended after OAuth completes. Treat both account files as secrets; they exclude mailbox tokens, verification codes, and OAuth tokens.

All unit tests are offline. They mock remote mailbox, CPA, and browser integration edges, so they neither create accounts nor access external services; live browser/OAuth work remains an authorized, manual integration exercise.
