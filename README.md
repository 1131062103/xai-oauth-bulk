# xai-oauth-bulk

Bulk xAI OAuth login helper for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).

Use this tool only with accounts you own or are authorized to manage.

## What it does

xAI CLI OAuth uses the **OAuth 2.0 Device Code** flow (RFC 8628), not a redirect callback.

For each account in your list:

1. Obtain a device verification URL (from CLIProxyAPI management API **or** direct xAI endpoints)
2. Open a **new fully isolated Chromium** (unique profile + debug port)
3. Automate the device UI:
   - Continue → Login with email → email → Next → password → wait Turnstile → Login
   - Consent: exact **Allow / 允许** (real click)
4. Wait until tokens are issued
5. Persist credentials as CPA-compatible `xai-<email>.json` (standalone) or let CPA save them (api mode)
6. Quit the browser and delete the temp profile

Default concurrency is **serial + headed** (most reliable with Cloudflare).

## Modes

### `api` (calls running CLIProxyAPI)

Requires CLIProxyAPI up with management key configured.

1. When `skip_existing` is enabled, `GET /v0/management/auth-files` once and skip exact matching `xai-<email>.json` files already held by CPA
2. `GET /v0/management/xai-auth-url` for each remaining account
3. Browser completes device login
4. Poll `GET /v0/management/get-auth-status?state=...` until `ok`
5. CPA writes the auth file into its own `auth-dir`
6. On failure: `DELETE /v0/management/oauth-session?state=...`

CPA is the authority for API-mode duplicate detection. If its auth-file inventory cannot be read, the batch stops before any browser or OAuth session starts. Use `--no-skip-existing` only when intentionally reauthorizing accounts; it bypasses this precheck.

Auth headers (both accepted by CPA):

- `Authorization: Bearer <management-key>`
- `X-Management-Key: <management-key>`

### `standalone`

Does not need CLIProxyAPI running.

1. Request device code from `auth.x.ai`
2. Browser completes login
3. Poll token endpoint
4. Write `out_dir/xai-<email>.json` (CPA-compatible schema)

Copy files into CLIProxyAPI `auths/` (or configured auth-dir) to load them.

## Install

```bash
cd tools/xai-oauth-bulk
python -m venv .venv
# Windows MSYS / bash:
source .venv/Scripts/activate
pip install -r requirements.txt
```

Requires Google Chrome or Chromium on the machine.

## Configure

```bash
cp config.example.yaml config.yaml
cp accounts.example.txt accounts.txt
# edit config.yaml and accounts.txt
```

`accounts.txt` formats:

```text
email:password
email,password
# comments ignored
```

## Run

```bash
# api mode (CPA running)
python run.py --config config.yaml

# standalone
python run.py --mode standalone --accounts accounts.txt --out-dir output/auths

# one account
python run.py --mode standalone --accounts accounts.txt --email user@example.com

# limit first N
python run.py --mode standalone --accounts accounts.txt --limit 5

# intentionally bypass existing-credential checks
python run.py --mode api --accounts accounts.txt --no-skip-existing
```

Also: `python -m xai_oauth_bulk ...` from this directory.

## Authorized account registration

Registration is deliberately **opt-in** and is only for accounts you own or are explicitly authorized to create. It provisions a mailbox, completes the xAI registration flow in a browser, then completes the normal OAuth flow. It does not import an account list.

1. Copy the example configuration and choose one mailbox provider you are authorized to use: `cloudflare`, `duckmail`, or `yyds`.
2. Set `account_source: register`, `registration_enabled: true`, a nonzero `register_count`, and `mailbox_provider` in `config.yaml`.
3. Fill the corresponding provider endpoint and credential placeholders. Do not commit those secrets.
4. Optional: copy `blocked_domains.example.txt` to `blocked_domains.txt` and list domains to reject (suffix match; e.g. `dpdns.org` blocks `v720f8y9y5@xx.lucky04.dpdns.org`). Filtered addresses are re-provisioned up to `mailbox_domain_filter_max_attempts`.
5. Keep `workers: 1` and `headless: false`. Registration rejects parallel workers; a visible browser lets the operator handle any Turnstile challenge normally.
6. Run a small, authorized batch first:

```bash
python run.py --config config.yaml --account-source register --enable-registration --register-count 1 --headed
```

`--limit` can bound a registration batch and takes precedence over `register_count`. `--accounts`, `--email`, and `--offset` cannot be used with `account_source=register`.

Registration email addresses are dynamically provisioned, so the API-mode CPA existing-credential precheck is intentionally not applicable and is skipped. This differs from file input, where `skip_existing: true` inventories CPA credentials before any OAuth/browser activity. CPA API mode still requires `cpa_management_key`.

### Mailbox provider settings

The provider settings in `config.example.yaml` are placeholders for deployments and credentials you control:

- **Cloudflare:** set `cloudflare_api_base`, `cloudflare_api_key`, and the matching `cloudflare_auth_mode`; customize the four endpoint paths only if your compatible service uses different routes.
- **DuckMail:** set `duckmail_api_base` and, if required by the authorized service, `duckmail_api_key`.
- **YYDS:** set `yyds_api_base` and either `yyds_api_key` or `yyds_jwt`. Optional comma-separated preferred and blocked domain lists are supported.
- **Domain filter (all providers):** `mailbox_blocked_domains_file` (default `blocked_domains.txt`) and/or `mailbox_blocked_domains` (comma-separated). Rules match exact domains and subdomains. A missing file is treated as an empty filter.

No provider credentials, mailbox tokens, or verification codes are written to the account ledger.

## Offline tests

The regression suite is offline: mailbox-provider responses, browser-page behavior, CPA precheck behavior, registration validation, profile generation, ledger writing, and registration dispatch are mocked or local. It does not provision mailboxes, create accounts, launch a browser, or contact xAI/CPA.

```bash
python -m unittest discover -s tests -v
# Registration-focused tests
python -m unittest tests.test_mailbox tests.test_registration tests.test_registration_flow -v
```

## Isolation guarantees

Per account the tool:

- creates a unique Chrome `user-data-dir` under the system temp folder
- uses `auto_port()` so CDP ports do not collide
- always quits the browser after the job
- best-effort deletes the temp profile

## Cloudflare / Turnstile notes

- Prefer **headed** browser (`headless: false`)
- The tool **waits** for `cf-turnstile-response` and tries a normal widget click
- It does **not** ship webdriver-spoofing extensions
- If Turnstile blocks automation, complete the checkbox manually in the opened window while the poller waits

## Outputs

| Path | Description |
| --- | --- |
| `output/auths/xai-*.json` | standalone credentials |
| `output/auths/.api-ok-*.txt` | api mode local success markers (not used to detect CPA credentials) |
| `output/failed.jsonl` | failed jobs |
| `output/accounts.jsonl` | successful registration ledger; contains generated account credentials, so protect it as a secret |

## Disclaimer

For authorized account management and integration testing with your own CLIProxyAPI instance only. Respect xAI terms of service and applicable law.
