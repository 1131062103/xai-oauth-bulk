"""Offline tests for multi-box / dashed verification-code filling."""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from xai_oauth_bulk.browser.flow import (
    BrowserFlowError,
    _fill_verification_code,
    _verification_input_elements,
)


class _OtpInput:
    def __init__(
        self,
        *,
        maxlength: str | None = None,
        name: str = "",
        autocomplete: str = "",
        input_type: str = "text",
        on_input=None,
        accept_input: bool = True,
    ) -> None:
        self.value = ""
        self._attrs = {
            "maxlength": maxlength or "",
            "name": name,
            "autocomplete": autocomplete,
            "type": input_type,
        }
        self._on_input = on_input
        self._accept_input = accept_input
        self.clear_calls = 0
        self.input_calls: list[str] = []
        self.js_calls: list[str] = []

    def attr(self, name: str) -> str:
        return self._attrs.get(name, "")

    def clear(self) -> None:
        self.clear_calls += 1
        self.value = ""

    def input(self, value: str, clear: bool = False) -> None:
        if clear:
            self.value = ""
        text = str(value)
        self.input_calls.append(text)
        if not self._accept_input:
            return
        maxlength = self._attrs.get("maxlength") or ""
        if maxlength.isdigit():
            room = max(0, int(maxlength) - len(self.value))
            text = text[:room]
        self.value += text
        if self._on_input is not None:
            self._on_input(self, text)

    def run_js(self, script: str, *args: object) -> str:
        # Mimic native value setter used by _set_input_value_js.
        if args:
            value = str(args[0])
        else:
            match = re.search(r"const value = '([^']*)'", script)
            value = match.group(1) if match else ""
        self.js_calls.append(value)
        self.value = value
        if self._on_input is not None:
            self._on_input(self, value)
        return value

    def click(self) -> None:
        return None

    def focus(self) -> None:
        return None


class _OtpPage:
    def __init__(self, inputs: list[_OtpInput]) -> None:
        self._inputs = inputs

    def eles(self, selector: str):
        if selector == "tag:input":
            return list(self._inputs)
        return []

    def ele(self, selector: str, timeout: float = 0.0):
        del timeout
        mapping = {
            "css:input[autocomplete='one-time-code']": "autocomplete:one-time-code",
            "css:input[name='code']": "name:code",
            "css:input[name='verification_code']": "name:verification_code",
            "css:input[name='otp']": "name:otp",
        }
        key = mapping.get(selector)
        if not key:
            return None
        kind, value = key.split(":", 1)
        for item in self._inputs:
            if item.attr(kind) == value:
                return item
        return None


class VerificationCodeFillTests(unittest.TestCase):
    def test_single_field_accepts_dashed_xai_code(self) -> None:
        field = _OtpInput(name="code", autocomplete="one-time-code")
        page = _OtpPage([field])
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "HF5-BI4", logs.append)
        self.assertTrue(ok)
        self.assertEqual(field.value, "HF5BI4")
        self.assertIn("1 input field", logs[-1])

    def test_six_maxlength_boxes_fill_all_characters(self) -> None:
        boxes = [_OtpInput(maxlength="1", name="code") for _ in range(6)]
        page = _OtpPage(boxes)
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "HF5-BI4", logs.append)
        self.assertTrue(ok)
        self.assertEqual("".join(box.value for box in boxes), "HF5BI4")

    def test_six_boxes_without_clearing_siblings(self) -> None:
        """Regression: clear() on one box must not be required (wipes siblings)."""

        def wipe_all_on_clear(box: _OtpInput, _text: str) -> None:
            return None

        boxes: list[_OtpInput] = []

        def make_box() -> _OtpInput:
            box = _OtpInput(maxlength="1")

            def clear() -> None:
                box.clear_calls += 1
                # Simulate buggy OTP widgets where select-all clears the group.
                for sibling in boxes:
                    sibling.value = ""

            box.clear = clear  # type: ignore[method-assign]
            box._on_input = wipe_all_on_clear
            return box

        boxes = [make_box() for _ in range(6)]
        page = _OtpPage(boxes)
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "HF5-BI4", logs.append)
        self.assertTrue(ok)
        self.assertEqual("".join(box.value for box in boxes), "HF5BI4")
        self.assertEqual(sum(box.clear_calls for box in boxes), 0)

    def test_progressive_three_plus_three_groups(self) -> None:
        first = [_OtpInput(maxlength="1") for _ in range(3)]
        second = [_OtpInput(maxlength="1") for _ in range(3)]
        visible = list(first)

        class _ProgressivePage(_OtpPage):
            def eles(self, selector: str):
                if selector == "tag:input":
                    return list(visible)
                return []

        def advance(box: _OtpInput, text: str) -> None:
            del box
            if text and all(item.value for item in first) and second[0] not in visible:
                visible.extend(second)

        for box in first:
            box._on_input = advance

        page = _ProgressivePage(visible)
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "hf5-bi4", logs.append)
        self.assertTrue(ok)
        self.assertEqual("".join(box.value for box in first + second), "HF5BI4")

    def test_rejects_non_six_character_codes(self) -> None:
        page = _OtpPage([_OtpInput(name="code")])
        with self.assertRaises(BrowserFlowError):
            _fill_verification_code(page, "HF5-BI", lambda _: None)

    def test_detection_prefers_maxlength_boxes_over_named_field(self) -> None:
        boxes = [_OtpInput(maxlength="1", name="code") for _ in range(6)]
        # A hidden/shared named field should not win over the six boxes.
        shared = _OtpInput(name="code", autocomplete="one-time-code")
        page = _OtpPage(boxes + [shared])
        found = _verification_input_elements(page)
        self.assertEqual(len(found), 6)
        self.assertTrue(all(item is box for item, box in zip(found, boxes)))

    def test_empty_dom_value_is_not_treated_as_success(self) -> None:
        """Regression: never claim success when the field stays empty."""
        field = _OtpInput(name="code", autocomplete="one-time-code", accept_input=False)

        def refuse_js(self_script: str, *args: object) -> str:
            del self_script, args
            return ""

        # Override JS setter so both CDP and JS paths leave value empty.
        field.run_js = refuse_js  # type: ignore[method-assign]
        page = _OtpPage([field])
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "HF5-BI4", logs.append)
        self.assertFalse(ok)
        self.assertEqual(field.value, "")
        self.assertTrue(any("filled 0/6" in line for line in logs))

    def test_js_setter_recovers_when_keyboard_insert_is_ignored(self) -> None:
        field = _OtpInput(name="code", autocomplete="one-time-code", accept_input=False)
        page = _OtpPage([field])
        logs: list[str] = []
        with patch("xai_oauth_bulk.browser.flow._sleep"):
            ok = _fill_verification_code(page, "HF5-BI4", logs.append)
        self.assertTrue(ok)
        self.assertEqual(field.value, "HF5BI4")
        self.assertTrue(any("via js" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
