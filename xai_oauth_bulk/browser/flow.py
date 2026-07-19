"""xAI browser RPA: registration + device-code OAuth approval.

Flow overview (registration path)::

    entry → choose sign-up → email → verification code
         → profile (名/姓/密码 + Turnstile) → Continue → Allow
         → device authorized (token poll is external)

Public entry points:
- ``register_account`` — create an authorized account and leave the session
  ready for OAuth handoff.
- ``approve_device_code`` — complete device-code login / consent. After
  registration call with ``reopen=False`` so the current session is kept.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from .turnstile import wait_turnstile

# ---------------------------------------------------------------------------
# Types and errors
# ---------------------------------------------------------------------------

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


class BrowserFlowError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Page / timing helpers
# ---------------------------------------------------------------------------

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


def _element_value(element: Any) -> str:
    try:
        return str(element.value or "") if hasattr(element, "value") else ""
    except Exception:
        return ""


def _safe_attr(element: Any, name: str) -> str:
    try:
        return str(element.attr(name) or "")
    except BaseException:
        return ""


def _has_any_text(text: str, fragments: tuple[str, ...]) -> bool:
    normalized = _norm(text).lower()
    return any(fragment.lower() in normalized for fragment in fragments)


# ---------------------------------------------------------------------------
# Click helpers
# ---------------------------------------------------------------------------

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


def _click_visible(page: Any, labels: list[str], log: LogFn) -> str | None:
    """Click an explicitly labelled visible control using normal input only."""
    for label in labels:
        el = _find_button_exact(page, label)
        if not el:
            continue
        try:
            try:
                el.scroll.to_see()
            except Exception:
                pass
            el.click()
            log(f"clicked visible exact {label!r}")
            return label
        except Exception as e:
            log(f"visible click {label!r} failed: {e}")
    return None


def _click_link_containing(page: Any, href_fragment: str, log: LogFn) -> bool:
    """Click a visible anchor whose href contains the expected registration path."""
    try:
        links = page.eles("tag:a") or []
    except Exception:
        links = []
    for link in links:
        try:
            href = str(link.attr("href") or "")
            if href_fragment not in href:
                continue
            try:
                link.scroll.to_see()
            except Exception:
                pass
            link.click()
            log("opened registration link")
            return True
        except BaseException:
            continue
    try:
        link = page.ele(f"css:a[href*='{href_fragment}']", timeout=0.3)
        if link is not None:
            link.click()
            log("opened registration link")
            return True
    except Exception:
        pass
    return False


def _click_continue(page: Any, log: LogFn, *, real: bool = True) -> bool:
    """Click a post-login / redirect Continue control (zh or en)."""
    labels = ["继续", "Continue", "Continue to authorize", "继续授权", "下一步", "Next"]
    if real and _click_visible(page, labels, log):
        return True
    return bool(_click_exact(page, labels, log, real=real))


def _click_allow(page: Any, log: LogFn) -> bool:
    """Click the OAuth consent Allow control with real click first."""
    labels = ["允许", "Allow", "Authorize", "Approve", "同意", "Grant access", "授权"]
    if _click_visible(page, labels, log):
        return True
    if _click_exact(page, labels, log, real=True):
        return True
    try:
        page.run_js(
            """
            const labels = ['允许','Allow','Authorize','Approve','同意','Grant access','授权'];
            const btn = [...document.querySelectorAll('button')].find(b => {
              const t = (b.innerText || b.textContent || '').trim();
              return labels.includes(t);
            });
            if (btn) { btn.click(); return true; }
            const f = document.querySelector('form');
            if (!f) return false;
            let a = f.querySelector('input[name=action]');
            if (!a) {
              a = document.createElement('input');
              a.type = 'hidden';
              a.name = 'action';
              f.appendChild(a);
            }
            a.value = 'allow';
            f.submit();
            return true;
            """
        )
        log("consent form submit via JS fallback")
        return True
    except Exception as e:
        log(f"consent fallback failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Input fill helpers
# ---------------------------------------------------------------------------

def _set_input_value_js(element: Any, value: str) -> bool:
    """Set a controlled React/Vue input via the native value setter + input events."""
    try:
        element.run_js(
            """
            const el = this;
            const value = arguments[0];
            const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
            const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) {
                desc.set.call(el, value);
            } else {
                el.value = value;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
            return el.value;
            """,
            value,
        )
        return True
    except TypeError:
        # Some DrissionPage builds only accept the script body without extra args.
        try:
            safe = value.replace("\\", "\\\\").replace("'", "\\'")
            element.run_js(
                f"""
                const el = this;
                const value = '{safe}';
                const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
                const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) {{
                    desc.set.call(el, value);
                }} else {{
                    el.value = value;
                }}
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return el.value;
                """
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


def _fill_element(el: Any, value: str, log: LogFn, label: str) -> bool:
    try:
        cur = _element_value(el)
        if cur == value:
            return True
        try:
            el.scroll.to_see()
        except Exception:
            pass
        try:
            el.click()
        except Exception:
            pass
        try:
            el.clear()
        except Exception:
            pass
        # Prefer real keyboard/CDP insert first (Turnstile-friendly), then JS setter.
        try:
            el.input(value, clear=True)
        except TypeError:
            try:
                el.input(value)
            except Exception:
                pass
        except Exception:
            pass
        cur = _element_value(el)
        if cur == value:
            log(f"filled {label}")
            return True
        # DrissionPage by_js path sets property + change event.
        try:
            el.input(value, clear=True, by_js=True)
        except TypeError:
            try:
                el.input(value, by_js=True)
            except Exception:
                pass
        except Exception:
            pass
        cur = _element_value(el)
        if cur == value:
            log(f"filled {label} via js input")
            return True
        try:
            el.set.value(value)  # type: ignore[attr-defined]
        except Exception:
            try:
                el.set.property("value", value)  # type: ignore[attr-defined]
            except Exception:
                pass
        cur = _element_value(el)
        if cur == value:
            log(f"filled {label} via set.value")
            return True
        if _set_input_value_js(el, value):
            cur = _element_value(el)
            if cur == value:
                log(f"filled {label} via js value setter")
                return True
        # Final plain input attempt without clear.
        try:
            el.input(value)
        except Exception:
            pass
        cur = _element_value(el)
        if cur == value:
            log(f"filled {label}")
            return True
        log(f"fill {label} incomplete (got {len(cur)} chars, want {len(value)})")
        return False
    except Exception as e:
        log(f"fill {label} failed: {e}")
        return False


def _fill(page: Any, selector: str, value: str, log: LogFn, label: str) -> None:
    try:
        el = page.ele(selector, timeout=1.0)
    except Exception as e:
        log(f"fill {label} failed: {e}")
        return
    if el is not None:
        _fill_element(el, value, log, label)


def _fill_first(page: Any, selectors: tuple[str, ...], value: str, log: LogFn, label: str) -> bool:
    for selector in selectors:
        try:
            element = page.ele(selector, timeout=0.25)
        except BaseException:
            continue
        if element is not None and _fill_element(element, value, log, label):
            return True
    return False


def _fill_all_password_fields(page: Any, password: str, log: LogFn) -> int:
    """Fill every visible password input (password + confirm password)."""
    try:
        fields = page.eles("css:input[type='password']") or []
    except Exception:
        fields = []
    if not fields:
        try:
            one = page.ele("css:input[type='password']", timeout=0.3)
        except Exception:
            one = None
        fields = [one] if one is not None else []
    filled = 0
    for index, field in enumerate(fields):
        label = "registration password" if index == 0 else f"registration password confirm #{index}"
        if _fill_element(field, password, log, label):
            filled += 1
    return filled


def _fill_by_placeholder_or_aria(
    page: Any,
    value: str,
    log: LogFn,
    label: str,
    needles: tuple[str, ...],
) -> bool:
    """Match text/placeholder/aria-label for locales that omit name= attributes."""
    try:
        inputs = page.eles("tag:input") or []
    except Exception:
        inputs = []
    lowered = tuple(n.lower() for n in needles)
    for element in inputs:
        try:
            input_type = str(element.attr("type") or "text").lower()
            if input_type in {"hidden", "checkbox", "radio", "submit", "button", "password", "email", "file"}:
                continue
            hay = " ".join(
                str(element.attr(attr) or "")
                for attr in ("placeholder", "aria-label", "name", "id", "autocomplete")
            ).lower()
            if not any(needle in hay for needle in lowered):
                continue
            if _fill_element(element, value, log, label):
                return True
        except BaseException:
            continue
    return False


def _visible_text_inputs(page: Any) -> list[Any]:
    """Return non-hidden text-like inputs (excludes password/email/submit)."""
    try:
        inputs = page.eles("tag:input") or []
    except Exception:
        inputs = []
    result: list[Any] = []
    for element in inputs:
        try:
            input_type = str(element.attr("type") or "text").lower()
            if input_type in {
                "hidden",
                "checkbox",
                "radio",
                "submit",
                "button",
                "password",
                "email",
                "file",
                "image",
                "reset",
            }:
                continue
            result.append(element)
        except BaseException:
            continue
    return result


def _fill_by_label_text(
    page: Any,
    value: str,
    log: LogFn,
    label: str,
    label_texts: tuple[str, ...],
) -> bool:
    """Fill an input associated with an external label (xAI zh: 名/姓/密码)."""
    for text in label_texts:
        if not text:
            continue
        # Prefer exact label text; xAI uses bare 名/姓 next to empty inputs.
        xpaths = (
            f"xpath://label[normalize-space(.)='{text}']/following::input[1]",
            f"xpath://label[normalize-space(.)='{text}']/../input",
            f"xpath://label[normalize-space(.)='{text}']//input",
            f"xpath://*[self::label or self::span or self::div or self::p]"
            f"[normalize-space(.)='{text}']/following::input[1]",
            f"xpath://input[@aria-label='{text}']",
            f"xpath://input[@placeholder='{text}']",
        )
        for xpath in xpaths:
            try:
                element = page.ele(xpath, timeout=0.25)
            except BaseException:
                continue
            if element is None:
                continue
            try:
                input_type = str(element.attr("type") or "text").lower()
                if input_type in {"hidden", "checkbox", "radio", "submit", "button"}:
                    continue
            except BaseException:
                pass
            if _fill_element(element, value, log, label):
                return True
    return False


# ---------------------------------------------------------------------------
# Screen detection
# ---------------------------------------------------------------------------

def _is_profile_screen(page: Any, text: str, password_present: bool) -> bool:
    """Detect the post-OTP profile form (名/姓/密码 + Turnstile / Complete registration)."""
    if password_present:
        return True
    return _has_any_text(
        text,
        (
            "set your password",
            "create a password",
            "choose a password",
            "complete registration",
            "complete your account",
            "finish signing up",
            "first name",
            "last name",
            "your name",
            "display name",
            "完成注册",
            "创建密码",
            "设置密码",
            "密码",
            "名字",
            "姓氏",
            # Bare zh labels on xAI complete-registration form.
            "名",
            "姓",
            "请验证您是真人",
        ),
    )


def _device_authorized(url: str, text: str) -> bool:
    low = (text or "").lower()
    return (
        "device/done" in (url or "")
        or "设备已授权" in (text or "")
        or "device authorized" in low
        or "you can close this window" in low
        or "可以关闭此窗口" in (text or "")
        or "authorization complete" in low
        or "授权完成" in (text or "")
    )


def _looks_like_consent_screen(url: str, text: str) -> bool:
    t = text or ""
    low = t.lower()
    if "/consent" in (url or ""):
        return True
    if "Authorize Grok" in t or "授权 Grok" in t:
        return True
    if "wants to access" in low or "希望访问" in t or "请求访问" in t or "grant access" in low:
        return True
    if ("authorize" in low or "授权" in t) and any(
        key in low or key in t for key in ("grok", "build", "cli", "device", "应用", "client", "oauth")
    ):
        return True
    return False


def _registration_complete(url: str, text: str) -> bool:
    """True when the profile form is gone and the OAuth handoff UI is showing."""
    if _device_authorized(url, text):
        return True
    if _is_profile_screen(None, text, password_present=False) and _has_any_text(
        text, ("完成注册", "set your password", "create a password", "密码")
    ):
        # Still on the name/password form — not done.
        # Note: bare "名"/"姓" alone should not block if we already left the form;
        # password presence is the stronger signal and is checked by the caller.
        if _has_any_text(text, ("完成注册", "请验证您是真人", "set your password", "create a password")):
            return False
    still_registering = _has_any_text(
        text,
        (
            "create your account",
            "create account",
            "verify your email",
            "enter verification code",
            "set your password",
            "完成注册",
            "确认邮箱",
            "验证码",
        ),
    )
    if still_registering and "sign-up" in (url or "").lower():
        return False
    if still_registering and "signup" in (url or "").lower():
        return False
    return _device_authorized(url, text) or _looks_like_consent_screen(url, text) or _has_any_text(
        text,
        (
            "account created",
            "welcome",
            "authorize",
            "consent",
            "continue to",
            "正在重定向",
            "设备已授权",
            "授权完成",
        ),
    )


# ---------------------------------------------------------------------------
# Verification code (OTP)
# ---------------------------------------------------------------------------

def _is_otp_like_input(element: Any) -> bool:
    """True for editable inputs that can hold verification-code characters."""
    input_type = _safe_attr(element, "type").lower() or "text"
    if input_type in {"hidden", "checkbox", "radio", "submit", "button", "email", "password", "file"}:
        return False
    # Keep text/tel/number/search and empty-type inputs; drop obvious non-OTP widgets.
    if input_type not in {"", "text", "tel", "number", "one-time-code", "search"}:
        return False
    return True


def _looks_like_otp_field(element: Any) -> bool:
    """Heuristic for a dedicated verification/OTP control."""
    autocomplete = _safe_attr(element, "autocomplete").lower()
    name = _safe_attr(element, "name").lower()
    input_mode = _safe_attr(element, "inputmode").lower()
    maxlength = _safe_attr(element, "maxlength")
    if autocomplete in {"one-time-code", "otp"}:
        return True
    if name in {"code", "verification_code", "otp", "token", "user_code"}:
        return True
    if input_mode in {"numeric", "text"} and maxlength in {"1", "6", "7"}:
        return True
    if maxlength == "1":
        return True
    return False


def _verification_input_elements(page: Any) -> list[Any]:
    """Return one field or the ordered boxes of a verification-code control."""
    try:
        inputs = page.eles("tag:input") or []
    except Exception:
        inputs = []

    # Prefer genuine maxlength=1 OTP boxes when a full set is present. A six-box
    # control commonly shares a name with other selectors, so scan all inputs
    # before trusting only the first CSS match.
    segmented: list[Any] = []
    otp_named: list[Any] = []
    generic_text_inputs: list[Any] = []
    for element in inputs:
        try:
            if not _is_otp_like_input(element):
                continue
            generic_text_inputs.append(element)
            maxlength = _safe_attr(element, "maxlength")
            if maxlength == "1":
                segmented.append(element)
            if _looks_like_otp_field(element):
                otp_named.append(element)
        except BaseException:
            continue

    if len(segmented) >= 6:
        return segmented[:6]
    # xAI-style codes are XXX-XXX; some UIs expose the first trio first.
    if len(segmented) >= 2:
        return segmented
    if len(otp_named) == 1:
        return otp_named[:1]
    if 2 <= len(otp_named) <= 6:
        return otp_named
    # Some OTP components render six normal text inputs without maxlength.
    if len(generic_text_inputs) >= 6:
        return generic_text_inputs[:6]

    for selector in (
        "css:input[autocomplete='one-time-code']",
        "css:input[name='code']",
        "css:input[name='verification_code']",
        "css:input[name='otp']",
        "css:input[inputmode='numeric']",
        "css:input[inputmode='text']",
    ):
        try:
            element = page.ele(selector, timeout=0.2)
        except BaseException:
            continue
        if element is not None and _is_otp_like_input(element):
            return [element]
    return segmented or otp_named


def _code_chars_present(fields: list[Any], normalized: str) -> bool:
    joined = re.sub(r"[^A-Za-z0-9]", "", "".join(_element_value(field) for field in fields))
    return joined.upper() == normalized.upper() and len(joined) == len(normalized)


def _read_code_chars(fields: list[Any]) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", "".join(_element_value(field) for field in fields)).upper()


def _focus_element(element: Any) -> None:
    try:
        element.click()
        return
    except BaseException:
        pass
    try:
        if hasattr(element, "focus"):
            element.focus()
    except BaseException:
        pass


def _type_into(element: Any, value: str, *, clear: bool = False, allow_js: bool = True) -> bool:
    """Type into an element; avoid destructive clear on multi-box OTP widgets.

    Returns True only when the call did not raise. Callers must still verify
    ``_element_value`` when the UI exposes the value in the DOM.
    """
    try:
        if clear:
            try:
                element.clear()
            except BaseException:
                pass
            try:
                element.input(value, clear=True)
                return True
            except TypeError:
                pass
            except BaseException:
                pass
        _focus_element(element)
        try:
            element.input(value)
            return True
        except BaseException:
            if allow_js and _set_input_value_js(element, (_element_value(element) + value) if not clear else value):
                return True
            return False
    except BaseException:
        if allow_js and _set_input_value_js(element, value if clear else (_element_value(element) + value)):
            return True
        return False


def _fill_verification_code(page: Any, code: str, log: LogFn) -> bool:
    """Fill a one-field or multi-box verification control with six code characters.

    xAI codes look like ``HF5-BI4``. The UI may be:
    - one input (with or without a literal hyphen)
    - six single-character boxes
    - two groups of three boxes revealed progressively

    Success requires the DOM to reflect all six alphanumeric characters (hyphen
    optional). Empty-value false positives are intentionally rejected.
    """
    normalized = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if len(normalized) != 6:
        raise BrowserFlowError("verification code must contain exactly six letters or digits")
    dashed = f"{normalized[:3]}-{normalized[3:]}"

    fields = _verification_input_elements(page)
    if not fields:
        log("verification code received, but no verification input is visible yet")
        return False

    def _success(fields_now: list[Any], how: str) -> bool:
        if _code_chars_present(fields_now, normalized):
            log(how)
            return True
        return False

    # Strategy A: single visible field — CDP/keyboard first, then native JS setter.
    if len(fields) == 1:
        field = fields[0]
        for candidate in (normalized, dashed, str(code or "").strip()):
            if not candidate:
                continue
            _type_into(field, candidate, clear=True, allow_js=False)
            _sleep(0.1)
            if _success([field], "filled verification code into 1 input field"):
                return True
            # React controlled inputs often ignore insertText; use native setter.
            if _set_input_value_js(field, candidate):
                _sleep(0.1)
                if _success([field], "filled verification code into 1 input field via js"):
                    return True
        # Character-by-character fallback (some masked OTP inputs).
        try:
            field.clear()
        except BaseException:
            pass
        _focus_element(field)
        for character in normalized:
            _type_into(field, character, clear=False, allow_js=True)
            _sleep(0.04)
        if _success([field], "filled verification code into 1 input field"):
            return True
        # If the widget still looks single-field but value is empty, rescan in case
        # the page upgraded to multi-box after focus.
        rescanned = _verification_input_elements(page)
        if len(rescanned) > 1:
            fields = rescanned
        else:
            got = _read_code_chars([field])
            log(
                f"verification code received, filled {len(got)}/6 "
                f"characters into the single input"
            )
            return False

    # Strategy B: multi-box bulk insert into the first box (auto-distribute).
    first = fields[0]
    _type_into(first, normalized, clear=False, allow_js=False)
    _sleep(0.15)
    visible = _verification_input_elements(page) or fields
    if _success(visible, f"filled verification code into {len(visible)} input field(s) via first-box bulk input"):
        return True
    if _set_input_value_js(first, normalized):
        _sleep(0.1)
        visible = _verification_input_elements(page) or fields
        if _success(visible, f"filled verification code into {len(visible)} input field(s) via first-box bulk input"):
            return True

    # Strategy C: one character per box. Never clear() — select-all often wipes
    # the whole OTP group and leaves only the first 1–2 characters visible.
    for _ in range(16):
        visible_fields = _verification_input_elements(page)
        if not visible_fields:
            _sleep(0.25)
            continue
        if _success(visible_fields, f"filled verification code into {len(visible_fields)} individual input fields"):
            return True

        already = _read_code_chars(visible_fields)
        if already and normalized.startswith(already):
            remaining = normalized[len(already) :]
            empty_fields = [
                field
                for field in visible_fields
                if not re.sub(r"[^A-Za-z0-9]", "", _element_value(field))
            ]
            targets = empty_fields or visible_fields[len(already) :]
        else:
            remaining = normalized
            targets = visible_fields

        if not remaining:
            if _success(visible_fields, f"filled verification code into {len(visible_fields)} individual input fields"):
                return True
            _sleep(0.25)
            continue
        if not targets:
            _sleep(0.25)
            continue

        filled_now = 0
        for element, character in zip(targets, remaining):
            ok = _type_into(element, character, clear=False, allow_js=False)
            if not ok or not re.sub(r"[^A-Za-z0-9]", "", _element_value(element)):
                # Per-box JS fallback when keyboard insert is ignored.
                _set_input_value_js(element, character)
            filled_now += 1
            _sleep(0.05)
        remaining = remaining[filled_now:]
        visible_fields = _verification_input_elements(page) or visible_fields
        if _success(visible_fields, f"filled verification code into {len(normalized)} individual input fields"):
            return True
        if not remaining:
            # Progressive UIs may need a beat before the next trio mounts.
            _sleep(0.3)
            continue
        _sleep(0.25)

    visible_fields = _verification_input_elements(page) or fields
    got = _read_code_chars(visible_fields)
    log(
        f"verification code received, filled {len(got)}/{len(normalized)} "
        f"characters, but the input did not accept the full code"
    )
    return False


# ---------------------------------------------------------------------------
# Registration profile form
# ---------------------------------------------------------------------------

def _fill_profile_name_fields(
    page: Any,
    *,
    first_name: str,
    last_name: str,
    display_name: str,
    log: LogFn,
) -> tuple[bool, bool]:
    """Fill first/last (名/姓) using selectors, labels, then left-to-right order."""
    first_filled = False
    last_filled = False

    if first_name:
        first_filled = _fill_first(
            page,
            (
                "css:input[name='first_name']",
                "css:input[name='given_name']",
                "css:input[name='firstName']",
                "css:input[autocomplete='given-name']",
                "css:input[id*='first']",
                "css:input[id*='First']",
                "css:input[id*='given']",
            ),
            first_name,
            log,
            "first name",
        )
        if not first_filled:
            first_filled = _fill_by_label_text(
                page,
                first_name,
                log,
                "first name",
                ("名", "名字", "First name", "Given name", "first name"),
            )
        if not first_filled:
            first_filled = _fill_by_placeholder_or_aria(
                page,
                first_name,
                log,
                "first name",
                ("first name", "given name", "名", "名字", "firstname"),
            )

    if last_name:
        last_filled = _fill_first(
            page,
            (
                "css:input[name='last_name']",
                "css:input[name='family_name']",
                "css:input[name='lastName']",
                "css:input[autocomplete='family-name']",
                "css:input[id*='last']",
                "css:input[id*='Last']",
                "css:input[id*='family']",
            ),
            last_name,
            log,
            "last name",
        )
        if not last_filled:
            last_filled = _fill_by_label_text(
                page,
                last_name,
                log,
                "last name",
                ("姓", "姓氏", "Last name", "Family name", "last name"),
            )
        if not last_filled:
            last_filled = _fill_by_placeholder_or_aria(
                page,
                last_name,
                log,
                "last name",
                ("last name", "family name", "姓", "姓氏", "lastname"),
            )

    # xAI "完成注册": two empty text boxes in order 名 then 姓, labels outside inputs.
    if (first_name or last_name) and not (first_filled and last_filled):
        text_inputs = _visible_text_inputs(page)
        if len(text_inputs) >= 2:
            if first_name and not first_filled:
                first_filled = _fill_element(text_inputs[0], first_name, log, "first name")
            if last_name and not last_filled:
                last_filled = _fill_element(text_inputs[1], last_name, log, "last name")
        elif len(text_inputs) == 1 and display_name and not (first_filled or last_filled):
            _fill_element(text_inputs[0], display_name, log, "display name")

    if display_name and not (first_filled or last_filled):
        if not _fill_first(
            page,
            (
                "css:input[name='name']",
                "css:input[name='display_name']",
                "css:input[name='displayName']",
                "css:input[autocomplete='name']",
            ),
            display_name,
            log,
            "display name",
        ):
            _fill_by_placeholder_or_aria(
                page,
                display_name,
                log,
                "display name",
                ("display name", "full name", "your name", "姓名", "名称"),
            )

    return first_filled, last_filled


# ---------------------------------------------------------------------------
# Public: device authorization
# ---------------------------------------------------------------------------

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
    reopen: bool = True,
) -> None:
    """Drive the xAI device authorization UI until done or timeout.

    Token acquisition is external (CPA poll or local device token poll).
    stop_event may be set by the poller when tokens are ready.

    After a successful in-browser registration the session is already
    authenticated: pass ``reopen=False`` so the current Continue → Allow
    handoff is not discarded by reloading the device URI.
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

    if reopen:
        log("opening device authorization page")
        try:
            page.get(verification_uri_complete, timeout=60)
        except TypeError:
            page.get(verification_uri_complete)
        _sleep(1.0)
    else:
        log("resuming device authorization in the current authenticated session")
        _sleep(0.5)

    deadline = time.time() + timeout_sec
    phase = "device"
    login_attempts = 0
    allow_clicks = 0
    continue_clicks = 0

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("authorization completed; closing browser")
            return

        url = _page_url(page)
        text = _visible_text(page)

        if _device_authorized(url, text):
            log("device done page — waiting for token poll")
            # Keep the browser open until the external poller sets stop_event.
            _sleep(1.5)
            continue

        if "Invalid action" in text:
            log("Invalid action — reopen device uri")
            try:
                page.get(verification_uri_complete)
            except Exception as e:
                log(f"reopen failed: {e}")
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
            log("404/not-found — reopen device uri")
            try:
                page.get(verification_uri_complete)
            except Exception as e:
                log(f"reopen failed: {e}")
            _sleep(1.2)
            phase = "device"
            continue

        # Consent — Allow/允许 (post-registration and normal login share this screen).
        if _looks_like_consent_screen(url, text) or _find_button_exact(page, "允许") or _find_button_exact(
            page, "Allow"
        ):
            phase = "consent"
            if allow_clicks < 8 and _click_allow(page, log):
                allow_clicks += 1
                log(f"consent allow click #{allow_clicks}")
                _sleep(2.5)
                continue
            _sleep(1.0)
            continue

        # Post-login / redirect interstitial: only Continue is needed.
        if (
            "正在重定向" in text
            or "redirect" in low
            or ("/account" in url and "sign-in" not in url)
            or _has_any_text(text, ("continue to", "继续以", "继续授权", "返回应用", "return to"))
            or (
                phase in {"device", "consent", "email", "password", "post_login"}
                and _find_button_exact(page, "继续")
                and not page.ele("css:input[type='password']", timeout=0.15)
                and not page.ele("css:input[name='user_code']", timeout=0.15)
            )
        ):
            if continue_clicks < 10 and _click_continue(page, log, real=True):
                continue_clicks += 1
                phase = "post_login"
                log(f"continue click #{continue_clicks}")
                _sleep(2.0)
                continue

        # Device code page
        try:
            has_user_code = bool(page.ele("css:input[name='user_code']", timeout=0.3))
        except Exception:
            has_user_code = False
        if has_user_code and "consent" not in url:
            phase = "device"
            if user_code:
                try:
                    uc = page.ele("css:input[name='user_code']")
                    cur = (uc.value or "") if uc else ""
                    want = re.sub(r"[^A-Za-z0-9]", "", user_code)
                    have = re.sub(r"[^A-Za-z0-9]", "", cur)
                    if want and want.upper() not in have.upper():
                        compact = want.upper()
                        dashed_code = (
                            f"{compact[:3]}-{compact[3:]}" if len(compact) == 6 else user_code
                        )
                        for candidate in (compact, dashed_code, user_code):
                            try:
                                uc.clear()
                            except Exception:
                                pass
                            try:
                                uc.input(candidate, clear=True)
                            except TypeError:
                                uc.input(candidate)
                            except Exception:
                                continue
                            cur = (uc.value or "") if uc else ""
                            have = re.sub(r"[^A-Za-z0-9]", "", cur)
                            if want.upper() in have.upper() or (not cur and candidate):
                                log("filled user_code")
                                break
                except Exception:
                    pass
            if _click_continue(page, log, real=True):
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

        # Already-authenticated registration handoff: bare Continue before login fields.
        if not page.ele("css:input[type='password']", timeout=0.15) and not page.ele(
            "css:input[type='email']", timeout=0.15
        ):
            if _click_continue(page, log, real=True):
                phase = "post_login"
                _sleep(2.0)
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


