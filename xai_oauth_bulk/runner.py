"""Batch orchestrator for dual-mode xAI OAuth login."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .account_ledger import append_account_ledger, save_registered_account
from .accounts import Account, filter_accounts, parse_accounts_file
from .browser.flow import approve_device_code, register_account
from .browser.isolation import IsolatedBrowser, create_isolated_browser
from .config import Config
from .cpa_client import CPAClient, CPAClientError, DeviceStartResult
from .mailbox import Mailbox, MailboxService
from .oauth_device import (
    DeviceCodeSession,
    OAuthDeviceError,
    TokenResult,
    poll_device_token,
    request_device_code,
)
from .registration import RegistrationProfile, build_registration_profile
from .schema import build_cpa_xai_auth, credential_file_name
from .writer import existing_auth_path, write_cpa_xai_auth

LogFn = Callable[[str], None]


@dataclass
class JobResult:
    email: str
    ok: bool
    mode: str
    path: str = ""
    error: str = ""
    skipped: bool = False


# ---------------------------------------------------------------------------
# Logging / failure bookkeeping
# ---------------------------------------------------------------------------


def _log_prefix(email: str, msg: str) -> str:
    return f"[{email}] {msg}"


def _elog(log: LogFn, email: str) -> LogFn:
    return lambda msg: log(_log_prefix(email, msg))


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


def _batch_summary(results: list[JobResult], log: LogFn) -> None:
    ok_n = sum(1 for r in results if r.ok and not r.skipped)
    skip_n = sum(1 for r in results if r.skipped)
    fail_n = sum(1 for r in results if not r.ok)
    log(f"batch done ok={ok_n} skipped={skip_n} failed={fail_n} total={len(results)}")


# ---------------------------------------------------------------------------
# Shared job plumbing
# ---------------------------------------------------------------------------


def _proxy(cfg: Config) -> str | None:
    return (cfg.proxy or "").strip() or None


def _require_cpa_key(cfg: Config, email: str) -> JobResult | None:
    if (cfg.cpa_management_key or "").strip():
        return None
    return JobResult(
        email=email,
        ok=False,
        mode="api",
        error="cpa_management_key is required for api mode",
    )


def _cpa_client(cfg: Config, proxy: str | None) -> CPAClient:
    return CPAClient(cfg.cpa_base_url, cfg.cpa_management_key, proxy=proxy)


def _open_browser(cfg: Config, proxy: str | None, elog: LogFn) -> IsolatedBrowser:
    return create_isolated_browser(
        headless=cfg.headless,
        proxy=proxy,
        chrome_path=cfg.chrome_path,
        log=elog,
    )


def _close_browser(ib: IsolatedBrowser | None, stop_event: threading.Event, elog: LogFn) -> None:
    stop_event.set()
    if ib is not None:
        ib.close(log=elog)


def _start_thread(target: Callable[[], None], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _box_result(
    box: dict[str, Any],
    key: str,
    err_box: dict[str, BaseException],
    *,
    missing: Exception,
) -> Any:
    if key not in box:
        if "err" in err_box:
            raise err_box["err"]
        raise missing
    return box[key]


def _write_api_ok_marker(out_dir: Path, email: str, state: str) -> Path:
    """Local success marker only — CPA already persisted the credential server-side."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = credential_file_name(email).removesuffix(".json")
    marker = out_dir / f".api-ok-{stem}.txt"
    marker.write_text(
        f"ok\nemail={email}\nstate={state}\nat={datetime.now(tz=timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )
    return marker


def _write_standalone_auth(
    *,
    email: str,
    token: TokenResult,
    sess: DeviceCodeSession,
    cfg: Config,
    out_dir: Path | str,
) -> Path:
    payload = build_cpa_xai_auth(
        email=email,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        id_token=token.id_token,
        expires_in=token.expires_in,
        base_url=cfg.base_url,
        token_endpoint=sess.token_endpoint,
    )
    return write_cpa_xai_auth(out_dir, payload)


def _start_token_poll(
    *,
    sess: DeviceCodeSession,
    proxy: str | None,
    stop_event: threading.Event,
    token_box: dict[str, Any],
    err_box: dict[str, BaseException],
    elog: LogFn,
    email: str,
    expires_in: int,
    success_log: str | None = "token poll SUCCESS",
    thread_name: str | None = None,
) -> threading.Thread:
    def _poll() -> None:
        try:
            time.sleep(1)
            tr = poll_device_token(
                sess.device_code,
                token_endpoint=sess.token_endpoint,
                interval=max(sess.interval, 5),
                expires_in=expires_in,
                log=elog,
                proxy=proxy,
                cancel=lambda: stop_event.is_set() and "token" not in token_box,
            )
            token_box["token"] = tr
            stop_event.set()
            if success_log:
                elog(success_log)
        except BaseException as e:  # noqa: BLE001
            err_box["err"] = e
            stop_event.set()

    return _start_thread(_poll, thread_name or f"oauth-poll-{email}")


def _start_cpa_status_poll(
    *,
    client: CPAClient,
    state: str,
    timeout_sec: float,
    stop_event: threading.Event,
    status_box: dict[str, Any],
    err_box: dict[str, BaseException],
    elog: LogFn,
    email: str,
    success_log: str | None = "CPA status SUCCESS",
    thread_name: str | None = None,
) -> threading.Thread:
    def _poll_status() -> None:
        try:
            client.wait_auth_status(
                state,
                timeout_sec=timeout_sec,
                interval_sec=2.0,
                log=elog,
                cancel=lambda: stop_event.is_set() and "ok" not in status_box,
            )
            status_box["ok"] = True
            stop_event.set()
            if success_log:
                elog(success_log)
        except BaseException as e:  # noqa: BLE001
            err_box["err"] = e
            stop_event.set()

    return _start_thread(_poll_status, thread_name or f"cpa-poll-{email}")


def _approve(
    ib: IsolatedBrowser,
    *,
    verification_uri_complete: str,
    account: Account,
    user_code: str,
    timeout_sec: float,
    stop_event: threading.Event,
    elog: LogFn,
    reopen: bool = True,
) -> None:
    approve_device_code(
        ib.page,
        verification_uri_complete=verification_uri_complete,
        email=account.email,
        password=account.password,
        user_code=user_code,
        timeout_sec=timeout_sec,
        stop_event=stop_event,
        log=elog,
        reopen=reopen,
    )


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def _registration_mailbox_service(cfg: Config) -> MailboxService:
    values = asdict(cfg)
    values["email_provider"] = cfg.mailbox_provider
    session = requests.Session()
    proxy = (cfg.proxy or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return MailboxService(values, session=session)


def _provision_registration_mailbox(
    cfg: Config,
    log: LogFn,
) -> tuple[MailboxService, Mailbox]:
    service = _registration_mailbox_service(cfg)
    last_error: Exception | None = None
    for attempt in range(1, cfg.mailbox_max_retries + 1):
        try:
            return service, service.provision(log=log)
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < cfg.mailbox_max_retries:
                log(f"mailbox provisioning retry {attempt + 1}/{cfg.mailbox_max_retries}")
    raise last_error or RuntimeError("mailbox provisioning failed")


def _run_browser_registration(
    *,
    ib: IsolatedBrowser,
    email: str,
    profile: RegistrationProfile,
    mailbox_service: MailboxService,
    mailbox: Mailbox,
    start_url: str,
    cfg: Config,
    stop_event: threading.Event,
    elog: LogFn,
) -> None:
    registered = register_account(
        ib.page,
        email=email,
        password=profile.password,
        first_name=profile.first_name,
        last_name=profile.last_name,
        display_name=profile.display_name,
        verification_code=lambda: mailbox_service.wait_for_code(
            mailbox,
            timeout_sec=cfg.mailbox_poll_timeout_sec,
            poll_interval_sec=cfg.mailbox_poll_interval_sec,
            log=elog,
        ),
        start_url=start_url,
        timeout_sec=cfg.registration_timeout_sec,
        stop_event=stop_event,
        log=elog,
    )
    if not registered:
        raise RuntimeError("registration did not complete")


def _save_registered_credentials(
    *,
    cfg: Config,
    email: str,
    profile: RegistrationProfile,
    mode: str,
    elog: LogFn,
) -> None:
    """Persist email/password as soon as browser sign-up succeeds."""
    ledger_path, creds_txt = save_registered_account(
        ledger_path=cfg.account_ledger_path,
        email=email,
        profile=profile,
        mode=mode,
        status="registered",
    )
    elog(f"saved credentials status=registered ledger={ledger_path} accounts_txt={creds_txt}")


def _record_oauth_ok(
    *,
    cfg: Config,
    email: str,
    profile: RegistrationProfile,
    mode: str,
    oauth_path: str,
) -> None:
    append_account_ledger(
        cfg.account_ledger_path,
        email=email,
        profile=profile,
        mode=mode,
        oauth_path=oauth_path,
        status="oauth_ok",
    )


# ---------------------------------------------------------------------------
# Per-account jobs: existing credentials (file source)
# ---------------------------------------------------------------------------


def run_one_standalone(account: Account, cfg: Config, log: LogFn) -> JobResult:
    email = account.email
    elog = _elog(log, email)
    out_dir = Path(cfg.out_dir).expanduser().resolve()
    if cfg.skip_existing:
        existing = existing_auth_path(out_dir, email)
        if existing:
            elog(f"skip existing {existing}")
            return JobResult(email=email, ok=True, mode="standalone", path=str(existing), skipped=True)

    proxy = _proxy(cfg)
    ib: IsolatedBrowser | None = None
    stop_event = threading.Event()
    token_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}

    try:
        sess = request_device_code(proxy=proxy)
        elog("device authorization started; opening browser")
        poll_thread = _start_token_poll(
            sess=sess,
            proxy=proxy,
            stop_event=stop_event,
            token_box=token_box,
            err_box=err_box,
            elog=elog,
            email=email,
            expires_in=min(sess.expires_in, int(cfg.browser_timeout_sec) + 60),
        )
        ib = _open_browser(cfg, proxy, elog)
        _approve(
            ib,
            verification_uri_complete=sess.verification_uri_complete,
            account=account,
            user_code=sess.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            elog=elog,
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 30)
        tr = _box_result(
            token_box,
            "token",
            err_box,
            missing=OAuthDeviceError("token poll ended without result"),
        )
        path = _write_standalone_auth(email=email, token=tr, sess=sess, cfg=cfg, out_dir=out_dir)
        elog(f"wrote {path}")
        return JobResult(email=email, ok=True, mode="standalone", path=str(path))
    except Exception as e:  # noqa: BLE001
        elog(f"FAILED: {e}")
        return JobResult(email=email, ok=False, mode="standalone", error=str(e))
    finally:
        _close_browser(ib, stop_event, elog)


def run_one_api(account: Account, cfg: Config, log: LogFn) -> JobResult:
    email = account.email
    elog = _elog(log, email)
    early = _require_cpa_key(cfg, email)
    if early is not None:
        return early

    out_dir = Path(cfg.out_dir).expanduser().resolve()
    proxy = _proxy(cfg)
    client = _cpa_client(cfg, proxy)
    state = ""
    ib: IsolatedBrowser | None = None
    stop_event = threading.Event()
    status_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}

    try:
        start = client.start_xai_oauth()
        state = start.state
        elog("CPA device authorization started; opening browser")
        poll_thread = _start_cpa_status_poll(
            client=client,
            state=state,
            timeout_sec=max(cfg.browser_timeout_sec, 60) + 30,
            stop_event=stop_event,
            status_box=status_box,
            err_box=err_box,
            elog=elog,
            email=email,
        )
        ib = _open_browser(cfg, proxy, elog)
        _approve(
            ib,
            verification_uri_complete=start.url,
            account=account,
            user_code=start.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            elog=elog,
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 60)
        _box_result(
            status_box,
            "ok",
            err_box,
            missing=CPAClientError("CPA status poll ended without success"),
        )
        marker = _write_api_ok_marker(out_dir, email, state)
        elog(f"CPA auth saved server-side; marker {marker}")
        return JobResult(email=email, ok=True, mode="api", path=str(marker))
    except Exception as e:  # noqa: BLE001
        elog(f"FAILED: {e}")
        if state:
            client.cancel_oauth_session(state)
        return JobResult(email=email, ok=False, mode="api", error=str(e))
    finally:
        _close_browser(ib, stop_event, elog)


