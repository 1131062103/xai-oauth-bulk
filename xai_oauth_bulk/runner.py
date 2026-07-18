"""Batch orchestrator for dual-mode xAI OAuth login."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .accounts import Account, filter_accounts, parse_accounts_file
from .browser.flow import approve_device_code
from .browser.isolation import create_isolated_browser
from .config import Config
from .cpa_client import CPAClient, CPAClientError
from .oauth_device import OAuthDeviceError, poll_device_token, request_device_code
from .schema import build_cpa_xai_auth, credential_file_name
from .writer import existing_auth_path, write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class JobResult:
    email: str
    ok: bool
    mode: str
    path: str = ""
    error: str = ""
    skipped: bool = False


def _log_prefix(email: str, msg: str) -> str:
    return f"[{email}] {msg}"


def _append_fail_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record_failure(path: Path, result: JobResult) -> None:
    if result.ok or result.skipped:
        return
    _append_fail_log(
        path,
        {
            "email": result.email,
            "mode": result.mode,
            "error": result.error,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        },
    )


def run_one_standalone(account: Account, cfg: Config, log: LogFn) -> JobResult:
    email = account.email
    out_dir = Path(cfg.out_dir).expanduser().resolve()
    if cfg.skip_existing:
        existing = existing_auth_path(out_dir, email)
        if existing:
            log(_log_prefix(email, f"skip existing {existing}"))
            return JobResult(email=email, ok=True, mode="standalone", path=str(existing), skipped=True)

    proxy = (cfg.proxy or "").strip() or None
    ib = None
    stop_event = threading.Event()
    token_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}

    try:
        sess = request_device_code(proxy=proxy)
        log(_log_prefix(email, "device authorization started; opening browser"))

        def _poll() -> None:
            try:
                time.sleep(1)
                tr = poll_device_token(
                    sess.device_code,
                    token_endpoint=sess.token_endpoint,
                    interval=max(sess.interval, 5),
                    expires_in=min(sess.expires_in, int(cfg.browser_timeout_sec) + 60),
                    log=lambda m: log(_log_prefix(email, m)),
                    proxy=proxy,
                    cancel=lambda: stop_event.is_set() and "token" not in token_box,
                )
                token_box["token"] = tr
                stop_event.set()
                log(_log_prefix(email, "token poll SUCCESS"))
            except BaseException as e:  # noqa: BLE001
                err_box["err"] = e
                stop_event.set()

        poll_thread = threading.Thread(target=_poll, name=f"oauth-poll-{email}", daemon=True)
        poll_thread.start()

        ib = create_isolated_browser(
            headless=cfg.headless,
            proxy=proxy,
            chrome_path=cfg.chrome_path,
            log=lambda m: log(_log_prefix(email, m)),
        )
        approve_device_code(
            ib.page,
            verification_uri_complete=sess.verification_uri_complete,
            email=account.email,
            password=account.password,
            user_code=sess.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            log=lambda m: log(_log_prefix(email, m)),
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 30)

        if "token" not in token_box:
            if "err" in err_box:
                raise err_box["err"]
            raise OAuthDeviceError("token poll ended without result")

        tr = token_box["token"]
        payload = build_cpa_xai_auth(
            email=email,
            access_token=tr.access_token,
            refresh_token=tr.refresh_token,
            id_token=tr.id_token,
            expires_in=tr.expires_in,
            base_url=cfg.base_url,
            token_endpoint=sess.token_endpoint,
        )
        path = write_cpa_xai_auth(out_dir, payload)
        log(_log_prefix(email, f"wrote {path}"))
        return JobResult(email=email, ok=True, mode="standalone", path=str(path))
    except Exception as e:  # noqa: BLE001
        log(_log_prefix(email, f"FAILED: {e}"))
        return JobResult(email=email, ok=False, mode="standalone", error=str(e))
    finally:
        stop_event.set()
        if ib is not None:
            ib.close(log=lambda m: log(_log_prefix(email, m)))


def run_one_api(account: Account, cfg: Config, log: LogFn) -> JobResult:
    email = account.email
    out_dir = Path(cfg.out_dir).expanduser().resolve()

    if not (cfg.cpa_management_key or "").strip():
        return JobResult(
            email=email,
            ok=False,
            mode="api",
            error="cpa_management_key is required for api mode",
        )

    proxy = (cfg.proxy or "").strip() or None
    client = CPAClient(
        cfg.cpa_base_url,
        cfg.cpa_management_key,
        proxy=proxy,
    )
    state = ""
    ib = None
    stop_event = threading.Event()
    status_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}

    try:
        start = client.start_xai_oauth()
        state = start.state
        log(_log_prefix(email, "CPA device authorization started; opening browser"))

        def _poll_status() -> None:
            try:
                client.wait_auth_status(
                    state,
                    timeout_sec=max(cfg.browser_timeout_sec, 60) + 30,
                    interval_sec=2.0,
                    log=lambda m: log(_log_prefix(email, m)),
                    cancel=lambda: stop_event.is_set() and "ok" not in status_box,
                )
                status_box["ok"] = True
                stop_event.set()
                log(_log_prefix(email, "CPA status SUCCESS"))
            except BaseException as e:  # noqa: BLE001
                err_box["err"] = e
                stop_event.set()

        poll_thread = threading.Thread(target=_poll_status, name=f"cpa-poll-{email}", daemon=True)
        poll_thread.start()

        ib = create_isolated_browser(
            headless=cfg.headless,
            proxy=proxy,
            chrome_path=cfg.chrome_path,
            log=lambda m: log(_log_prefix(email, m)),
        )
        approve_device_code(
            ib.page,
            verification_uri_complete=start.url,
            email=account.email,
            password=account.password,
            user_code=start.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            log=lambda m: log(_log_prefix(email, m)),
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 60)

        if "ok" not in status_box:
            if "err" in err_box:
                raise err_box["err"]
            raise CPAClientError("CPA status poll ended without success")

        # Optional local mirror: CPA already saved auth server-side.
        # We only write a marker note if out_dir is set — do not invent tokens.
        marker = out_dir / f".api-ok-{credential_file_name(email).replace('.json', '')}.txt"
        out_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"ok\nemail={email}\nstate={state}\nat={datetime.now(tz=timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        log(_log_prefix(email, f"CPA auth saved server-side; marker {marker}"))
        return JobResult(email=email, ok=True, mode="api", path=str(marker))
    except Exception as e:  # noqa: BLE001
        log(_log_prefix(email, f"FAILED: {e}"))
        if state:
            client.cancel_oauth_session(state)
        return JobResult(email=email, ok=False, mode="api", error=str(e))
    finally:
        stop_event.set()
        if ib is not None:
            ib.close(log=lambda m: log(_log_prefix(email, m)))


def run_batch(cfg: Config, log: LogFn | None = None) -> list[JobResult]:
    log = log or (lambda m: print(m, flush=True))
    accounts_path = Path(cfg.accounts_file).expanduser()
    if not accounts_path.is_absolute():
        accounts_path = Path.cwd() / accounts_path
    accounts = parse_accounts_file(accounts_path)
    accounts = filter_accounts(
        accounts,
        email=cfg.email_filter,
        offset=cfg.offset,
        limit=cfg.limit,
    )
    if not accounts:
        log("no accounts to process")
        return []

    log(
        f"batch start mode={cfg.mode} count={len(accounts)} workers={cfg.workers} "
        f"headless={cfg.headless}"
    )

    fail_log = Path(cfg.fail_log).expanduser()
    if not fail_log.is_absolute():
        fail_log = Path.cwd() / fail_log

    results: list[JobResult] = []
    if cfg.mode == "api" and cfg.skip_existing:
        log("CPA precheck: listing existing xAI credential files")
        try:
            existing_names = CPAClient(
                cfg.cpa_base_url,
                cfg.cpa_management_key,
                proxy=(cfg.proxy or "").strip() or None,
            ).list_xai_auth_file_names()
        except CPAClientError as e:
            error = f"CPA precheck failed: unable to list auth files: {e}"
            log(error)
            results = [
                JobResult(email=acc.email, ok=False, mode="api", error=error)
                for acc in accounts
            ]
            for result in results:
                _record_failure(fail_log, result)
            log(f"batch done ok=0 skipped=0 failed={len(results)} total={len(results)}")
            return results

        pending_accounts = []
        for acc in accounts:
            name = credential_file_name(acc.email)
            if name in existing_names:
                log(_log_prefix(acc.email, "skipped"))
                results.append(
                    JobResult(
                        email=acc.email,
                        ok=True,
                        mode="api",
                        path=f"CPA:{name}",
                        skipped=True,
                    )
                )
            else:
                pending_accounts.append(acc)
        log(
            f"CPA precheck: found {len(existing_names)} xAI credentials; "
            f"skipped {len(results)} accounts"
        )
        accounts = pending_accounts

    worker_fn = run_one_api if cfg.mode == "api" else run_one_standalone

    if cfg.workers <= 1:
        for i, acc in enumerate(accounts):
            r = worker_fn(acc, cfg, log)
            results.append(r)
            _record_failure(fail_log, r)
            if i + 1 < len(accounts) and cfg.sleep_between_sec > 0:
                time.sleep(cfg.sleep_between_sec)
    else:
        with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
            futs = {ex.submit(worker_fn, acc, cfg, log): acc for acc in accounts}
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                _record_failure(fail_log, r)

    ok_n = sum(1 for r in results if r.ok and not r.skipped)
    skip_n = sum(1 for r in results if r.skipped)
    fail_n = sum(1 for r in results if not r.ok)
    log(f"batch done ok={ok_n} skipped={skip_n} failed={fail_n} total={len(results)}")
    return results
