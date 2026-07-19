"""Offline tests for the xAI zh complete-registration profile form."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from xai_oauth_bulk.browser.flow import (
    _fill_profile_name_fields,
    _is_profile_screen,
    register_account,
)


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


class _ZhProfilePage:
    """Mirrors the headed screenshot: 完成注册 + 名/姓/密码 + Cloudflare + 完成注册."""

    def __init__(self) -> None:
        self.url = "https://accounts.x.ai/sign-up/complete"
        self.first = _Input(name="")
        self.last = _Input(name="")
        self.password = _Input(input_type="password", name="password")
        self.submitted = False

    def run_js(self, _: str) -> str:
        return "完成注册 名 姓 密码 请验证您是真人 Cloudflare 完成注册 返回"

    def eles(self, selector: str):
        if selector == "tag:input":
            return [self.first, self.last, self.password]
        if selector == "css:input[type='password']":
            return [self.password]
        if selector == "tag:button":
            return [
                _Button("完成注册", lambda: setattr(self, "submitted", True)),
                _Button("返回", lambda: None),
            ]
        return []

    def ele(self, selector: str, timeout: float = 0.0):
        del timeout
        if selector == "css:input[type='password']":
            return self.password
        if selector.startswith("xpath:") and "名" in selector and "姓" not in selector.replace("姓氏", ""):
            # first matching path for 名
            if "名" in selector and "姓" not in selector:
                return self.first
        if selector.startswith("xpath:") and ("姓" in selector or "Last" in selector):
            return self.last
        if selector.startswith("xpath:") and "密码" in selector:
            return self.password
        return None


class ProfileFormFillTests(unittest.TestCase):
    def test_detects_zh_complete_registration_screen(self) -> None:
        text = "完成注册\n名\n姓\n密码\n请验证您是真人\n完成注册"
        self.assertTrue(_is_profile_screen(None, text, password_present=False))
        self.assertTrue(_is_profile_screen(None, "other", password_present=True))

    def test_fills_zh_name_boxes_by_document_order(self) -> None:
        page = _ZhProfilePage()
        # Force path without name= attributes / xpath: only document order.
        page.ele = lambda selector, timeout=0.0: (  # type: ignore[method-assign]
            page.password if selector == "css:input[type='password']" else None
        )
        logs: list[str] = []
        first_ok, last_ok = _fill_profile_name_fields(
            page,
            first_name="Ada",
            last_name="Lovelace",
            display_name="Ada Lovelace",
            log=logs.append,
        )
        self.assertTrue(first_ok)
        self.assertTrue(last_ok)
        self.assertEqual(page.first.value, "Ada")
        self.assertEqual(page.last.value, "Lovelace")

    def test_register_account_fills_profile_after_code_step(self) -> None:
        class _Flow:
            def __init__(self) -> None:
                self.state = "profile"
                self.url = "https://accounts.x.ai/sign-up/complete"
                self.first = _Input()
                self.last = _Input()
                self.password = _Input(input_type="password")
                self.done = False

            def run_js(self, _: str) -> str:
                if self.done:
                    return "Welcome authorize continue to"
                return "完成注册 名 姓 密码 请验证您是真人 完成注册"

            def _finish(self) -> None:
                self.done = True
                self.url = "https://accounts.x.ai/welcome"

            def eles(self, selector: str):
                if self.done:
                    return []
                if selector == "tag:input":
                    return [self.first, self.last, self.password]
                if selector == "css:input[type='password']":
                    return [self.password]
                if selector == "tag:button":
                    return [_Button("完成注册", self._finish)]
                return []

            def ele(self, selector: str, timeout: float = 0.0):
                del timeout
                if selector == "css:input[type='password']":
                    return self.password
                return None

        page = _Flow()
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
                display_name="Ada Lovelace",
                verification_code=lambda: "HF5BI4",
                timeout_sec=5,
                log=logs.append,
            )
        self.assertTrue(ok)
        self.assertEqual(page.first.value, "Ada")
        self.assertEqual(page.last.value, "Lovelace")
        self.assertEqual(page.password.value, "authorized-password-1")
        self.assertIn("on registration profile form", " ".join(logs))


if __name__ == "__main__":
    unittest.main()