# ---------------------------------------------------------------------------
# Per-account jobs: authorized registration + OAuth
# ---------------------------------------------------------------------------


def run_one_registered_standalone(cfg: Config, log: LogFn) -> JobResult:
    """Create one authorized account, then complete the standalone OAuth flow."""
    email = "registration"
    proxy = _proxy(cfg)
    ib: IsolatedBrowser | None = None
    stop_event = threading.Event()
    token_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}
    elog: LogFn = _elog(log, email)

    try:
        mailbox_service, mailbox = _provision_registration_mailbox(cfg, log)
        email = mailbox.address
        elog = _elog(log, email)
        profile = build_registration_profile()
        account = Account(email=email, password=profile.password)
        elog("mailbox provisioned; starting device authorization")
        sess = request_device_code(proxy=proxy)

        poll_thread = _start_token_poll(
            sess=sess,
            proxy=proxy,
            stop_event=stop_event,
            token_box=token_box,
            err_box=err_box,
            elog=elog,
            email=email,
            expires_in=min(
                sess.expires_in,
                int(cfg.registration_timeout_sec + cfg.browser_timeout_sec) + 60,
            ),
            success_log=None,
            thread_name=f"oauth-register-poll-{email}",
        )
        ib = _open_browser(cfg, proxy, elog)
        try:
            _run_browser_registration(
                ib=ib,
                email=email,
                profile=profile,
                mailbox_service=mailbox_service,
                mailbox=mailbox,
                start_url=sess.verification_uri_complete,
                cfg=cfg,
                stop_event=stop_event,
                elog=elog,
            )
        except RuntimeError as e:
            raise OAuthDeviceError(str(e)) from e
        _save_registered_credentials(cfg=cfg, email=email, profile=profile, mode="standalone", elog=elog)
        # Keep authenticated registration tab; do not reload device URI.
        _approve(
            ib,
            verification_uri_complete=sess.verification_uri_complete,
            account=account,
            user_code=sess.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            elog=elog,
            reopen=False,
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 30)
        tr = _box_result(
            token_box,
            "token",
            err_box,
            missing=OAuthDeviceError("token poll ended without result"),
        )
        path = _write_standalone_auth(
            email=email,
            token=tr,
            sess=sess,
            cfg=cfg,
            out_dir=cfg.out_dir,
        )
        _record_oauth_ok(
            cfg=cfg,
            email=email,
            profile=profile,
            mode="standalone",
            oauth_path=str(path),
        )
        elog(f"registration and OAuth completed oauth_path={path}")
        return JobResult(email=email, ok=True, mode="standalone", path=str(path))
    except Exception as e:  # noqa: BLE001
        elog(f"FAILED: {e}")
        return JobResult(email=email, ok=False, mode="standalone", error=str(e))
    finally:
        _close_browser(ib, stop_event, elog)


