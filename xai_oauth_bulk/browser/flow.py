"""xAI device-code browser approval flow (email/password RPA)."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from .turnstile import wait_turnstile

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


class BrowserFlowError(RuntimeError):
    pass


def _sleep(sec: float) -> None:
    time.sleep(sec)


def _page_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _visible_text(page: Any) -> str:
    try:
        t = page.run_js(
            "return (document.body && (document.body.innerText || document.body.textContent)) || '';"
        )
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        pass
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _find_button_exact(page: Any, label: str) -> Any | None:
    try:
        for el in page.eles("tag:button") or []:
            try:
                if _norm(el.text or "") == label:
                    return el
            except Exception:
                continue
    except Exception:
        pass
    try:
        return page.ele(f"xpath://button[normalize-space(.)='{label}']", timeout=0.3)
    except Exception:
        return None


def _click_exact(
    page: Any,
    labels: list[str],
    log: LogFn,
    *,
    real: bool = False,
) -> str | None:
    for label in labels:
        el = _find_button_exact(page, label)
        if not el:
            continue
        try:
            if real:
                try:
                    el.scroll.to_see()
                except Exception:
                    pass
                el.click()
                log(f"clicked REAL exact {label!r}")
            else:
                el.click(by_js=True)
                log(f"clicked JS exact {label!r}")
            return label
        except Exception as e:
            log(f"click {label!r} failed: {e}")
            if real:
                try:
                    el.click(by_js=True)
                    log(f"clicked JS fallback exact {label!r}")
                    return label
                except Exception as e2:
                    log(f"js fallback {label!r} failed: {e2}")
    return None


def _fill(page: Any, selector: str, value: str, log: LogFn, label: str) -> None:
    try:
        el = page.ele(selector, timeout=1.0)
        if el is None:
            return
        try:
            cur = (el.value or "") if hasattr(el, "value") else ""
        except Exception:
            cur = ""
        if cur == value:
            return
        try:
            el.clear()
        except Exception:
            pass
        el.input(value)
        log(f"filled {label}")
    except Exception as e:
        log(f"fill {label} failed: {e}")


def approve_device_code(
    page: Any,
    *,
    verification_uri_complete: str,
    email: str,
    password: str,
    user_code: str = "",
    timeout_sec: float = 240.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> None:
    """Drive the xAI device authorization UI until done or timeout.

    Token acquisition is external (CPA poll or local device token poll).
    stop_event may be set by the poller when tokens are ready.
    """
    log = log or _noop
    if page is None:
        raise BrowserFlowError("page is None")
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        raise BrowserFlowError("email/password required")

    if not user_code and "user_code=" in (verification_uri_complete or ""):
        try:
            user_code = verification_uri_complete.split("user_code=", 1)[1].split("&", 1)[0]
        except Exception:
            user_code = ""

    log(f"open device url: {verification_uri_complete}")
    try:
        page.get(verification_uri_complete, timeout=60)
    except TypeError:
        page.get(verification_uri_complete)
    _sleep(1.0)

    deadline = time.time() + timeout_sec
    phase = "device"
    login_attempts = 0
    last_url = ""

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("stop_event set — leave browser loop")
            return

        url = _page_url(page)
        text = _visible_text(page)
        if url != last_url:
            log(f"url: {url[:180]}")
            last_url = url
            snip = _norm(text)[:160]
            if snip:
                log(f"visible: {snip}")

        if "device/done" in url or "设备已授权" in text or "device authorized" in text.lower():
            log("device done page — waiting for token poll")
            _sleep(1.5)
            continue

        if "Invalid action" in text:
            log("Invalid action — reopen device uri")
            page.get(verification_uri_complete)
            _sleep(1.2)
            phase = "device"
            continue

        low = (text or "").lower()
        if (
            "404" in (url or "")
            or "not found" in low
            or "页面不存在" in (text or "")
            or "this page could not be found" in low
        ):
            log(f"404/not-found — reopen device uri")
            try:
                page.get(verification_uri_complete)
            except Exception as e:
                log(f"reopen failed: {e}")
            _sleep(1.2)
            phase = "device"
            continue

        # Consent — real click on exact Allow only
        if "/consent" in url or "授权 Grok Build" in text or "Authorize Grok Build" in text:
            phase = "consent"
            if _click_exact(page, ["允许", "Allow", "Authorize", "Approve"], log, real=True):
                _sleep(2.5)
                continue
            try:
                page.run_js(
                    """
                    const f=document.querySelector('form');
                    if(!f) return;
                    let a=f.querySelector('input[name=action]');
                    if(!a){a=document.createElement('input');a.type='hidden';a.name='action';f.appendChild(a);}
                    a.value='allow';
                    const btn=[...f.querySelectorAll('button')].find(b=>((b.innerText||'').trim())==='允许'||(b.innerText||'').trim()==='Allow');
                    if(btn) btn.click(); else f.submit();
                    """
                )
                log("consent form submit via JS fallback")
                _sleep(2.5)
            except Exception as e:
                log(f"consent fallback failed: {e}")
            continue

        # Device code page
        if page.ele("css:input[name='user_code']", timeout=0.3) and "consent" not in url:
            phase = "device"
            if user_code:
                try:
                    uc = page.ele("css:input[name='user_code']")
                    cur = (uc.value or "") if uc else ""
                    if user_code.replace("-", "") not in cur.replace("-", ""):
                        uc.clear()
                        uc.input(user_code)
                        log("filled user_code")
                except Exception:
                    pass
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue
            try:
                el = page.ele("css:button[type='submit']", timeout=0.5)
                if el:
                    el.click(by_js=True)
                    log("clicked device submit")
                    _sleep(2.0)
                    continue
            except Exception:
                pass

        if "正在重定向" in text or ("/account" in url and "sign-in" not in url):
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue

        if "全部允许" in text or "隐私偏好" in text:
            _click_exact(page, ["全部允许", "全部拒绝"], log, real=False)
            _sleep(0.5)

        if "使用邮箱登录" in text or "Continue with email" in text:
            if _click_exact(
                page,
                ["使用邮箱登录", "Continue with email", "Sign in with email"],
                log,
                real=False,
            ):
                _sleep(1.5)
                phase = "email"
                continue

        if page.ele("css:input[type='email']", timeout=0.3) and not page.ele(
            "css:input[type='password']", timeout=0.2
        ):
            phase = "email"
            _fill(page, "css:input[type='email']", email, log, "email")
            if _click_exact(page, ["下一步", "Next", "Continue", "继续"], log, real=False):
                _sleep(1.8)
                continue

        if page.ele("css:input[type='password']", timeout=0.3):
            phase = "password"
            if login_attempts >= 5:
                _sleep(1.0)
                continue
            login_attempts += 1
            log(f"login attempt {login_attempts}")
            _fill(page, "css:input[type='email']", email, log, "email")
            wait_turnstile(page, log, 25)
            _fill(page, "css:input[type='password']", password, log, "password")
            wait_turnstile(page, log, 12)
            if not _click_exact(page, ["登录", "Sign in", "Log in"], log, real=True):
                try:
                    el = page.ele("css:button[type='submit']", timeout=0.5) or page.ele(
                        "css:button[data-testid='sign-in-submit']", timeout=0.5
                    )
                    if el:
                        el.click()
                        log("clicked login submit real")
                except Exception as e:
                    log(f"login submit fail: {e}")
            for _ in range(24):
                if stop_event is not None and stop_event.is_set():
                    return
                _sleep(0.5)
                if not page.ele("css:input[type='password']", timeout=0.2):
                    break
                if "sign-in" not in _page_url(page):
                    break
            continue

        _sleep(1.0)

    if stop_event is not None and stop_event.is_set():
        log("browser finished via stop_event")
        return
    log(f"browser loop ended phase={phase} login_attempts={login_attempts}")
