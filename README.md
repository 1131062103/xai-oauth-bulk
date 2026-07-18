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

1. `GET /v0/management/xai-auth-url`
2. Browser completes device login
3. Poll `GET /v0/management/get-auth-status?state=...` until `ok`
4. CPA writes the auth file into its own `auth-dir`
5. On failure: `DELETE /v0/management/oauth-session?state=...`

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
```

Also: `python -m xai_oauth_bulk ...` from this directory.

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
| `output/auths/.api-ok-*.txt` | api mode local success markers |
| `output/failed.jsonl` | failed jobs |

## Disclaimer

For authorized account management and integration testing with your own CLIProxyAPI instance only. Respect xAI terms of service and applicable law.
