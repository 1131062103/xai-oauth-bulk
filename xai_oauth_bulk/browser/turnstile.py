"""Cloudflare Turnstile wait helpers (ordinary clicks only; no webdriver spoofing)."""

from __future__ import annotations

import time
from typing import Any, Callable

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _token_ready(page: Any) -> int:
    """Return token length when a non-empty Turnstile response is present."""
    selectors = (
        "css:input[name='cf-turnstile-response']",
        "@name=cf-turnstile-response",
        "css:input[name='cf-turnstile-response'][type='hidden']",
    )
    for selector in selectors:
        try:
            el = page.ele(selector, timeout=0.2)
            if el is None:
                continue
            value = (el.attr("value") or getattr(el, "value", None) or "").strip()
            if len(value) > 20:
                return len(value)
        except Exception:
            continue
    return 0


def _try_click_turnstile(page: Any, log: LogFn) -> bool:
    """Best-effort ordinary click on a visible Turnstile checkbox/widget."""
    # Path 1: shadow-root checkbox behind cf-turnstile-response.
    try:
        challenge = page.ele("@name=cf-turnstile-response", timeout=0.2) or page.ele(
            "css:input[name='cf-turnstile-response']", timeout=0.2
        )
        if challenge is not None:
            wrapper = challenge.parent()
            iframe = None
            try:
                iframe = wrapper.shadow_root.ele("tag:iframe")
            except Exception:
                iframe = None
            if iframe is None:
                try:
                    iframe = wrapper.ele("tag:iframe", timeout=0.2)
                except Exception:
                    iframe = None
            if iframe is not None:
                try:
                    body = iframe.ele("tag:body", timeout=0.5)
                    body_sr = getattr(body, "shadow_root", None)
                    btn = body_sr.ele("tag:input") if body_sr is not None else None
                    if btn is None and body is not None:
                        btn = body.ele("css:input[type='checkbox']", timeout=0.2) or body.ele(
                            "tag:input", timeout=0.2
                        )
                    if btn is not None:
                        btn.click()
                        log("clicked turnstile checkbox")
                        return True
                except Exception:
                    pass
                try:
                    iframe.click()
                    log("clicked turnstile iframe")
                    return True
                except Exception:
                    pass
    except Exception:
        pass

    # Path 2: public Turnstile container / iframe locators.
    for selector in (
        "css:.cf-turnstile",
        "css:div[class*='turnstile']",
        "css:iframe[src*='challenges.cloudflare.com']",
        "css:iframe[src*='turnstile']",
    ):
        try:
            widget = page.ele(selector, timeout=0.25)
            if widget is None:
                continue
            try:
                widget.scroll.to_see()
            except Exception:
                pass
            widget.click()
            log(f"clicked turnstile widget via {selector}")
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def wait_turnstile(page: Any, log: LogFn | None = None, timeout: float = 45.0) -> bool:
    """Poll until cf-turnstile-response has a non-empty token.

    Attempts a normal click on the turnstile widget when present. Does not
    patch navigator.webdriver or inject anti-detection scripts. In headed mode
    the operator may still need to complete the checkbox manually.
    """
    log = log or _noop
    deadline = time.time() + timeout
    clicked = False
    reminded = False
    while time.time() < deadline:
        length = _token_ready(page)
        if length > 20:
            log(f"turnstile ready len={length}")
            return True

        if not clicked:
            clicked = _try_click_turnstile(page, log)

        remaining = deadline - time.time()
        if not reminded and remaining < timeout * 0.5:
            log("turnstile still pending — complete the Cloudflare checkbox in the headed browser if visible")
            reminded = True

        time.sleep(0.9)
    log("turnstile not ready within timeout (continue anyway)")
    return False
