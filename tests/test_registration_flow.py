"""Offline state-machine tests for visible registration controls."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from xai_oauth_bulk.browser.flow import register_account


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


class _RegistrationPage:
    def __init__(self) -> None:
        self.state = "entry"
        self.url = "https://auth.example/device"
        self.email = _Input(input_type="email", name="email")
        self.code = _Input(name="code")
        self.password = _Input(input_type="password")
        self.name = _Input(name="name")

    def run_js(self, _: str) -> str:
        return {
            "entry": "",
            "choice": "Register",
            "email": "Create your account",
            "code": "Verify your email with a verification code",
            "profile": "Set your password",
            "done": "Welcome",
        }[self.state]

    def eles(self, selector: str):
        if selector == "tag:button":
            labels = {
                "entry": [("Continue", lambda: self._set("choice"))],
                "choice": [("Register", lambda: self._set("email"))],
                "email": [("Continue", lambda: self._set("code"))],
                "code": [("Verify", lambda: self._set("profile"))],
                "profile": [("Create account", lambda: self._set("done"))],
                "done": [],
            }
            return [_Button(label, action) for label, action in labels[self.state]]
        if selector == "tag:input":
            if self.state == "email":
                return [self.email]
            if self.state == "code":
                return [self.code]
            if self.state == "profile":
                return [self.name, self.password]
            return []
        if selector == "css:input[type='password']" and self.state == "profile":
            return [self.password]
        return []

    def ele(self, selector: str, timeout: float = 0.0):
        del timeout
        if self.state == "email" and selector in {
            "css:input[type='email']",
            "css:input[name='email']",
        }:
            return self.email
        if self.state == "code" and selector in {
            "css:input[autocomplete='one-time-code']",
            "css:input[name='code']",
            "css:input[name='verification_code']",
        }:
            return self.code
        if self.state == "profile":
            if selector == "css:input[type='password']":
                return self.password
            if selector in {
                "css:input[name='name']",
                "css:input[name='display_name']",
                "css:input[autocomplete='name']",
            }:
                return self.name
        return None

    def _set(self, state: str) -> None:
        self.state = state


class RegistrationFlowTests(unittest.TestCase):
    def test_registers_using_visible_controls_and_mailbox_code(self) -> None:
        page = _RegistrationPage()
        logs: list[str] = []
        with (
            patch("xai_oauth_bulk.browser.flow._sleep"),
            patch("xai_oauth_bulk.browser.flow.wait_turnstile", return_value=True),
        ):
            ok = register_account(
                page,
                email="owner@example.test",
                password="authorized-password",
                display_name="Account Owner",
                verification_code=lambda: "123456",
                timeout_sec=5,
                log=logs.append,
            )

        self.assertTrue(ok)
        self.assertEqual(page.state, "done")
        self.assertEqual(page.email.value, "owner@example.test")
        self.assertEqual(page.code.value, "123456")
        self.assertEqual(page.password.value, "authorized-password")
        self.assertEqual(page.name.value, "Account Owner")
        self.assertIn("received registration verification code", logs)

    def test_requires_a_mailbox_callback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "verification_code callback"):
            register_account(
                _RegistrationPage(),
                email="owner@example.test",
                password="authorized-password",
                verification_code=None,  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
