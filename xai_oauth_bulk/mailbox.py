"""Temporary-mail providers and bounded verification-code polling."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import random
import re
import secrets
import string
import time
from typing import Any, Callable, Mapping, Protocol

import requests


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"
YYDS_API_BASE = "https://maliapi.215.im/v1"


class MailboxError(RuntimeError):
    """A mailbox provider could not provision or read a mailbox."""


class VerificationCodeTimeout(MailboxError):
    """No verification code arrived before the configured deadline."""


@dataclass(frozen=True)
class Mailbox:
    provider: str
    address: str
    token: str


@dataclass(frozen=True)
class MailboxConfig:
    provider: str = "duckmail"
    request_timeout_sec: float = 15.0
    poll_timeout_sec: float = 180.0
    poll_interval_sec: float = 3.0
    duckmail_api_base: str = DUCKMAIL_API_BASE
    duckmail_api_key: str = ""
    cloudflare_api_base: str = ""
    cloudflare_api_key: str = ""
    cloudflare_auth_mode: str = "none"
    cloudflare_path_domains: str = "/api/domains"
    cloudflare_path_accounts: str = "/api/new_address"
    cloudflare_path_token: str = "/api/token"
    cloudflare_path_messages: str = "/api/mails"
    cloudflare_domains: tuple[str, ...] = ()
    yyds_api_base: str = YYDS_API_BASE
    yyds_api_key: str = ""
    yyds_jwt: str = ""
    yyds_preferred_domains: tuple[str, ...] = ()
    yyds_blocked_domains: tuple[str, ...] = ()
    yyds_domain_selection: str = "random"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "MailboxConfig":
        def text(name: str, default: str = "") -> str:
            return str(values.get(name, default) or "").strip()

        def names(name: str) -> tuple[str, ...]:
            raw = values.get(name, "")
            if isinstance(raw, str):
                return tuple(x.strip().lower() for x in raw.split(",") if x.strip())
            if isinstance(raw, (list, tuple)):
                return tuple(str(x).strip().lower() for x in raw if str(x).strip())
            return ()

        def number(name: str, default: float) -> float:
            try:
                return max(float(values.get(name, default)), 0.0)
            except (TypeError, ValueError):
                return default

        return cls(
            provider=(text("mailbox_provider") or text("email_provider", "duckmail")).lower(),
            request_timeout_sec=number("mailbox_request_timeout_sec", 15.0),
            poll_timeout_sec=number("mailbox_poll_timeout_sec", 180.0),
            poll_interval_sec=number("mailbox_poll_interval_sec", 3.0),
            duckmail_api_base=text("duckmail_api_base", DUCKMAIL_API_BASE).rstrip("/"),
            duckmail_api_key=text("duckmail_api_key"),
            cloudflare_api_base=text("cloudflare_api_base").rstrip("/"),
            cloudflare_api_key=text("cloudflare_api_key"),
            cloudflare_auth_mode=text("cloudflare_auth_mode", "none").lower(),
            cloudflare_path_domains=_path(text("cloudflare_path_domains", "/api/domains")),
            cloudflare_path_accounts=_path(text("cloudflare_path_accounts", "/api/new_address")),
            cloudflare_path_token=_path(text("cloudflare_path_token", "/api/token")),
            cloudflare_path_messages=_path(text("cloudflare_path_messages", "/api/mails")),
            cloudflare_domains=names("defaultDomains") or names("cloudflare_domains"),
            yyds_api_base=text("yyds_api_base", YYDS_API_BASE).rstrip("/"),
            yyds_api_key=text("yyds_api_key"),
            yyds_jwt=text("yyds_jwt"),
            yyds_preferred_domains=names("yyds_preferred_domains"),
            yyds_blocked_domains=names("yyds_blocked_domains"),
            yyds_domain_selection=text("yyds_domain_selection", "random").lower(),
        )


def _path(value: str) -> str:
    return value if value.startswith("/") else f"/{value}"


def _username(length: int = 10) -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("hydra:member", "results", "messages", "data"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = candidate.get("messages")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def extract_verification_code(text: str, subject: str = "") -> str | None:
    """Extract only an xAI-shaped code from a validated message."""
    subject_match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
    if subject_match:
        return subject_match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    for pattern in (
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _message_text(message: Mapping[str, Any]) -> tuple[str, str]:
    parts: list[str] = []
    for key in ("text", "raw", "content", "intro", "body", "snippet"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    html = message.get("html", [])
    if isinstance(html, str):
        html = [html]
    if isinstance(html, list):
        parts.extend(re.sub(r"<[^>]+>", " ", value) for value in html if isinstance(value, str))
    return unescape("\n".join(parts)), str(message.get("subject", "") or "")


class MailboxProvider(Protocol):
    name: str

    def provision(self) -> Mailbox: ...

    def messages(self, mailbox: Mailbox) -> list[dict[str, Any]]: ...

    def message_detail(self, mailbox: Mailbox, message_id: str) -> dict[str, Any]: ...


class _Provider:
    name: str

    def __init__(self, config: MailboxConfig, session: requests.Session) -> None:
        self.config = config
        self.session = session

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.config.request_timeout_sec)
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise MailboxError(f"{self.name} request failed: {exc}") from exc

    def _json(self, response: Any) -> dict[str, Any] | list[Any]:
        try:
            payload = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise MailboxError(f"{self.name} returned invalid JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise MailboxError(f"{self.name} returned an unexpected JSON payload")
        return payload


class DuckMailProvider(_Provider):
    name = "duckmail"

    def _headers(self, token: str = "") -> dict[str, str]:
        value = token or self.config.duckmail_api_key
        return {"Authorization": f"Bearer {value}"} if value else {}

    def provision(self) -> Mailbox:
        domains = _items(self._json(self._request("GET", f"{self.config.duckmail_api_base}/domains", headers=self._headers())))
        verified = [item for item in domains if item.get("isVerified") and item.get("domain")]
        private = [item for item in verified if item.get("ownerId")]
        candidate = (private or verified)
        if not candidate:
            raise MailboxError("DuckMail returned no verified domains")
        address = f"{_username()}@{candidate[0]['domain']}"
        password = secrets.token_urlsafe(18)
        self._request("POST", f"{self.config.duckmail_api_base}/accounts", json={"address": address, "password": password, "expiresIn": 0}, headers={"Content-Type": "application/json", **self._headers()})
        payload = self._json(self._request("POST", f"{self.config.duckmail_api_base}/token", json={"address": address, "password": password}))
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise MailboxError("DuckMail did not return a mailbox token")
        return Mailbox(self.name, address, token)

    def messages(self, mailbox: Mailbox) -> list[dict[str, Any]]:
        return _items(self._json(self._request("GET", f"{self.config.duckmail_api_base}/messages", headers=self._headers(mailbox.token))))

    def message_detail(self, mailbox: Mailbox, message_id: str) -> dict[str, Any]:
        payload = self._json(self._request("GET", f"{self.config.duckmail_api_base}/messages/{message_id}", headers=self._headers(mailbox.token)))
        return payload if isinstance(payload, dict) else {}


class CloudflareProvider(_Provider):
    name = "cloudflare"

    def __init__(self, config: MailboxConfig, session: requests.Session) -> None:
        super().__init__(config, session)
        if not config.cloudflare_api_base:
            raise MailboxError("cloudflare_api_base is required for the Cloudflare provider")

    def _auth_headers(self, content_type: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"} if content_type else {}
        key, mode = self.config.cloudflare_api_key, self.config.cloudflare_auth_mode
        if key and mode == "x-api-key":
            headers["X-API-Key"] = key
        elif key and mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif key and mode not in {"none", "query-key"}:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _params(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = dict(params or {})
        if self.config.cloudflare_api_key and self.config.cloudflare_auth_mode == "query-key":
            result["key"] = self.config.cloudflare_api_key
        return result

    def provision(self) -> Mailbox:
        path = self.config.cloudflare_path_accounts
        domain = random.choice(self.config.cloudflare_domains) if self.config.cloudflare_domains else ""
        if path.rstrip("/").lower() == "/admin/new_address":
            payload: dict[str, Any] = {"name": _username(), "enablePrefix": True}
            if domain:
                payload["domain"] = domain
            headers = self._auth_headers(content_type=True)
        else:
            payload = {"domain": domain} if domain else {}
            headers = {"Content-Type": "application/json"}
        response = self._request("POST", f"{self.config.cloudflare_api_base}{path}", json=payload, headers=headers, params=self._params())
        data = self._json(response)
        address = data.get("address") if isinstance(data, dict) else None
        token = data.get("jwt") if isinstance(data, dict) else None
        if not isinstance(address, str) or not address or not isinstance(token, str) or not token:
            raise MailboxError("Cloudflare new-address response lacks address or jwt")
        return Mailbox(self.name, address, token)

    def messages(self, mailbox: Mailbox) -> list[dict[str, Any]]:
        response = self._request("GET", f"{self.config.cloudflare_api_base}{self.config.cloudflare_path_messages}", headers={"Authorization": f"Bearer {mailbox.token}"}, params=self._params({"limit": 20, "offset": 0}))
        return _items(self._json(response))

    def message_detail(self, mailbox: Mailbox, message_id: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {mailbox.token}"}
        urls = (f"{self.config.cloudflare_api_base}/api/mail/{message_id}", f"{self.config.cloudflare_api_base}{self.config.cloudflare_path_messages}/{message_id}")
        last_error: MailboxError | None = None
        for url in urls:
            try:
                payload = self._json(self._request("GET", url, headers=headers, params=self._params()))
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    return payload["data"]
                return payload if isinstance(payload, dict) else {}
            except MailboxError as exc:
                last_error = exc
        raise last_error or MailboxError("Cloudflare message detail request failed")


class YYDSProvider(_Provider):
    name = "yyds"

    def _headers(self, token: str = "", content_type: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"} if content_type else {}
        value = token or self.config.yyds_jwt
        if value:
            headers["Authorization"] = f"Bearer {value}"
        elif self.config.yyds_api_key:
            headers["X-API-Key"] = self.config.yyds_api_key
        return headers

    def provision(self) -> Mailbox:
        if not self.config.yyds_jwt and not self.config.yyds_api_key:
            raise MailboxError("yyds_api_key or yyds_jwt is required for the YYDS provider")
        raw_domains = self._json(self._request("GET", f"{self.config.yyds_api_base}/domains", headers=self._headers()))
        domains = raw_domains.get("data", []) if isinstance(raw_domains, dict) and raw_domains.get("success") else []
        verified = [item for item in domains if isinstance(item, dict) and item.get("isVerified") and str(item.get("domain", "")).lower() not in self.config.yyds_blocked_domains]
        preferred = {name: item for item in verified for name in [str(item.get("domain", "")).lower()]}
        selected = next((preferred[name] for name in self.config.yyds_preferred_domains if name in preferred), None)
        if selected is None:
            private = [item for item in verified if not item.get("isPublic")]
            public = [item for item in verified if item.get("isPublic")]
            choices = private or public or verified
            if not choices:
                raise MailboxError("YYDS returned no usable verified domains")
            selected = random.choice(choices) if self.config.yyds_domain_selection == "random" else choices[0]
        username = _username()
        payload = self._json(self._request("POST", f"{self.config.yyds_api_base}/accounts", json={"address": username, "domain": selected["domain"]}, headers=self._headers(content_type=True)))
        data = payload.get("data", {}) if isinstance(payload, dict) and payload.get("success") else {}
        address = data.get("address") or f"{username}@{selected['domain']}"
        token = data.get("token")
        if not token:
            token_payload = self._json(self._request("POST", f"{self.config.yyds_api_base}/token", json={"address": address}, headers=self._headers(content_type=True)))
            token = token_payload.get("data", {}).get("token") if isinstance(token_payload, dict) and token_payload.get("success") else None
        if not isinstance(token, str) or not token:
            raise MailboxError("YYDS did not return a mailbox token")
        return Mailbox(self.name, str(address), token)

    def messages(self, mailbox: Mailbox) -> list[dict[str, Any]]:
        payload = self._json(self._request("GET", f"{self.config.yyds_api_base}/messages", params={"address": mailbox.address}, headers=self._headers(mailbox.token)))
        return _items(payload)

    def message_detail(self, mailbox: Mailbox, message_id: str) -> dict[str, Any]:
        payload = self._json(self._request("GET", f"{self.config.yyds_api_base}/messages/{message_id}", headers=self._headers(mailbox.token)))
        if isinstance(payload, dict) and payload.get("success") and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}


class MailboxService:
    """Provisions one configured mailbox provider and waits for one code."""

    def __init__(self, config: MailboxConfig | Mapping[str, Any] | Any, *, session: requests.Session | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        if isinstance(config, MailboxConfig):
            self.config = config
        elif isinstance(config, Mapping):
            self.config = MailboxConfig.from_mapping(config)
        else:
            self.config = MailboxConfig.from_mapping(vars(config))
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        providers: dict[str, type[_Provider]] = {"duckmail": DuckMailProvider, "cloudflare": CloudflareProvider, "yyds": YYDSProvider}
        provider_cls = providers.get(self.config.provider)
        if provider_cls is None:
            raise MailboxError(f"unsupported email provider: {self.config.provider!r}")
        self.provider: MailboxProvider = provider_cls(self.config, self.session)

    def provision(self) -> Mailbox:
        return self.provider.provision()

    def wait_for_code(
        self,
        mailbox: Mailbox,
        *,
        timeout_sec: float | None = None,
        poll_interval_sec: float | None = None,
        log: Callable[[str], None] | None = None,
    ) -> str:
        timeout = self.config.poll_timeout_sec if timeout_sec is None else max(timeout_sec, 0.0)
        interval = self.config.poll_interval_sec if poll_interval_sec is None else max(poll_interval_sec, 0.0)
        deadline = self.clock() + timeout
        attempts: dict[str, int] = {}
        while self.clock() < deadline:
            try:
                messages = self.provider.messages(mailbox)
            except MailboxError:
                messages = []
            for message in messages:
                message_id = str(message.get("id") or message.get("msgid") or "")
                if not message_id or attempts.get(message_id, 0) >= 5:
                    continue
                recipients = message.get("to") or []
                addresses = [
                    str(item.get("address", "")).lower()
                    for item in recipients
                    if isinstance(item, dict)
                ]
                listed_address = str(message.get("address", "")).lower()
                is_cloudflare = self.provider.name == "cloudflare"
                if addresses:
                    if mailbox.address.lower() not in addresses:
                        continue
                elif listed_address:
                    if listed_address != mailbox.address.lower():
                        continue
                elif not is_cloudflare:
                    # DuckMail/YYDS must explicitly identify the target mailbox.
                    continue

                attempts[message_id] = attempts.get(message_id, 0) + 1
                if is_cloudflare:
                    # Cloudflare-compatible APIs may expose content in the list response.
                    text, subject = _message_text(message)
                    try:
                        detail = self.provider.message_detail(mailbox, message_id)
                        detail_text, detail_subject = _message_text(detail)
                        text = f"{text}\n{detail_text}"
                        subject = subject or detail_subject
                    except MailboxError:
                        pass
                else:
                    # Match the reference implementation: do not trust list snippets
                    # for DuckMail/YYDS, and require a full message-detail response.
                    try:
                        detail = self.provider.message_detail(mailbox, message_id)
                    except MailboxError:
                        continue
                    text, subject = _message_text(detail)

                code = extract_verification_code(text, subject)
                if code:
                    if log:
                        log(f"verified mailbox email received (message={message_id}); code extracted")
                    return code
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(interval, remaining))
        raise VerificationCodeTimeout(f"{self.provider.name} did not receive a verification code within {timeout:g}s")