def run_one_registered_api(cfg: Config, log: LogFn) -> JobResult:
    """Create one authorized account, then complete CPA-managed OAuth."""
    email = "registration"
    early = _require_cpa_key(cfg, email)
    if early is not None:
        return early

    proxy = _proxy(cfg)
    client = _cpa_client(cfg, proxy)
    ib: IsolatedBrowser | None = None
    state = ""
    stop_event = threading.Event()
    status_box: dict[str, Any] = {}
    err_box: dict[str, BaseException] = {}
    elog: LogFn = _elog(log, email)

    try:
        mailbox_service, mailbox = _provision_registration_mailbox(cfg, log)
        email = mailbox.address
        elog = _elog(log, email)
        profile = build_registration_profile()
        account = Account(email=email, password=profile.password)
        start: DeviceStartResult = client.start_xai_oauth()
        state = start.state
        elog("mailbox provisioned; starting CPA authorization")

        poll_thread = _start_cpa_status_poll(
            client=client,
            state=state,
            timeout_sec=max(cfg.registration_timeout_sec + cfg.browser_timeout_sec, 60) + 30,
            stop_event=stop_event,
            status_box=status_box,
            err_box=err_box,
            elog=elog,
            email=email,
            success_log=None,
            thread_name=f"cpa-register-poll-{email}",
        )
        ib = _open_browser(cfg, proxy, elog)
        try:
            _run_browser_registration(
                ib=ib,
                email=email,
                profile=profile,
                mailbox_service=mailbox_service,
                mailbox=mailbox,
                start_url=start.url,
                cfg=cfg,
                stop_event=stop_event,
                elog=elog,
            )
        except RuntimeError as e:
            raise CPAClientError(str(e)) from e
        _save_registered_credentials(cfg=cfg, email=email, profile=profile, mode="api", elog=elog)
        # Keep authenticated registration tab; Continue → Allow is in-session.
        _approve(
            ib,
            verification_uri_complete=start.url,
            account=account,
            user_code=start.user_code,
            timeout_sec=cfg.browser_timeout_sec,
            stop_event=stop_event,
            elog=elog,
            reopen=False,
        )
        poll_thread.join(timeout=max(cfg.browser_timeout_sec, 60) + 60)
        _box_result(
            status_box,
            "ok",
            err_box,
            missing=CPAClientError("CPA status poll ended without success"),
        )
        out_dir = Path(cfg.out_dir).expanduser().resolve()
        marker = _write_api_ok_marker(out_dir, email, state)
        oauth_ref = f"CPA:{credential_file_name(email)}"
        _record_oauth_ok(
            cfg=cfg,
            email=email,
            profile=profile,
            mode="api",
            oauth_path=oauth_ref,
        )
        elog(f"registration and CPA OAuth completed oauth_path={oauth_ref}")
        return JobResult(email=email, ok=True, mode="api", path=str(marker))
    except Exception as e:  # noqa: BLE001
        elog(f"FAILED: {e}")
        if state:
            client.cancel_oauth_session(state)
        return JobResult(email=email, ok=False, mode="api", error=str(e))
    finally:
        _close_browser(ib, stop_event, elog)