# ---------------------------------------------------------------------------
# Public: account registration
# ---------------------------------------------------------------------------

def register_account(
    page: Any,
    *,
    email: str,
    password: str,
    verification_code: Callable[[], str | None],
    first_name: str = "",
    last_name: str = "",
    display_name: str = "",
    start_url: str = "",
    timeout_sec: float = 300.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> bool:
    """Register an authorized xAI account through visible browser controls.

    ``verification_code`` is called only after the UI requests an email code.
    It should return the code, or ``None`` while the mailbox is still pending.
    The function never bypasses challenge widgets or submits hidden forms.  On
    success the authenticated browser remains open for device-code approval.
    """
    log = log or _noop
    email = (email or "").strip()
    password = password or ""
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    display_name = (display_name or "").strip()
    if page is None:
        raise BrowserFlowError("page is None")
    if not email or not password:
        raise BrowserFlowError("email/password required for registration")
    if not callable(verification_code):
        raise BrowserFlowError("verification_code callback is required")

    if start_url:
        log("opening registration entry page")
        try:
            page.get(start_url, timeout=60)
        except TypeError:
            page.get(start_url)
        _sleep(1.0)

    deadline = time.time() + timeout_sec
    phase = "entry"
    code_value = ""
    code_requested_at = 0.0

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("registration stopped")
            return False

        url = _page_url(page)
        text = _visible_text(page)

        # After profile submit: drive Continue → Allow until device is authorized
        # or the session is clearly ready for the outer approve_device_code handoff.
        if phase in {"profile", "post_reg", "handoff"}:
            if _device_authorized(url, text):
                log("registration completed; device authorized")
                return True
            if stop_event is not None and stop_event.is_set():
                log("registration completed; token poll finished during handoff")
                return True
            if _looks_like_consent_screen(url, text) or _find_button_exact(page, "允许") or _find_button_exact(
                page, "Allow"
            ):
                phase = "handoff"
                if _click_allow(page, log):
                    log("clicked Allow after registration")
                    _sleep(2.5)
                    continue
            if _click_continue(page, log, real=True):
                phase = "post_reg"
                log("clicked Continue after registration")
                _sleep(2.0)
                continue
            if phase in {"post_reg", "handoff"} and _registration_complete(url, text):
                log("registration completed; browser is ready for authorization")
                return True
            # If submit did not navigate away from the profile form, resume filling.
            try:
                still_profile = bool(page.ele("css:input[type='password']", timeout=0.2))
            except Exception:
                still_profile = False
            if phase in {"post_reg", "handoff"} and still_profile and _has_any_text(
                text, ("完成注册", "set your password", "create a password", "请验证您是真人")
            ):
                phase = "profile"
            elif phase in {"post_reg", "handoff"}:
                # Stay in handoff rather than falling back into email/OTP logic.
                _sleep(0.8)
                continue

        if phase in {"verification", "profile"} and _registration_complete(url, text):
            log("registration completed; browser is ready for authorization")
            return True

        # The initial device/auth page may need an explicit Continue first.
        if phase == "entry" and _click_visible(page, ["Continue", "继续"], log):
            phase = "choose_registration"
            _sleep(1.2)
            continue

        # After device-page Continue, wait for and click the visible registration action.
        # Do not fall through to email fields until that explicit transition succeeds.
        if phase == "choose_registration":
            if _click_visible(
                page,
                ["Register", "Create account", "Sign up", "注册", "创建账户"],
                log,
            ) or _click_link_containing(page, "sign-up", log):
                phase = "email_signup"
                _sleep(1.0)
                continue
            _sleep(0.8)
            continue

        if phase == "entry" and _has_any_text(
            text, ("register", "create an account", "sign up", "注册", "创建账户")
        ):
            if _click_visible(
                page,
                ["Register", "Create account", "Sign up", "注册", "创建账户"],
                log,
            ):
                phase = "email_signup"
                _sleep(1.0)
                continue

        if phase != "email_form" and _has_any_text(
            text,
            ("sign up with email", "continue with email", "use email", "使用邮箱", "使用邮箱注册"),
        ):
            if _click_visible(
                page,
                ["Sign up with email", "Continue with email", "Use email", "使用邮箱注册", "使用邮箱"],
                log,
            ):
                phase = "email_form"
                _sleep(1.0)
                continue

        verification_screen = _has_any_text(
            text,
            ("verification code", "verify your email", "confirmation code", "验证码", "确认邮箱"),
        )
        email_present = False
        if phase not in {"verification", "profile"} and not verification_screen:
            email_present = _fill_first(
                page,
                (
                    "css:input[type='email']",
                    "css:input[name='email']",
                    "css:input[name='email_address']",
                    "css:input[autocomplete='email']",
                ),
                email,
                log,
                "registration email",
            )
        try:
            password_present = bool(page.ele("css:input[type='password']", timeout=0.2))
        except Exception:
            password_present = False

        if email_present and not password_present:
            phase = "email_signup"
            if _click_visible(
                page,
                ["Continue", "Next", "Create account", "Sign up", "注册", "继续", "下一步"],
                log,
            ):
                log("submitted registration email")
                _sleep(1.5)
                continue

        profile_screen = _is_profile_screen(page, text, password_present)

        # OTP step. Never treat the profile form's 名/姓 boxes as verification inputs.
        if not profile_screen and phase != "profile":
            code_inputs = _verification_input_elements(page)
            if code_inputs or _has_any_text(
                text,
                ("verification code", "verify your email", "confirmation code", "验证码", "确认邮箱"),
            ):
                phase = "verification"
                if not code_value:
                    if not code_requested_at:
                        code_requested_at = time.time()
                        log("waiting for registration verification code")
                    try:
                        candidate = verification_code()
                    except Exception as e:
                        raise BrowserFlowError(f"verification code lookup failed: {e}") from e
                    if candidate:
                        code_value = str(candidate).strip()
                        log("received registration verification code")
                    else:
                        _sleep(1.0)
                        continue
                if not _fill_verification_code(page, code_value, log):
                    _sleep(0.8)
                    continue
                if _click_visible(
                    page,
                    ["Verify", "Confirm email", "Continue", "Next", "确认邮箱", "确认", "继续", "下一步"],
                    log,
                ):
                    _sleep(1.3)
                    continue

        # Profile step: 名/姓/密码 + Cloudflare Turnstile ("完成注册").
        if profile_screen:
            if phase != "profile":
                log("on registration profile form (name/password/turnstile)")
            phase = "profile"
            first_filled, last_filled = _fill_profile_name_fields(
                page,
                first_name=first_name,
                last_name=last_name,
                display_name=display_name,
                log=log,
            )
            if first_name or last_name:
                log(
                    f"profile name fields: first={'ok' if first_filled or not first_name else 'miss'} "
                    f"last={'ok' if last_filled or not last_name else 'miss'}"
                )

            password_filled = _fill_all_password_fields(page, password, log)
            if password_filled == 0:
                # Label-based password for zh "密码" when type= attribute is delayed.
                if not _fill_by_label_text(
                    page,
                    password,
                    log,
                    "registration password",
                    ("密码", "Password", "password"),
                ):
                    _fill(page, "css:input[type='password']", password, log, "registration password")
                    try:
                        pwd_check = page.ele("css:input[type='password']", timeout=0.2)
                        password_filled = 1 if pwd_check and _element_value(pwd_check) == password else 0
                    except Exception:
                        password_filled = 0
                else:
                    password_filled = 1

            # Wait for Turnstile after credentials are present so the token binds
            # to a complete form. Headed mode may still need a manual checkbox.
            turnstile_ok = wait_turnstile(page, log, 45)
            if not turnstile_ok:
                log("waiting for Cloudflare Turnstile (complete checkbox in headed browser if shown)")
                wait_turnstile(page, log, 30)

            # Only submit once password fields accept the value (avoid empty submit).
            pwd_el = None
            try:
                pwd_el = page.ele("css:input[type='password']", timeout=0.2)
            except Exception:
                pwd_el = None
            ready_to_submit = password_filled > 0 or bool(pwd_el and _element_value(pwd_el))
            if not ready_to_submit:
                log("profile form not ready to submit yet (password empty)")
            elif _click_visible(
                page,
                [
                    "完成注册",
                    "Create account",
                    "Complete sign up",
                    "Complete registration",
                    "Sign up",
                    "Continue",
                    "Submit",
                    "注册",
                    "创建账户",
                    "继续",
                    "完成",
                ],
                log,
            ):
                phase = "post_reg"
                log("submitted registration profile; waiting for Continue/Allow handoff")
                _sleep(2.0)
                continue
            _sleep(0.8)
            continue

        # Do not automate unknown screens: retain the headed browser for the operator.
        _sleep(0.8)

    log(f"registration timed out phase={phase}")
    return False
