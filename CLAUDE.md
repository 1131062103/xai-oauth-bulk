# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python tool for authorized bulk xAI OAuth Device Code logins that produces credentials compatible with CLIProxyAPI (CPA). It requires Google Chrome or Chromium and uses DrissionPage for headed browser automation. The default serial, headed execution is intentional for Cloudflare/Turnstile reliability.

Use only with accounts the operator owns or is authorized to manage.

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

# Target one authorized account (closest focused integration smoke run)
python run.py --mode standalone --accounts accounts.txt --email user@example.com

# Process a bounded batch / intentionally reauthorize (bypass skip_existing)
python run.py --mode standalone --accounts accounts.txt --limit 5
python run.py --mode api --accounts accounts.txt --no-skip-existing

# Explicitly opt into a one-account registration run (authorized provisioning only)
python run.py --config config.yaml --account-source register --enable-registration --register-count 1 --headed

# Equivalent module entry point
python -m xai_oauth_bulk --mode standalone --accounts accounts.txt --out-dir output/auths

# Offline regression suite (no browser, CPA, mailbox, or xAI network calls)
python -m unittest discover -s tests -v

# Focused modules
python -m unittest tests.test_cpa_precheck tests.test_mailbox tests.test_registration \
  tests.test_registration_flow tests.test_post_reg_handoff \
  tests.test_profile_form_fill tests.test_verification_code_fill -v

# Single test class or method
python -m unittest tests.test_cpa_precheck.TestCPAPrecheck -v
python -m unittest tests.test_post_reg_handoff.TestPostRegHandoff.test_approve_keeps_session -v

# Basic offline syntax validation
python -m compileall -q xai_oauth_bulk run.py
```

There is no configured build system, linter, or type checker. Live OAuth/browser runs are authorized integrations, not routine tests—prefer one-account runs.

Exit codes from `cli.main`: `0` all jobs ok/skipped, `1` at least one failure, `2` config/validation error.

## Architecture

`run.py` and `python -m xai_oauth_bulk` both enter `xai_oauth_bulk.cli`. The CLI merges YAML + CLI overrides via `config.load_config` / `validate_config`, resolves relative `accounts_file` / `out_dir` / `fail_log` / `account_ledger_path` against the tool root, then calls `runner.run_batch`.

`runner.py` is the batch coordinator:

- **File source** (`account_source: file`): parse/filter accounts (`accounts.py`), optional CPA or local skip-existing, then `run_one_api` or `run_one_standalone` serially or via `ThreadPoolExecutor`. Failures append to `fail_log` (`output/failed.jsonl`). Each job always closes its isolated browser in `finally`.
- **Register source** (`account_source: register`): separate guarded path—requires `registration_enabled`, supported `mailbox_provider`, and positive `register_count` or `--limit`. Always serial (`workers: 1`). Dynamic emails only; `--accounts` / `--email` / `--offset` are rejected.

Two token-acquisition paths share the browser UI flow:

- **`api` mode (default):** when `skip_existing` is on, inventories CPA xAI filenames once via `cpa_client.CPAClient.list_xai_auth_file_names` and skips exact `xai-<email>.json` matches. If that inventory call fails, the **entire batch fails** before any browser/OAuth session starts. Remaining accounts use CPA device-login URL + status poll; CPA persists credentials in its auth-dir. Local `.api-ok-*.txt` markers are not used for skip detection. On failure, CPA OAuth session is cancelled. Use `--no-skip-existing` only when intentionally reauthorizing.
- **`standalone` mode:** `oauth_device.py` requests a device code from xAI, polls the token endpoint on a background thread while the browser completes consent, then `schema.build_cpa_xai_auth` + `writer.write_cpa_xai_auth` atomically store `xai-<email>.json` under `out_dir`. Skip-existing checks the local out_dir only.

Browser stack:

- `browser/isolation.py` — fresh Chromium profile + unique CDP port per account; best-effort profile delete on close.
- `browser/flow.py` — large RPA module: login consent (`approve_device_code`) and registration (`register_account`). After registration, OAuth must call `approve_device_code(..., reopen=False)` so the authenticated session keeps the in-tab Continue → Allow handoff (do not reload the device URI).
- `browser/turnstile.py` — waits for Turnstile and uses ordinary widget interaction only; operators may need to complete a checkbox manually in the headed window.

Registration pipeline (per account): provision mailbox (`mailbox.py`: cloudflare / duckmail / yyds) → `registration.build_registration_profile` → start device/CPA OAuth in parallel with browser sign-up → on browser success immediately `account_ledger.save_registered_account` (`status=registered`) → in-session `approve_device_code(reopen=False)` → on OAuth success append ledger `status=oauth_ok`. Early credential save ensures OAuth failure does not lose the new account. Registration skips CPA duplicate precheck (no email exists beforehand); API mode still needs `cpa_management_key`.

## Configuration and outputs

`config.example.yaml` documents keys. Important `Config` defaults: `mode: api`, `headless: false`, `workers: 1`, `skip_existing: true`, `account_source: file`. Registration stays off until `registration_enabled: true` with `account_source: register`, nonzero count, and `mailbox_provider`. Provider endpoint/credential fields are blank placeholders for authorized services only.

Account input: `email:password` or `email,password`; blank lines and `#` comments ignored.

| Path | Role |
| --- | --- |
| `output/auths/xai-*.json` | standalone CPA-compatible credentials |
| `output/auths/.api-ok-*.txt` | API-mode local success markers only |
| `output/failed.jsonl` | failed jobs |
| `output/accounts.jsonl` | registration ledger (`registered` then optional `oauth_ok`) |
| `output/accounts-registered.txt` | `email:password` lines for later file-mode reuse |

Treat ledger/credentials files as secrets. They store generated email/password (and names), not mailbox tokens, verification codes, or OAuth tokens. `config.yaml`, `accounts.txt`, and `output/` are gitignored.

## Tests

All unit tests are offline. They mock mailbox providers, CPA, and browser page edges—no live account creation or external services. Coverage centers on CPA precheck/batch abort, mailbox providers, registration guards/dispatch, profile/OTP form fill, and post-registration OAuth handoff (`reopen=False`). Live browser/OAuth remains a manual authorized integration exercise.