# ---------------------------------------------------------------------------
# Batch entry
# ---------------------------------------------------------------------------


def run_batch(cfg: Config, log: LogFn | None = None) -> list[JobResult]:
    log = log or (lambda m: print(m, flush=True))
    fail_log = Path(cfg.fail_log).expanduser()
    if not fail_log.is_absolute():
        fail_log = Path.cwd() / fail_log

    if cfg.account_source == "register":
        return _run_registration_batch(cfg, log, fail_log)
    return _run_file_batch(cfg, log, fail_log)


def _run_registration_batch(cfg: Config, log: LogFn, fail_log: Path) -> list[JobResult]:
    count = cfg.limit or cfg.register_count
    log(
        f"registration batch start mode={cfg.mode} count={count} workers={cfg.workers} "
        f"headless={cfg.headless}"
    )
    log("CPA precheck: not applicable to dynamically provisioned registration emails")
    register_one = run_one_registered_api if cfg.mode == "api" else run_one_registered_standalone
    results: list[JobResult] = []
    for index in range(count):
        result = register_one(cfg, log)
        results.append(result)
        _record_failure(fail_log, result)
        if index + 1 < count and cfg.sleep_between_sec > 0:
            time.sleep(cfg.sleep_between_sec)
    _batch_summary(results, log)
    return results


