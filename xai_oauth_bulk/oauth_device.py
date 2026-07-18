"""Standalone xAI OAuth device-code grant (RFC 8628).

Aligned with CLIProxyAPI internal/auth/xai.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


class OAuthDeviceError(RuntimeError):
    pass


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    token_endpoint: str
    raw: dict[str, Any]


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    id_token: str | None
    token_type: str
    expires_in: int
    raw: dict[str, Any]


def _session(proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "xai-oauth-bulk/0.1",
        }
    )
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def discover(proxy: str | None = None, timeout: float = 30.0) -> tuple[str, str]:
    s = _session(proxy)
    try:
        resp = s.get(DISCOVERY_URL, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        # fall back to known endpoints
        return DEVICE_CODE_URL, TOKEN_URL
    device_ep = str(body.get("device_authorization_endpoint") or DEVICE_CODE_URL).strip()
    token_ep = str(body.get("token_endpoint") or TOKEN_URL).strip()
    return device_ep or DEVICE_CODE_URL, token_ep or TOKEN_URL


def request_device_code(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> DeviceCodeSession:
    device_ep, token_ep = discover(proxy=proxy, timeout=timeout)
    s = _session(proxy)
    resp = s.post(
        device_ep,
        data={"client_id": client_id, "scope": scope},
        timeout=timeout,
    )
    try:
        body = resp.json()
    except ValueError as e:
        raise OAuthDeviceError(f"device code non-json HTTP {resp.status_code}: {resp.text[:300]}") from e
    if resp.status_code != 200:
        raise OAuthDeviceError(f"device code request failed HTTP {resp.status_code}: {body}")
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise OAuthDeviceError(f"device code response missing fields: {body}")
    vuri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=vuri,
        verification_uri_complete=vcomplete,
        expires_in=int(body.get("expires_in") or 1800),
        interval=max(int(body.get("interval") or 5), 1),
        token_endpoint=token_ep,
        raw=body,
    )


def poll_device_token(
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    token_endpoint: str = TOKEN_URL,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    proxy: str | None = None,
) -> TokenResult:
    log = log or _noop
    s = _session(proxy)
    deadline = time.time() + max(expires_in - 5, 30)
    sleep_for = max(interval, 1)
    waiting_logged = False
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        resp = s.post(
            token_endpoint or TOKEN_URL,
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": device_code,
                "client_id": client_id,
            },
            timeout=timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
            access = str(body["access_token"]).strip()
            refresh = str(body.get("refresh_token") or "").strip()
            if not refresh:
                raise OAuthDeviceError("token response missing refresh_token")
            return TokenResult(
                access_token=access,
                refresh_token=refresh,
                id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
                token_type=str(body.get("token_type") or "Bearer"),
                expires_in=int(body.get("expires_in") or 21600),
                raw=body,
            )
        err = str(body.get("error") or "") if isinstance(body, dict) else ""
        desc = str(body.get("error_description") or "") if isinstance(body, dict) else ""
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
                log(f"OAuth poll slowed down; retrying in {sleep_for}s")
            elif not waiting_logged:
                log("waiting for browser authorization")
                waiting_logged = True
            time.sleep(sleep_for)
            continue
        if err in ("expired_token", "access_denied"):
            raise OAuthDeviceError(f"device auth failed: {err}: {desc}")
        if resp.status_code == 400 and err:
            raise OAuthDeviceError(f"device auth token error: {err}: {desc or body}")
        log(f"oauth poll unexpected HTTP {resp.status_code}: {body!r}")
        time.sleep(sleep_for)
    raise OAuthDeviceError("device auth timed out waiting for user approval")
