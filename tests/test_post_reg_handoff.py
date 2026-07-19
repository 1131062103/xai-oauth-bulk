"""Offline tests for Continue → Allow after registration profile submit."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from xai_oauth_bulk.browser.flow import approve_device_code, register_account


class _Scroll:
    def to_see(self) -> None:
        return None


class _Input:
    def __init__(self, input_type: str = "text", name: str = "") -> None:
        self.value = ""
        self.scroll = _Scroll()
        self._type = input_type
        self._name = name

    def attr(self, key: str) -> str:
        if key == "type":
            return self._type
        if key == "name":
            return self._name
        return ""

    def clear(self) -> None:
        self.value = ""

    def click(self, **_: object) -> None:
        return None

    def input(self, value: str, clear: bool = False, by_js: bool = False) -> None:
        del by_js
        if clear:
            self.value = ""
        self.value = str(value)

    def run_js(self, script: str, *args: object) -> str:
        del script
        if args:
            self.value = str(args[0])
        return self.value


class _Button:
    def __init__(self, label: str, on_click) -> None:
        self.text = label
        self.scroll = _Scroll()
        self._on_click = on_click

    def click(self, **_: object) -> None:
        self._on_click()


class _HandoffPage:
    """profile → continue → allow → device/done."""

    def __init__(self) -> None:
        self.state = "profile"
        self.url = "https://accounts.x.ai/sign-up/complete"
        self.first = _Input()
        self.last = _Input()
        self.password = _Input(input_type="password")
        self.loaded = False

    def get(self, url: str, timeout: float = 0.0) -> None:
        del timeout
        self.loaded = True
        self.url = url
        self.state = "device"

    def run_js(self, script: str = "", *args: object) -> str:
        del script, args
        return {
            "profile": "完成注册 名 姓 密码 请验证您是真人 完成注册",
            "continue": "正在重定向 继续",
            "consent": "授权 Grok Build 允许",
            "done": "设备已授权 device authorized",
        }[self.state]

    def eles(self, selector: str):
        if self.state == "profile" and selector == "tag:input":
            return [self.first, self.last, self.password]
        if self.state == "profile" and selector == "css:input[type='password']":
            return [self.password]
        if selector == "tag:button":
            labels = {
                "profile": [("完成注册", lambda: self._set("continue"))],
                "continue": [("继续", lambda: self._set("consent"))],
                "consent": [("允许", lambda: self._set("done"))],
                "done": [],
            }
            return [_Button(label, action) for label, action in labels[self.state]]
        return []

    def ele(self, selector: str, timeout: float = 0.0):
        del timeout
        if self.state == "profile" and selector == "css:input[type='password']":
            return self.password
        return None

    def _set(self, state: str) -> None:
        self.state = state
        self.url = {
            "continue": "https://accounts.x.ai/account",
            "consent": "https://accounts.x.ai/oauth/consent",
            "done": "https://accounts.x.ai/device/done",
            "profile": self.url,
            "device": "https://accounts.x.ai/device",
        }.get(state, self.url)


class PostRegHandoffTests(unittest.TestCase):
    def test_register_account_clicks_continue_then_allow(self) -> None:
        page = _HandoffPage()
        logs: list[str] = []
        with (
            patch("xai_oauth_bulk.browser.flow._sleep"),
            patch("xai_oauth_bulk.browser.flow.wait_turnstile", return_value=True),
        ):
            ok = register_account(
                page,
                email="owner@example.test",
                password="authorized-password-1",
                first_name="Ada",
                last_name="Lovelace",
                verification_code=lambda: "HF5BI4",
                timeout_sec=5,
                log=logs.append,
            )
        self.assertTrue(ok)
        self.assertEqual(page.state, "done")
        self.assertEqual(page.first.value, "Ada")
        self.assertEqual(page.last.value, "Lovelace")
        self.assertEqual(page.password.value, "authorized-password-1")
        joined = " ".join(logs)
        self.assertIn("submitted registration profile", joined)
        self.assertIn("clicked Continue after registration", joined)
        self.assertIn("clicked Allow after registration", joined)

    def test_approve_without_reopen_resumes_continue_allow(self) -> None:
        page = _HandoffPage()
        page.state = "continue"
        page.url = "https://accounts.x.ai/account"
        stop = threading.Event()
        logs: list[str] = []

        def _finish_when_done(msg: str) -> None:
            logs.append(msg)
            if page.state == "done":
                stop.set()

        with patch("xai_oauth_bulk.browser.flow._sleep"):
            approve_device_code(
                page,
                verification_uri_complete="https://accounts.x.ai/device?user_code=ABC-123",
                email="owner@example.test",
                password="authorized-password-1",
                user_code="ABC-123",
                timeout_sec=5,
                stop_event=stop,
                log=_finish_when_done,
                reopen=False,
            )
        self.assertFalse(page.loaded)
        self.assertEqual(page.state, "done")
        self.assertIn("resuming device authorization", " ".join(logs))


if __name__ == "__main__":
    unittest.main()