def _run_file_batch(cfg: Config, log: LogFn, fail_log: Path) -> list[JobResult]:
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

    results: list[JobResult] = []
    if cfg.mode == "api" and cfg.skip_existing:
        results, accounts = _cpa_skip_existing(cfg, accounts, log, fail_log)
        if not accounts and results and all(not r.ok for r in results):
            # Precheck hard-failed the whole batch.
            return results

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

    _batch_summary(results, log)
    return results


def _cpa_skip_existing(
    cfg: Config,
    accounts: list[Account],
    log: LogFn,
    fail_log: Path,
) -> tuple[list[JobResult], list[Account]]:
    """Inventory CPA once; return (skipped-or-failed results, pending accounts)."""
    log("CPA precheck: listing existing xAI credential files")
    try:
        existing_names = _cpa_client(cfg, _proxy(cfg)).list_xai_auth_file_names()
    except CPAClientError as e:
        error = f"CPA precheck failed: unable to list auth files: {e}"
        log(error)
        results = [
            JobResult(email=acc.email, ok=False, mode="api", error=error)
            for acc in accounts
        ]
        for result in results:
            _record_failure(fail_log, result)
        _batch_summary(results, log)
        return results, []

    results: list[JobResult] = []
    pending: list[Account] = []
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
            pending.append(acc)
    log(
        f"CPA precheck: found {len(existing_names)} xAI credentials; "
        f"skipped {len(results)} accounts"
    )
    return results, pending
