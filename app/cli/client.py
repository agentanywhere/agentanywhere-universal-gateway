"""Tiny HTTP client used by the CLI to talk to the admin API.

Kept dependency-free (stdlib :mod:`urllib.request`) so the CLI works on
fresh installs even when optional packages are missing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class CLIError(Exception):
    """Raised when an HTTP request fails or the server reports an error."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class AdminClient:
    """Small REST client tailored to the gateway admin API."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        admin: bool = True,
    ) -> Any:
        url = self._build_url(path, params)
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if admin and self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                if not body:
                    return None
                try:
                    return json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
            raise CLIError(
                f"{method.upper()} {path} -> {exc.code}: {payload}",
                status_code=exc.code,
                payload=payload,
            ) from None
        except urllib.error.URLError as exc:
            raise CLIError(
                f"could not reach {url}: {exc.reason}",
                status_code=None,
            ) from None

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> str:
        payload = self.post(
            "/admin/auth/login",
            json_body={"username": username, "password": password},
            admin=False,
        )
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise CLIError(f"unexpected login response: {payload}")
        token = payload["access_token"]
        self.token = token
        return token

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"
        if params:
            qp = {k: v for k, v in params.items() if v is not None}
            if qp:
                url = f"{url}?{urllib.parse.urlencode(qp, doseq=True)}"
        return url
