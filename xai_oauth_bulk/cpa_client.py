"""CLIProxyAPI management API client for xAI device OAuth."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin

import requests

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


class CPAClientError(RuntimeError):
    pass


@dataclass
class DeviceStartResult:
    url: str
    state: str
    user_code: str
    expires_in: int
    raw: dict[str, Any]


class CPAClient:
    """Talks to CLIProxyAPI management endpoints.

    Auth: Authorization: Bearer <management-key>
    (also accepts X-Management-Key; Bearer matches the TUI client)
    """

    def __init__(
        self,
        base_url: str,
        management_key: str,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/") + "/"
        self.management_key = (management_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        if self.management_key:
            self.session.headers["Authorization"] = f"Bearer {self.management_key}"
            self.session.headers["X-Management-Key"] = self.management_key
        self.session.headers["Accept"] = "application/json"
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        require_dict: bool = True,
    ) -> dict[str, Any]:
        try:
            resp = self.session.request(
                method,
                self._url(path),
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise CPAClientError(f"request failed: {e}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.status_code >= 400:
            raise CPAClientError(f"HTTP {resp.status_code}: {body}")
        if isinstance(body, dict):
            return body
        if require_dict:
            raise CPAClientError(f"unexpected response: {body!r}")
        return {"status": "ok", "raw": body}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, require_dict=True)

    def _delete(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("DELETE", path, params=params, require_dict=False)

    def start_xai_oauth(self) -> DeviceStartResult:
        """GET /v0/management/xai-auth-url"""
        body = self._get("/v0/management/xai-auth-url")
        url = str(body.get("url") or "").strip()
        state = str(body.get("state") or "").strip()
        if not url or not state:
            raise CPAClientError(f"xai-auth-url missing url/state: {body}")
        return DeviceStartResult(
            url=url,
            state=state,
            user_code=str(body.get("user_code") or "").strip(),
            expires_in=int(body.get("expires_in") or 1800),
            raw=body,
        )

    def get_auth_status(self, state: str) -> tuple[str, str]:
        """GET /v0/management/get-auth-status?state=...

        Returns (status, error_message). status in wait|ok|error.
        """
        body = self._get("/v0/management/get-auth-status", params={"state": state})
        status = str(body.get("status") or "").strip().lower()
        err = str(body.get("error") or "").strip()
        if status in {"ok", "wait", "error"}:
            return status, err
        # defensive mapping
        if err:
            return "error", err
        return "wait", ""

    def list_xai_auth_file_names(self) -> set[str]:
        """Return the names of xAI credential files known to CPA."""
        body = self._get("/v0/management/auth-files")
        files = body.get("files")
        if not isinstance(files, list):
            raise CPAClientError("auth-files response missing files list")

        names: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                continue
            file_type = str(item.get("type") or item.get("provider") or "").strip().lower()
            name = item.get("name")
            if file_type == "xai" and isinstance(name, str) and name.strip():
                names.add(name.strip())
        return names

    def cancel_oauth_session(self, state: str) -> None:
        """DELETE /v0/management/oauth-session?state=..."""
        if not state:
            return
        try:
            self._delete("/v0/management/oauth-session", params={"state": state})
        except CPAClientError:
            # best-effort cancel
            pass

    def wait_auth_status(
        self,
        state: str,
        *,
        timeout_sec: float = 300.0,
        interval_sec: float = 2.0,
        log: LogFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        log = log or _noop
        deadline = time.time() + max(timeout_sec, 30.0)
        waiting_logged = False
        while time.time() < deadline:
            if cancel and cancel():
                raise CPAClientError("cancelled")
            status, err = self.get_auth_status(state)
            if status == "ok":
                log("CPA authorization completed")
                return
            if status == "error":
                raise CPAClientError(err or "authentication failed")
            if not waiting_logged:
                log("waiting for CPA authorization")
                waiting_logged = True
            time.sleep(max(interval_sec, 0.5))
        raise CPAClientError("timed out waiting for CPA OAuth completion")
