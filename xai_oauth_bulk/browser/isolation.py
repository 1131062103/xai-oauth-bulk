"""Fully isolated Chromium per account (unique profile + auto port)."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Callable

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class IsolatedBrowser:
    browser: Any
    page: Any
    profile_dir: str

    def close(self, log: LogFn | None = None) -> None:
        log = log or _noop
        try:
            self.browser.quit()
        except Exception as e:
            log(f"browser.quit: {e}")
        # Best-effort profile cleanup
        if self.profile_dir and os.path.isdir(self.profile_dir):
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                log(f"removed profile {self.profile_dir}")
            except Exception as e:
                log(f"profile cleanup: {e}")


def _default_chrome_candidates() -> list[str]:
    return [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]


def create_isolated_browser(
    *,
    headless: bool = False,
    proxy: str | None = None,
    chrome_path: str = "",
    log: LogFn | None = None,
) -> IsolatedBrowser:
    """Start a brand-new Chromium with unique user-data-dir and auto port."""
    log = log or _noop
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError as e:
        raise RuntimeError(
            "DrissionPage is required. Install with: pip install -r requirements.txt"
        ) from e

    opts = ChromiumOptions()
    profile_dir = os.path.join(
        tempfile.gettempdir(),
        "xai_oauth_bulk_chrome",
        f"{os.getpid()}_{uuid.uuid4().hex}",
    )
    os.makedirs(profile_dir, exist_ok=True)

    try:
        opts.set_user_data_path(profile_dir)
    except Exception:
        try:
            opts.set_argument(f"--user-data-dir={profile_dir}")
        except Exception:
            pass

    for flag in (
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--mute-audio",
        "--window-size=1280,900",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ):
        try:
            opts.set_argument(flag)
        except Exception:
            pass

    if headless:
        try:
            opts.headless(True)
        except Exception:
            try:
                opts.set_argument("--headless=new")
            except Exception:
                pass
        log("headless=True (Cloudflare may block)")
    else:
        try:
            opts.headless(False)
        except Exception:
            pass

    browser_bin = (chrome_path or "").strip()
    if not browser_bin:
        for cand in _default_chrome_candidates():
            if os.path.isfile(cand):
                browser_bin = cand
                break
    if browser_bin:
        try:
            opts.set_browser_path(browser_bin)
            log(f"browser path={browser_bin}")
        except Exception:
            pass

    if proxy:
        # Chromium expects host:port or scheme://host:port
        try:
            opts.set_argument(f"--proxy-server={proxy}")
            log(f"browser proxy={proxy}")
        except Exception as e:
            log(f"set proxy failed: {e}")

    # auto_port last so unique debug port is not clobbered
    try:
        opts.auto_port()
    except Exception:
        pass

    last_err: BaseException | None = None
    for attempt in range(1, 5):
        try:
            browser = Chromium(opts)
            page = browser.latest_tab
            if page is None:
                page = browser.new_tab()
            if page is None:
                raise RuntimeError("chromium started but page is None")
            log(f"isolated chromium started attempt={attempt} profile={profile_dir}")
            return IsolatedBrowser(browser=browser, page=page, profile_dir=profile_dir)
        except Exception as e:
            last_err = e
            log(f"chromium start failed attempt={attempt}: {e}")
            try:
                opts.auto_port()
            except Exception:
                pass

    raise RuntimeError(f"failed to start isolated chromium: {last_err}")
