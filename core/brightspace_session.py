"""
core/brightspace_session.py
===========================
Authenticated Brightspace access using a browser sign-in instead of OAuth.

The user signs in once through an embedded Brightspace login window (StarID +
MFA, exactly as on the website). We keep the resulting session cookies
(``d2lSessionVal`` / ``d2lSecureSessionVal``) plus the anti-CSRF token and call
the *same* REST API the Brightspace web UI itself uses, e.g.
``/d2l/api/le/1.82/calendar/events/myEvents/``.

Everything is stored DPAPI-encrypted. Sessions expire (hours to weeks depending
on the school's config); when a call bounces to the login page we raise
``BrightspaceSessionExpired`` so the UI can prompt for a fresh sign-in.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

_SESSION_KEY = "brightspace_session"

_JSON_HIJACK_PREFIXES = (")]}'", "while(1);", "for(;;);", "]]>>")


class BrightspaceSessionError(RuntimeError):
    pass


class BrightspaceSessionExpired(BrightspaceSessionError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    """A logged-out API request is answered with a redirect to the SSO/login
    page — treat any redirect on an API call as an expired session."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise BrightspaceSessionExpired(
            "Your Brightspace sign-in has expired — sign in again."
        )


class BrightspaceSession:
    def __init__(self, config_manager, secure_store) -> None:
        self.config_manager = config_manager
        self.secure_store = secure_store

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self.secure_store.get(_SESSION_KEY)
        except Exception:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if data.get("cookies") else None

    def save(self, cookies: Dict[str, str], xsrf: str, lms_url: str) -> None:
        self.secure_store.set(
            _SESSION_KEY,
            json.dumps(
                {
                    "cookies": {k: v for k, v in cookies.items() if v},
                    "xsrf": xsrf or "",
                    "lms_url": (lms_url or "").rstrip("/"),
                    "captured_at": int(time.time()),
                }
            ),
        )

    def clear(self) -> None:
        self.secure_store.delete(_SESSION_KEY)

    def is_active(self) -> bool:
        data = self._load()
        return bool(data and data.get("cookies", {}).get("d2lSessionVal"))

    def captured_at(self) -> Optional[int]:
        data = self._load()
        return data.get("captured_at") if data else None

    def lms_url(self) -> str:
        data = self._load()
        if data and data.get("lms_url"):
            return data["lms_url"]
        return (self.config_manager.get_brightspace_settings().get("lms_url") or "").rstrip("/")

    # ── REST ─────────────────────────────────────────────────────────────
    def api_get(self, route: str, params: Optional[Dict[str, Any]] = None) -> Any:
        data = self._load()
        if not data:
            raise BrightspaceSessionExpired("Not signed in to Brightspace.")

        base = data.get("lms_url") or self.lms_url()
        if not base:
            raise BrightspaceSessionError("Brightspace URL is not set.")

        url = base + route
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})

        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in data["cookies"].items()),
            "Accept": "application/json, text/plain, */*",
            "X-Csrf-Token": data.get("xsrf", ""),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Aegis",
        }

        opener = build_opener(_NoRedirect())
        try:
            with opener.open(Request(url, headers=headers), timeout=30) as resp:
                content_type = resp.headers.get_content_type()
                body = resp.read().decode("utf-8", errors="replace")
        except BrightspaceSessionExpired:
            raise
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BrightspaceSessionExpired(
                    "Brightspace rejected the saved sign-in — sign in again."
                ) from exc
            raise BrightspaceSessionError(
                f"Brightspace API {route} failed (HTTP {exc.code})."
            ) from exc
        except URLError as exc:
            raise BrightspaceSessionError(f"Could not reach Brightspace: {exc.reason}") from exc

        if "json" not in content_type and "text/plain" not in content_type:
            raise BrightspaceSessionExpired(
                "Brightspace returned a web page instead of data — sign in again."
            )

        cleaned = body.lstrip()
        for prefix in _JSON_HIJACK_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].lstrip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise BrightspaceSessionError(
                "Brightspace returned an unexpected response — sign in again."
            ) from exc
