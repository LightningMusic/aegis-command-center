"""
core/google_oauth.py
====================
"Sign in with Google" for the Calendar push. Same shape as the Brightspace
OAuth client: opens the Google consent screen in your system browser (Google
blocks OAuth inside embedded webviews), catches the loopback redirect, and
stores the refresh token DPAPI-encrypted.

You still create a Google Cloud OAuth client once (type: Desktop app) and paste
the Client ID + Secret into Settings — this just replaces the fiddly
"get a refresh token yourself" step with a button.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.oauth_loopback import loopback_capture, pkce_pair, random_state

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

DEFAULT_SCOPE = "https://www.googleapis.com/auth/calendar.events openid email"
DEFAULT_REDIRECT = "http://localhost:8714/"

_REFRESH_KEY = "google_oauth_refresh_token"
_SECRET_KEY = "google_oauth_client_secret"


class GoogleOAuthError(RuntimeError):
    pass


class GoogleOAuth:
    def __init__(self, config_manager, secure_store) -> None:
        self.config_manager = config_manager
        self.secure_store = secure_store
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    # ── config ───────────────────────────────────────────────────────────
    def _cfg(self) -> Dict[str, Any]:
        return self.config_manager.get_google_workspace_settings()

    def client_id(self) -> str:
        return (self._cfg().get("client_id") or "").strip()

    def redirect_uri(self) -> str:
        return (self._cfg().get("oauth_redirect_uri") or DEFAULT_REDIRECT).strip()

    def scope(self) -> str:
        return (self._cfg().get("oauth_scope") or DEFAULT_SCOPE).strip()

    def client_secret(self) -> str:
        try:
            secret = self.secure_store.get(_SECRET_KEY)
        except Exception:
            secret = None
        return secret or (self._cfg().get("client_secret") or "")

    def set_client_secret(self, value: str) -> None:
        value = (value or "").strip()
        if value:
            self.secure_store.set(_SECRET_KEY, value)
        else:
            self.secure_store.delete(_SECRET_KEY)

    def _refresh_token(self) -> Optional[str]:
        try:
            token = self.secure_store.get(_REFRESH_KEY)
        except Exception:
            token = None
        return token or (self._cfg().get("refresh_token") or None)

    def is_configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def is_connected(self) -> bool:
        return self.is_configured() and bool(self._refresh_token())

    def disconnect(self) -> None:
        self.secure_store.delete(_REFRESH_KEY)
        self._access_token = None
        self._expires_at = 0.0
        if self._cfg().get("refresh_token"):
            self.config_manager.update_google_workspace_settings({"refresh_token": ""})

    # ── interactive login ────────────────────────────────────────────────
    def run_interactive_login(self, timeout: int = 180, open_browser=None) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "Enter the Google Client ID and Client Secret first."

        verifier, challenge = pkce_pair()
        state = random_state()
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "scope": self.scope(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        try:
            result = loopback_capture(
                f"{AUTH_URL}?{urlencode(params)}",
                self.redirect_uri(),
                timeout=timeout,
                open_browser=open_browser,
            )
        except (ValueError, RuntimeError, TimeoutError) as exc:
            return False, str(exc)

        if result.get("error"):
            return False, f"Google returned: {result.get('error_description') or result['error']}"
        if result.get("state") != state:
            return False, "Sign-in state mismatch — try again."
        code = result.get("code")
        if not code:
            return False, "No authorization code was returned."

        try:
            self._exchange_code(code, verifier)
        except GoogleOAuthError as exc:
            return False, str(exc)

        email = ""
        try:
            email = self.userinfo_email()
        except Exception:  # noqa: BLE001
            pass
        return True, f"Signed in to Google{f' as {email}' if email else ''}."

    # ── token endpoint ───────────────────────────────────────────────────
    def _post_token(self, data: Dict[str, str]) -> Dict[str, Any]:
        req = Request(
            TOKEN_URL,
            data=urlencode(data).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error_description", "")
            except Exception:
                pass
            raise GoogleOAuthError(
                f"Google token request failed (HTTP {exc.code}). {detail}".strip()
            ) from exc
        except URLError as exc:
            raise GoogleOAuthError(f"Could not reach Google: {exc.reason}") from exc

    def _store(self, payload: Dict[str, Any]) -> None:
        access = payload.get("access_token")
        if not access:
            raise GoogleOAuthError("Google did not return an access token.")
        self._access_token = access
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
        refresh = payload.get("refresh_token")
        if refresh:
            self.secure_store.set(_REFRESH_KEY, refresh)

    def _exchange_code(self, code: str, verifier: str) -> None:
        self._store(
            self._post_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri(),
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                    "code_verifier": verifier,
                }
            )
        )

    def _refresh(self) -> None:
        refresh = self._refresh_token()
        if not refresh:
            raise GoogleOAuthError("Not signed in to Google.")
        self._store(
            self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                }
            )
        )

    def access_token(self, force: bool = False) -> str:
        if force or not self._access_token or time.monotonic() >= self._expires_at - 60:
            self._refresh()
        assert self._access_token
        return self._access_token

    def userinfo_email(self) -> str:
        req = Request(
            USERINFO_URL, headers={"Authorization": f"Bearer {self.access_token()}"}
        )
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8")).get("email", "")
        except (HTTPError, URLError):
            return ""
