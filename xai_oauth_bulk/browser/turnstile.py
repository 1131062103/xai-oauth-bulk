"""Cloudflare Turnstile wait helpers (no webdriver spoofing)."""

from __future__ import annotations

import time
from typing import Any, Callable

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def wait_turnstile(page: Any, log: LogFn | None = None, timeout: float = 45.0) -> bool:
    """Poll until cf-turnstile-response has a non-empty token.

    Attempts a normal click on the turnstile widget when present. Does not
    patch navigator.webdriver or inject anti-detection scripts.
    """
    log = log or _noop
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        try:
            el = page.ele("css:input[name='cf-turnstile-response']", timeout=0.3)
            if el is not None:
                v = (el.attr("value") or "").strip()
                if len(v) > 20:
                    log(f"turnstile ready len={len(v)}")
                    return True
        except Exception:
            pass

        # Best-effort: click visible turnstile checkbox via shadow root
        if not clicked:
            try:
                challenge = page.ele("@name=cf-turnstile-response", timeout=0.2)
                if challenge is not None:
                    wrapper = challenge.parent()
                    iframe = None
                    try:
                        iframe = wrapper.shadow_root.ele("tag:iframe")
                    except Exception:
                        iframe = None
                    if iframe is not None:
                        try:
                            body_sr = iframe.ele("tag:body").shadow_root
                            btn = body_sr.ele("tag:input")
                            if btn is not None:
                                btn.click()
                                clicked = True
                                log("clicked turnstile checkbox")
                        except Exception:
                            pass
            except Exception:
                pass

        time.sleep(0.9)
    log("turnstile not ready within timeout (continue anyway)")
    return False
